# Aletheia — duplicate-booking hardening (self-checks against app.py via Flask test client)
# Run:  python tests/dedupe_test.py
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Must be set BEFORE importing app: forces file mode (never touch Upstash cloud DB)
os.environ["UPSTASH_REDIS_REST_URL"] = ""
os.environ["UPSTASH_REDIS_REST_TOKEN"] = ""

_tmp = tempfile.mkdtemp(prefix="aletheia-test-")

import app as appmod  # noqa: E402

# Redirect the file DB to a temp dir so local data/db.json is never touched
appmod.DATA_FILE = Path(_tmp) / "db.json"
appmod.DB = {"registrations": [], "payments": []}
# Never send real emails during tests
appmod.send_ticket_email = lambda *a, **k: False

client = appmod.app.test_client()


def register(email, name="Test User", persons=2):
    return client.post("/api/register", json={
        "name": name, "email": email, "persons": persons,
    })


class DedupeTests(unittest.TestCase):
    def setUp(self):
        appmod.DB = {"registrations": [], "payments": []}

    def test_fresh_registration_creates_pending(self):
        r = register("fresh@example.com")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d["status"], "pending")
        self.assertNotIn("existing", d)
        self.assertTrue(d["id"].startswith("ME-"))
        self.assertIn("pa=7795498451%40ptyes", d["upiUri"])  # real VPA, not the dead @ybl

    def test_duplicate_email_returns_existing_ticket(self):
        first = register("dup@example.com", persons=1).get_json()
        second = register("dup@example.com", persons=3)  # different persons on purpose
        self.assertEqual(second.status_code, 200)
        d = second.get_json()
        self.assertTrue(d.get("existing"), "second register must be flagged existing")
        self.assertEqual(d["id"], first["id"], "same ticket id, not a new booking")
        self.assertEqual(d["persons"], 1, "original person count preserved")
        self.assertEqual(d["status"], "pending")
        self.assertIn("upiUri", d)
        # no duplicate rows were created
        self.assertEqual(len(appmod.DB["registrations"]), 1)
        self.assertEqual(len(appmod.DB["payments"]), 1)

    def test_dedupe_self_heals_missing_payment_row(self):
        reg = register("heal@example.com").get_json()
        appmod.DB["payments"] = []  # simulate the corrupted/lost payment record
        r = register("heal@example.com")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertTrue(d.get("existing"))
        self.assertEqual(d["id"], reg["id"])
        self.assertIn("upiUri", d)
        self.assertEqual(len(appmod.DB["payments"]), 1, "payment row regenerated")

    def test_dedupe_regenerates_uri_minted_with_old_vpa(self):
        reg = register("oldvpa@example.com").get_json()
        # simulate a booking from the wrong-@ybl era
        appmod.DB["payments"][0]["upiUri"] = appmod.DB["payments"][0]["upiUri"].replace(
            "7795498451@ptyes", "7795498451@ybl")
        appmod.DB["payments"][0]["upiId"] = "7795498451@ybl"
        d = register("oldvpa@example.com").get_json()
        self.assertTrue(d.get("existing"))
        self.assertIn("pa=7795498451%40ptyes", d["upiUri"], "stale VPA must be regenerated")

    def test_paid_booking_does_not_block_re_registration(self):
        reg = register("paid@example.com").get_json()
        appmod.mark_paid(reg["id"], source="test")
        d = register("paid@example.com").get_json()
        self.assertNotIn("existing", d)
        self.assertNotEqual(d["id"], reg["id"], "paid ticket stays valid; new booking allowed")

    def test_refunded_booking_does_not_block_re_registration(self):
        reg = register("refund@example.com").get_json()
        appmod.DB["registrations"][0]["status"] = "refunded"
        d = register("refund@example.com").get_json()
        self.assertNotIn("existing", d)
        self.assertNotEqual(d["id"], reg["id"])

    def test_older_than_24h_pending_allows_new_booking(self):
        reg = register("old@example.com").get_json()
        row = next(x for x in appmod.DB["registrations"] if x["id"] == reg["id"])
        row["createdAt"] -= 86_400_000 + 60_000  # 24h + 1 min ago
        d = register("old@example.com").get_json()
        self.assertNotIn("existing", d)
        self.assertNotEqual(d["id"], reg["id"])

    def test_different_emails_never_collide(self):
        a = register("a@example.com").get_json()
        b = register("b@example.com").get_json()
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual(len(appmod.DB["registrations"]), 2)

    def test_validation_errors_intact(self):
        self.assertEqual(register("", name="").status_code, 400)
        self.assertEqual(register("not-an-email").status_code, 400)


def rewind_first_booking(ms=601_000):
    """Make the only booking look 10+ minutes old (past the payment window)."""
    row = appmod.DB["registrations"][0]
    row["createdAt"] -= ms
    row["expiresAt"] = row["createdAt"] + appmod.PAYMENT_TIMEOUT_MS
    return row


def webhook(text):
    return client.post("/api/upi-webhook", json={"key": appmod.ADMIN_KEY, "text": text})


class ExpiryTests(unittest.TestCase):
    def setUp(self):
        appmod.DB = {"registrations": [], "payments": []}

    def test_registration_carries_10min_expiry(self):
        d = register("exp-fresh@example.com", persons=1).get_json()
        self.assertAlmostEqual(d["expiresAt"], d["createdAt"] + 600_000, delta=1500)

    def test_status_expires_stale_pending(self):
        reg = register("exp-status@example.com", persons=1).get_json()
        rewind_first_booking()
        r = client.get(f"/api/status/{reg['id']}")
        self.assertEqual(r.get_json()["status"], "expired")

    def test_paid_booking_never_expires(self):
        reg = register("exp-paid@example.com", persons=1).get_json()
        appmod.mark_paid(reg["id"], source="test")
        rewind_first_booking()
        r = client.get(f"/api/status/{reg['id']}")
        self.assertEqual(r.get_json()["status"], "paid")

    def test_expired_booking_does_not_block_new_registration(self):
        first = register("exp-again@example.com", persons=1).get_json()
        rewind_first_booking()
        second = register("exp-again@example.com", persons=2).get_json()
        self.assertNotEqual(second["id"], first["id"])
        self.assertNotIn("existing", second)

    def test_webhook_revives_expired_booking_via_code(self):
        """Money-safe core: late UPI payment still issues the ticket."""
        reg = register("exp-late@example.com", persons=1).get_json()
        rewind_first_booking()
        txn = (datetime.now() - timedelta(seconds=30)).strftime("on %d-%m-%y at %I:%M%p")
        resp = webhook(f"Received Rs 69 from Test User {txn} Ticket {reg['id']}")
        d = resp.get_json()
        self.assertTrue(d.get("accepted"), d)
        self.assertEqual(d["ticket"], reg["id"])
        self.assertEqual(client.get(f"/api/status/{reg['id']}").get_json()["status"], "paid")

    def test_webhook_fallback_finds_expired_booking(self):
        register("exp-fb@example.com", persons=1)
        rewind_first_booking()
        txn = (datetime.now() - timedelta(seconds=30)).strftime("on %d-%m-%y at %I:%M%p")
        d = webhook(f"Rs 69 received from Test User {txn}").get_json()
        self.assertTrue(d.get("accepted"), d)

    def test_webhook_still_rejects_txn_before_expired_booking(self):
        register("exp-early@example.com", persons=1)
        rewind_first_booking()
        old = (datetime.now() - timedelta(hours=1)).strftime("on %d-%m-%y at %I:%M%p")
        d = webhook(f"Received Rs 69 from Test User {old}").get_json()
        self.assertFalse(d.get("accepted"))
        self.assertEqual(d.get("reason"), "txn-before-booking")  # code matched, but txn predates booking

    def test_admin_data_counts_expired(self):
        register("exp-count@example.com", persons=1)
        rewind_first_booking()
        client.get(f"/api/status/{appmod.DB['registrations'][0]['id']}")
        import base64
        req = client.get("/api/admin/data", headers={"x-admin-key": appmod.ADMIN_KEY})
        self.assertEqual(req.get_json()["stats"]["expired"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

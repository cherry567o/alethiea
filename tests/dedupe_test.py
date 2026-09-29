# Aletheia — duplicate-booking hardening (self-checks against app.py via Flask test client)
# Run:  python tests/dedupe_test.py
import os
import sys
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main(verbosity=2)

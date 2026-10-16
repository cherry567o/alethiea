# Email-first flow + security hardening + Razorpay checkout, against app.py (Flask test client).
# Run:  python -m unittest tests.flow_security_test
import hashlib, hmac, json, os, sys, tempfile, unittest, urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.update({
    "ALETHEIA_NO_DOTENV": "1", "UPSTASH_REDIS_REST_URL": "", "UPSTASH_REDIS_REST_TOKEN": "",
    "ADMIN_KEY": "test-admin-key-0123456789abcdef", "PAYMENT_MODE": "razorpay",
    "RAZORPAY_KEY_ID": "rzp_test_abc", "RAZORPAY_KEY_SECRET": "secret", "RAZORPAY_WEBHOOK_SECRET": "whsec_test",
    "SMTP_USER": "sender@example.com", "SMTP_PASS": "x", "PUBLIC_BASE_URL": "https://example.com",
})
for k in ("UPI_ID", "STRIPE_SECRET_KEY", "ALLOW_DEMO", "REQUIRE_EMAIL_LINK"):
    os.environ.pop(k, None)
import app as appmod  # noqa: E402

REAL_SAVE, REAL_HIT, REAL_INVITE = appmod._save, appmod._hit, appmod.send_invite_email
appmod.DATA_FILE = Path(tempfile.mkdtemp()) / "db.json"
KEY = os.environ["ADMIN_KEY"]
SENT, INVITES, FAIL = [], [], {"code": None}


def fake_rzp(method, path, body=None, timeout=20):
    SENT.append((method, path, body))
    if FAIL["code"]:
        raise urllib.error.HTTPError("https://api.razorpay.com" + path, FAIL["code"], "err", {}, None)
    if path == "/orders" and method == "POST":
        return {"id": "order_T%d" % len(SENT), "amount": body["amount"], "currency": "INR", "status": "created"}
    return {"items": []}


class Base(unittest.TestCase):
    def setUp(self):
        appmod.DB = {"registrations": [], "payments": []}
        appmod._save = lambda db: None
        appmod._hit = lambda *a, **k: 0
        appmod._rzp_request = fake_rzp
        appmod.send_ticket_email = lambda *a, **k: None
        appmod.send_invite_email = lambda lead, base: INVITES.append((dict(lead), base))
        appmod.EMAIL_FLOW = True
        appmod.PAYMENT_MODE = "razorpay"
        appmod.deadline_info = lambda: None
        FAIL["code"] = None
        SENT.clear(); INVITES.clear(); appmod._MEM_HITS.clear()
        self.c = appmod.app.test_client()

    def lead(self, email="a@b.co", interests=("time",)):
        lead = {"token": "tok_" + os.urandom(8).hex(), "email": email, "name": "", "interests": list(interests),
                "createdAt": int(__import__("time").time() * 1000), "lastSentAt": 0}
        appmod._leads().append(lead)
        return lead

    def register(self, persons=2, email="a@b.co"):
        lead = self.lead(email)
        r = self.c.post("/api/register", json={"name": "Ravi", "email": email, "persons": persons, "token": lead["token"]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()


class EmailFlow(Base):
    def test_config_lists_the_nine_topics(self):
        j = self.c.get("/api/config").get_json()
        self.assertTrue(j["emailFlow"])
        self.assertEqual(len(j["topics"]), 9)

    def test_interest_stores_lead_and_sends_personal_link(self):
        r = self.c.post("/api/interest", json={"email": "Ravi@Example.com", "name": "Ravi", "interests": ["time", "karma"]})
        self.assertEqual(r.status_code, 200)
        lead, base = INVITES[-1]
        self.assertEqual(lead["email"], "ravi@example.com")
        self.assertEqual(lead["interests"], ["time", "karma"])
        self.assertEqual(base, "https://example.com")
        self.assertGreaterEqual(len(lead["token"]), 16)

    def test_interest_validation(self):
        post = lambda b: self.c.post("/api/interest", json=b).status_code
        self.assertEqual(post({"email": "nope", "interests": ["time"]}), 400)
        self.assertEqual(post({"email": "a@b.co", "interests": []}), 400)
        self.assertEqual(post({"email": "a@b.co", "interests": ["hacked"]}), 400)
        self.assertEqual(post({"email": "a@b.co"}), 400)
        self.assertEqual(post({"email": "a@b.co", "interests": ["hacked", "faith"]}), 200)
        self.assertEqual(INVITES[-1][0]["interests"], ["faith"])  # unknown topics are dropped

    def test_resend_is_throttled_but_reuses_link(self):
        self.assertEqual(self.c.post("/api/interest", json={"email": "a@b.co", "interests": ["time"]}).status_code, 200)
        self.assertEqual(self.c.post("/api/interest", json={"email": "a@b.co", "interests": ["time"]}).status_code, 429)
        appmod._leads()[0]["lastSentAt"] = 0  # a minute later
        self.assertEqual(self.c.post("/api/interest", json={"email": "a@b.co", "interests": ["dreams"]}).status_code, 200)
        self.assertEqual(len(appmod._leads()), 1)
        self.assertEqual(appmod._leads()[0]["interests"], ["dreams"])

    def test_email_failure_is_reported(self):
        def boom(lead, base): raise RuntimeError("smtp down")
        appmod.send_invite_email = boom
        self.assertEqual(self.c.post("/api/interest", json={"email": "a@b.co", "interests": ["time"]}).status_code, 502)

    def test_closed_deadline_blocks_signups(self):
        appmod.deadline_info = lambda: {"closed": True}
        self.assertEqual(self.c.post("/api/interest", json={"email": "a@b.co", "interests": ["time"]}).status_code, 403)

    def test_lead_lookup(self):
        lead = self.lead("x@y.co", ("time", "faith"))
        j = self.c.get("/api/lead/" + lead["token"]).get_json()
        self.assertEqual(j["email"], "x@y.co")
        self.assertEqual(j["interests"], ["Time & reality", "God & faith"])
        self.assertEqual(self.c.get("/api/lead/nope").status_code, 404)
        self.assertEqual(self.c.get("/api/lead/" + "z" * 24).status_code, 404)
        lead["createdAt"] = 0  # expired
        self.assertEqual(self.c.get("/api/lead/" + lead["token"]).status_code, 404)

    def test_register_requires_the_emailed_link(self):
        body = {"name": "Ravi", "email": "a@b.co", "persons": 1}
        r = self.c.post("/api/register", json=body)
        self.assertEqual(r.status_code, 403)
        self.assertTrue(r.get_json()["needLink"])
        self.assertEqual(self.c.post("/api/register", json={**body, "token": "wrong-token-value-123"}).status_code, 403)
        self.assertEqual(appmod.DB["registrations"], [])

    def test_register_binds_email_and_saves_interests(self):
        lead = self.lead("real@owner.co", ("karma", "dreams"))
        r = self.c.post("/api/register", json={"name": "Ravi", "email": "someone@else.co", "persons": 1, "token": lead["token"]})
        self.assertEqual(r.status_code, 200)
        reg = appmod.DB["registrations"][0]
        self.assertEqual(reg["email"], "real@owner.co")        # form email ignored
        self.assertEqual(reg["interests"], ["karma", "dreams"])
        self.assertEqual(lead["registeredId"], reg["id"])

    def test_flow_off_keeps_open_registration(self):
        appmod.EMAIL_FLOW = False
        r = self.c.post("/api/register", json={"name": "Ravi", "email": "a@b.co", "persons": 1})
        self.assertEqual(r.status_code, 200)

    def test_invite_email_escapes_html_and_carries_link(self):
        import smtplib
        captured = []
        class FakeSMTP:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def starttls(self): pass
            def login(self, *a): pass
            def send_message(self, m): captured.append(m)
        real_smtp = smtplib.SMTP
        smtplib.SMTP = FakeSMTP
        try:
            lead = {"token": "T" * 22, "email": "a@b.co\r\nBcc: evil@x.co", "name": "<script>alert(1)</script>", "interests": ["time"]}
            REAL_INVITE(lead, "https://example.com")
        finally:
            smtplib.SMTP = real_smtp
        m = captured[0]
        self.assertNotIn("\n", m["To"])                      # header injection stripped
        html_part = m.get_body(preferencelist=("html",)).get_content()
        plain_part = m.get_body(preferencelist=("plain",)).get_content()
        self.assertNotIn("<script>", html_part)
        self.assertIn("&lt;script&gt;", html_part)
        link = "https://example.com/register?link=" + "T" * 22
        self.assertIn(link, html_part)
        self.assertIn(link, plain_part)
        self.assertIn("Time & reality", plain_part)

class Security(Base):
    def test_admin_is_header_only_and_has_no_default(self):
        self.assertEqual(self.c.get("/api/admin/data").status_code, 401)
        self.assertEqual(self.c.get("/api/admin/data?key=" + KEY).status_code, 401)
        self.assertEqual(self.c.get("/api/admin/data", headers={"x-admin-key": "changeme123"}).status_code, 401)
        self.assertEqual(self.c.get("/api/admin/data", headers={"x-admin-key": KEY}).status_code, 200)
        old = appmod.ADMIN_READY; appmod.ADMIN_READY = False
        try:
            self.assertEqual(self.c.get("/api/admin/data", headers={"x-admin-key": KEY}).status_code, 401)
        finally:
            appmod.ADMIN_READY = old

    def test_checkin_page_escapes_and_never_prints_key(self):
        r = self.register()
        reg = appmod.DB["registrations"][0]
        reg["name"] = "<img src=x onerror=alert(1)>"
        appmod.mark_paid(r["id"], 138, "test")
        body = self.c.get("/checkin/%s?key=%s" % (r["id"], KEY)).get_data(as_text=True)
        self.assertNotIn("<img", body)
        self.assertIn("&lt;img", body)
        self.assertNotIn(KEY, body)

    def test_security_headers_allow_only_razorpay(self):
        h = self.c.get("/api/config").headers
        for k in ("X-Frame-Options", "X-Content-Type-Options", "Strict-Transport-Security", "Referrer-Policy"):
            self.assertIn(k, h)
        self.assertIn("checkout.razorpay.com", h["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", h["Content-Security-Policy"])

    def test_forwarded_host_is_not_trusted(self):
        old = appmod.PUBLIC_BASE_URL; appmod.PUBLIC_BASE_URL = ""
        os.environ.pop("VERCEL_PROJECT_PRODUCTION_URL", None)
        try:
            j = self.c.get("/api/start-qr", headers={"X-Forwarded-Host": "evil.com"}).get_json()
            self.assertNotIn("evil.com", j["url"])
        finally:
            appmod.PUBLIC_BASE_URL = old

    def test_rate_limits(self):
        appmod._hit = REAL_HIT
        codes = [self.c.post("/api/interest", json={"email": "u%d@b.co" % i, "interests": ["time"]}).status_code for i in range(8)]
        self.assertIn(429, codes)

    def test_no_payment_method_refuses_bookings(self):
        appmod.PAYMENT_MODE = "off"
        r = self.c.post("/api/register", json={"name": "A", "email": "a@b.co", "token": self.lead()["token"]})
        self.assertEqual(r.status_code, 503)

    def test_demo_payments_disabled(self):
        r = self.register()
        self.assertEqual(self.c.post("/api/demo/pay/" + r["id"]).status_code, 403)
        self.assertEqual(appmod.find_reg(r["id"])["status"], "pending")

    def test_frontend_has_no_secrets(self):
        for f in (ROOT / "public").glob("*.html"):
            t = f.read_text(encoding="utf-8")
            self.assertNotIn("changeme123", t)
            self.assertNotIn("key_secret", t.lower().replace("razorpay_key_secret", ""))
            self.assertNotIn("?key=", t)


class Checkout(Base):
    def sig(self, order_id, payment_id, secret="secret"):
        return hmac.new(secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()

    def order(self, rid, **extra):
        return self.c.post("/api/create-order", json={"reg_id": rid, **extra})

    def verify(self, rid, oid, pid="pay_1", sig=None):
        return self.c.post("/api/verify-payment", json={"reg_id": rid, "razorpay_order_id": oid, "razorpay_payment_id": pid,
                                                        "razorpay_signature": self.sig(oid, pid) if sig is None else sig})

    def hook(self, event, entity, secret="whsec_test", sig=None):
        body = json.dumps({"event": event.split(":")[0], "payload": {event.split(":")[0].split(".")[0]: {"entity": entity}}}).encode()
        sig = sig or hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return self.c.post("/webhook", data=body, headers={"X-Razorpay-Signature": sig, "Content-Type": "application/json"})

    def test_amount_comes_from_the_server(self):
        r = self.register(persons=2)
        res = self.order(r["id"], amount=100)
        self.assertEqual(res.status_code, 200)
        sent = [x for x in SENT if x[1] == "/orders"][-1][2]
        self.assertEqual((sent["amount"], sent["receipt"]), (13800, r["id"]))
        self.assertEqual(res.get_json()["key_id"], "rzp_test_abc")
        self.assertNotIn("secret", json.dumps(res.get_json()).replace("rzp_test", ""))

    def test_bad_keys_401_and_razorpay_errors_502(self):
        r = self.register()
        FAIL["code"] = 401
        self.assertEqual(self.order(r["id"]).status_code, 401)
        FAIL["code"] = 500
        self.assertEqual(self.order(r["id"]).status_code, 502)

    def test_valid_signature_issues_ticket_and_is_idempotent(self):
        r = self.register()
        o = self.order(r["id"]).get_json()["order_id"]
        self.assertEqual(self.verify(r["id"], o).status_code, 200)
        reg = appmod.find_reg(r["id"])
        self.assertEqual(reg["status"], "paid")
        pin = reg["pin"]
        self.assertTrue(self.verify(r["id"], o).get_json().get("already"))
        self.assertEqual(appmod.find_reg(r["id"])["pin"], pin)

    def test_bad_or_missing_signature_never_marks_paid(self):
        r = self.register()
        o = self.order(r["id"]).get_json()["order_id"]
        self.assertEqual(self.verify(r["id"], o, sig="0" * 64).status_code, 400)
        self.assertEqual(self.verify(r["id"], o, sig=self.sig(o, "pay_1", "wrong")).status_code, 400)
        self.assertEqual(self.c.post("/api/verify-payment", json={"reg_id": r["id"]}).status_code, 400)
        self.assertEqual(appmod.find_reg(r["id"])["status"], "pending")

    def test_paying_the_first_of_two_orders_still_works(self):
        r = self.register()
        first = self.order(r["id"]).get_json()["order_id"]
        self.order(r["id"])  # double click -> second order
        self.assertEqual(self.verify(r["id"], first).status_code, 200)
        self.assertEqual(appmod.find_reg(r["id"])["status"], "paid")

    def test_order_of_another_booking_cannot_pay_this_one(self):
        a, b = self.register(email="a@x.co"), self.register(email="b@x.co")
        oa = self.order(a["id"]).get_json()["order_id"]
        self.order(b["id"])
        # A's order paid, presented against B's booking id: the money belongs to A, B gets nothing.
        self.assertEqual(self.verify(b["id"], oa).status_code, 200)
        self.assertEqual(appmod.find_reg(b["id"])["status"], "pending")
        self.assertEqual(appmod.find_reg(a["id"])["status"], "paid")

    def test_refunded_booking_is_not_revived_by_a_late_payment(self):
        r = self.register()
        o = self.order(r["id"]).get_json()["order_id"]
        appmod.find_reg(r["id"])["status"] = "refunded"
        self.assertEqual(self.verify(r["id"], o).status_code, 409)
        self.assertEqual(appmod.find_reg(r["id"])["status"], "refunded")

    def test_webhook_fails_closed_and_checks_amount(self):
        r = self.register(persons=2)
        o = self.order(r["id"]).get_json()["order_id"]
        ent = {"id": o, "receipt": r["id"], "amount_paid": 13800, "notes": {"registrationId": r["id"]}}
        self.assertEqual(self.hook("order.paid", ent, sig="deadbeef").status_code, 400)
        self.assertEqual(self.hook("order.paid", {**ent, "amount_paid": 100}).status_code, 200)
        self.assertEqual(appmod.find_reg(r["id"])["status"], "pending")      # underpaid: ignored
        self.assertEqual(self.hook("order.paid", ent).status_code, 200)
        self.assertEqual(appmod.find_reg(r["id"])["status"], "paid")
        old = appmod.RZP_WEBHOOK_SECRET; appmod.RZP_WEBHOOK_SECRET = ""
        try:
            self.assertEqual(self.hook("order.paid", ent).status_code, 503)
        finally:
            appmod.RZP_WEBHOOK_SECRET = old

    def test_payment_captured_found_by_order_id(self):
        r = self.register(persons=1)
        o = self.order(r["id"]).get_json()["order_id"]
        self.assertEqual(self.hook("payment.captured", {"order_id": o, "amount": 6900, "notes": {}}).status_code, 200)
        self.assertEqual(appmod.find_reg(r["id"])["status"], "paid")

    def test_gateway_refund_cancels_ticket_with_dashboard_note(self):
        r = self.register()
        o = self.order(r["id"]).get_json()["order_id"]
        self.verify(r["id"], o)
        res = self.c.post("/api/admin/refund", json={"id": r["id"]}, headers={"x-admin-key": KEY})
        self.assertEqual(res.status_code, 200)
        self.assertIn("Razorpay", res.get_json()["note"])
        self.assertEqual(appmod.find_reg(r["id"])["status"], "refunded")


class Lock(Base):
    def setUp(self):
        super().setUp()
        self.store = st = {}
        def fake(*parts):
            op = parts[0].upper()
            if op == "GET": return st.get(parts[1])
            if op == "SET":
                if "NX" in [x.upper() for x in parts[3:]] and parts[1] in st: return None
                st[parts[1]] = parts[2]; return "OK"
            if op == "DEL": st.pop(parts[1], None); return 1
            if op == "INCR": st[parts[1]] = str(int(st.get(parts[1], 0)) + 1); return int(st[parts[1]])
            return 1
        self.saved = (appmod._redis_cmd, appmod.REDIS_URL, appmod.REDIS_TOKEN, appmod.time.sleep)
        appmod._redis_cmd, appmod.REDIS_URL, appmod.REDIS_TOKEN = fake, "http://fake", "t"
        appmod._save = REAL_SAVE
        appmod.time.sleep = lambda s: None
        appmod.EMAIL_FLOW = False

    def tearDown(self):
        appmod._redis_cmd, appmod.REDIS_URL, appmod.REDIS_TOKEN, appmod.time.sleep = self.saved
        appmod._save = lambda db: None

    def test_other_writes_preserved_and_lock_released(self):
        self.store["aletheia:db"] = json.dumps({"registrations": [{"id": "ME-AAAAAA", "name": "O", "email": "o@x.co", "persons": 1,
            "expectedAmount": 69, "amountPaid": 0, "status": "pending", "checkedIn": 0, "createdAt": 1}], "payments": []})
        r = self.c.post("/api/register", json={"name": "N", "email": "n@x.co", "persons": 1})
        self.assertEqual(r.status_code, 200)
        ids = [x["id"] for x in json.loads(self.store["aletheia:db"])["registrations"]]
        self.assertIn("ME-AAAAAA", ids); self.assertEqual(len(ids), 2)
        self.assertNotIn("aletheia:lock", self.store)

    def test_busy_lock_returns_503_and_is_not_stolen(self):
        self.store["aletheia:lock"] = "someone-else"
        self.assertEqual(self.c.post("/api/register", json={"name": "N", "email": "n@x.co"}).status_code, 503)
        self.assertEqual(self.store["aletheia:lock"], "someone-else")

    def test_failed_db_write_is_an_error_not_silent(self):
        real = appmod._redis_cmd
        appmod._redis_cmd = lambda *p: None if (p[0].upper() == "SET" and p[1] == "aletheia:db") else real(*p)
        self.assertEqual(self.c.post("/api/register", json={"name": "N", "email": "n@x.co"}).status_code, 500)


if __name__ == "__main__":
    unittest.main()

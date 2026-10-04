"""
Aletheia — event registration, Razorpay payments (UPI-ready), QR flow, admin monitor.

Flow:  /start (poster + share QR)  →  /register (form)  →  /pay/<id> (payment QR)
       → paid entry ticket QR → /admin.html monitoring.

Payments: Razorpay Payment Links — ₹69 × persons, QR opens the Razorpay page
(UPI / cards / netbanking, whatever your account has enabled).
Runs in DEMO MODE without Razorpay keys (payments simulated locally).
"""
import base64
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import smtplib
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode, unquote
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import qrcode
from flask import Flask, jsonify, redirect, request, send_from_directory

try:
    from dotenv import load_dotenv
    # load .env from THIS file's folder (works locally, under WSGI, and on PythonAnywhere)
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

# ---------------- config ----------------
UPI_ID = os.environ.get("UPI_ID", "").strip()  # e.g. yourname@okhdfcbank — plain-UPI mode when set
RZP_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "").strip()
RZP_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "").strip()
RZP_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "").strip()
RZP_READY = bool(RZP_KEY_ID and RZP_KEY_SECRET and RZP_KEY_ID.startswith(("rzp_test_", "rzp_live_")))

# Payment mode: "upi" (direct UPI QR + manual verification) beats Razorpay when UPI_ID is set.
# Stripe self-checkout (attendee pays on the same phone; payment auto-verified by Stripe).
# Note: Stripe is not currently accepting new Indian merchant accounts - activate only if
# you already have keys. Without a key, UPI mode stays active.
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
PAYMENT_MODE = "stripe" if STRIPE_SECRET_KEY.startswith("sk_") else ("upi" if UPI_ID else ("razorpay" if RZP_READY else "demo"))

ADMIN_KEY = os.environ.get("ADMIN_KEY", "changeme123")
PORT = int(os.environ.get("PORT", "3000") or 3000)
if PORT <= 0:  # some environments export PORT=0 — pick our predictable default
    PORT = 3000
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
# Booking deadline — registrations auto-close after this date (end of day).
# Override or clear it by setting REG_DEADLINE in .env (e.g. REG_DEADLINE=2026-12-01).
REG_DEADLINE = os.environ.get("REG_DEADLINE", "2026-11-20").strip()  # day before the event

# ---- outbound email (ticket delivery) — plain SMTP, e.g. Gmail with an app password ----
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587") or 587)
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_PASS = os.environ.get("SMTP_PASS", "").strip()  # Gmail: use an App Password, not the login password
SMTP_FROM = os.environ.get("SMTP_FROM", "").strip() or SMTP_USER
PER_PERSON_PRICE = 69  # rupees
# Pending bookings expire after 10 minutes without verified payment — keeps the
# books clean. Money-safe: the auto-verify webhook revives an expired booking
# when its payment actually arrives (late UPI notifications, slow confirmations).
PAYMENT_TIMEOUT_MS = 10 * 60 * 1000
CURRENCY = "INR"
RZP_API = "https://api.razorpay.com/v1"

BASE_DIR = Path(__file__).parent
PUBLIC_DIR = BASE_DIR / "public"
DATA_FILE = BASE_DIR / "data" / "db.json"
try:  # Vercel's filesystem is read-only — the cloud DB (Upstash) is used there instead
    DATA_FILE.parent.mkdir(exist_ok=True)
except Exception:
    pass

app = Flask(__name__, static_folder=str(PUBLIC_DIR), static_url_path="")

# --- Vercel rewrite support -------------------------------------------------
# vercel.json sends every request to the function with the real path carried
# in the __vpath query param (Vercel's rewrite replaces the URL path itself).
# Wrapping wsgi_app (not the Flask instance) guarantees it runs no matter how
# the serverless runtime invokes the app. No-op locally (no __vpath present).
class _RestoreOriginalPath:
    def __init__(self, wsgi):
        self.wsgi = wsgi

    def __call__(self, environ, start_response):
        raw = environ.get("QUERY_STRING", "")
        vpath, keep = "", []
        for pair in raw.split("&") if raw else []:
            if pair.startswith("__vpath="):
                vpath = pair[len("__vpath="):]
            else:
                keep.append(pair)
        if vpath:
            environ["PATH_INFO"] = "/" + unquote(vpath).strip("/")
            environ["QUERY_STRING"] = "&".join(keep)
        return self.wsgi(environ, start_response)


app.wsgi_app = _RestoreOriginalPath(app.wsgi_app)


@app.before_request
def _fresh_db_per_request():
    """Serverless instances stay warm and would otherwise serve stale in-memory
    bookings (and resurrect wiped rows on their next save). Reload from the
    cloud DB on every request; locally (file mode) this keeps parity too."""
    global DB
    if REDIS_URL and REDIS_TOKEN:
        DB = _load()


@app.after_request
def _no_cache_html(resp):
    """HTML pages must always be fresh — stale caches on phones caused poster glitches."""
    if (resp.content_type or "").startswith("text/html"):
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp

# ---------------- tiny JSON db (file locally, Upstash Redis on Vercel) ----------------

# On Vercel the filesystem is read-only, so bookings must live in a cloud store.
# Set UPSTASH_REDIS_REST_URL + UPSTASH_REDIS_REST_TOKEN (Vercel → Storage → Upstash
# → .env.local pulls them in; locally leave them empty and the file db is used).
REDIS_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").strip()
REDIS_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "").strip()


def _redis_cmd(*parts):
    """One Upstash REST call. Returns the parsed reply (None on any failure)."""
    if not (REDIS_URL and REDIS_TOKEN):
        return None
    try:
        req = urllib.request.Request(
            REDIS_URL.rstrip("/") + "/" + "/".join(urllib.request.quote(p, safe="") for p in parts),
            headers={"Authorization": f"Bearer {REDIS_TOKEN}"},
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode("utf-8")).get("result")
    except Exception as e:
        app.logger.error("Upstash error: %s", e)
        return None


def _load():
    if REDIS_URL and REDIS_TOKEN:
        raw = _redis_cmd("GET", "aletheia:db")
        if raw:
            try:
                return json.loads(raw)
            except Exception:
                pass
        return {"registrations": [], "payments": []}
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"registrations": [], "payments": []}


def _save(db):
    if REDIS_URL and REDIS_TOKEN:
        _redis_cmd("SET", "aletheia:db", json.dumps(db))
        return
    DATA_FILE.write_text(json.dumps(db, indent=2), encoding="utf-8")


DB = _load()

# ---------------- helpers ----------------

def public_base(request):
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    # behind a proxy (e.g. PythonAnywhere), trust the forwarded host header
    host = request.headers.get("X-Forwarded-Host") or request.host
    scheme = request.headers.get("X-Forwarded-Proto") or request.scheme
    return f"{scheme}://{host}".rstrip("/")


def _deadline_ts_from_env():
    if not REG_DEADLINE:
        return None
    try:
        s = REG_DEADLINE.replace("T", " ")
        if len(s) == 10:  # a bare date — close at the very end of that day
            return (datetime.fromisoformat(s) + timedelta(days=1, seconds=-1)).timestamp()
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


DEADLINE_TS = _deadline_ts_from_env()


def deadline_info():
    """None when no deadline is configured, else {deadlineTs(ms), closed, daysLeft}."""
    if not DEADLINE_TS:
        return None
    now = time.time()
    return {
        "deadlineTs": int(DEADLINE_TS * 1000),
        "closed": now >= DEADLINE_TS,
        "daysLeft": max(0, int((DEADLINE_TS - now) // 86400)),
    }


def make_qr(text: str) -> str:
    """Return a QR code as a data: URL (PNG)."""
    img = qrcode.make(text)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def new_id():
    return "ME-" + secrets.token_hex(3).upper()


# NOTE: we deliberately keep the advertised price EXACT (₹69.00 — no odd paise) so
# guests are never asked for a different amount than advertised. Auto-accept only
# fires when the paid amount is unambiguous (exactly one pending booking at that
# amount); ties fall back to one-click manual approval in the admin panel.


def upi_payment_uri(reg: dict) -> str:
    """Standard UPI deep link — scanning opens GPay/PhonePe/Paytm with the
    advertised amount pre-filled."""
    params = {
        "pa": UPI_ID,
        "pn": "Aletheia",
        "am": str(reg["expectedAmount"]),
        "cu": "INR",
        "tn": f"Ticket {reg['id']} {reg['name']}",
    }
    from urllib.parse import urlencode, unquote
    return "upi://pay?" + urlencode(params)


def find_reg(reg_id):
    return next((r for r in DB["registrations"] if r["id"] == reg_id), None)


def _expire_stale_pendings():
    """Flip pending bookings older than PAYMENT_TIMEOUT_MS to 'expired' so guests
    register fresh instead of stacking stale rows. Called at the start of
    register/status flows; the webhook can still revive them if money arrived."""
    now = int(time.time() * 1000)
    changed = False
    for r in DB["registrations"]:
        if r.get("status") == "pending" and now - r.get("createdAt", now) > PAYMENT_TIMEOUT_MS:
            r["status"] = "expired"
            r["expiredAt"] = now
            changed = True
    if changed:
        _save(DB)
    return changed


def mark_paid(reg_id, paid_amount=None, source=""):
    reg = find_reg(reg_id)
    if not reg or reg["status"] == "paid":
        return reg
    amount = paid_amount or reg["expectedAmount"]
    reg["status"] = "paid"
    reg["amountPaid"] = amount
    reg["paidAt"] = int(time.time() * 1000)  # shown in admin as 'accepted <time>'
    reg["paidSource"] = source  # 'auto-upi' shows as 🤖 in the admin panel
    # Door PIN: 4 digits, required with the ticket at check-in so a stolen ticket
    # link alone can't burn someone's entry.
    reg["pin"] = f"{secrets.randbelow(10000):04d}"
    payment = next((p for p in DB["payments"] if p["ref"] == reg_id), None)
    if payment:
        payment.update({"status": "succeeded", "amount": amount, "paidAt": int(time.time() * 1000), "source": source})
    _save(DB)
    # Email the ticket for security (attendee keeps a copy in their inbox).
    if source != "demo" and SMTP_USER and SMTP_PASS:
        if REDIS_URL and REDIS_TOKEN:  # serverless (Vercel): background threads freeze — send inline
            send_ticket_email(dict(reg))
        else:
            threading.Thread(target=send_ticket_email, args=(dict(reg),), daemon=True).start()
    return reg


def send_ticket_email(reg: dict):
    """Best-effort ticket email with the entry QR attached. Never raises into the request."""
    try:
        # On Vercel PUBLIC_BASE_URL may be unset — use the auto-provided deployment domain.
        vurl = os.environ.get("VERCEL_PROJECT_PRODUCTION_URL") or os.environ.get("VERCEL_URL") or ""
        if vurl and not vurl.startswith("http"):
            vurl = "https://" + vurl
        base = PUBLIC_BASE_URL or vurl or os.environ.get('PA_BASE_URL', '') or f"http://localhost:{PORT}"
        ticket_url = base + f"/pay/{reg['id']}"
        # The DB row has no QR attached — generate the entry-ticket QR fresh for this email.
        reg["ticketQrDataUrl"] = make_qr(ticket_url)
        msg = EmailMessage()
        msg["Subject"] = f"Your Aletheia ticket {reg['id']}"
        msg["From"] = SMTP_FROM or SMTP_USER
        msg["To"] = reg["email"]
        msg.set_content(
            f"Hi {reg['name']},\n\n"
            f"Your payment of Rs.{reg['amountPaid']} for Aletheia is confirmed.\n\n"
            f"Ticket ID: {reg['id']}\n"
            f"Persons: {reg['persons']}\n"
            f"Reopen / re-show your entry ticket QR any time:\n{ticket_url}\n\n"
            f"Show the QR at the entrance - each scan counts one person.\n"
            f"Your door PIN: {reg.get('pin', '----')} (keep it private - we will ask for it at the door)\n"
            f"Saturday, 21 November 2026, 6:30 to 8:00 PM (~1.5 hours)\n"
            f"Venue: to be announced, Bangalore (we will message you)\n"
            f"See you there.\n"
        )
        png_b64 = reg.get("ticketQrDataUrl", "").split("base64,", 1)[-1]
        if png_b64:
            msg.add_attachment(base64.b64decode(png_b64), maintype="image", subtype="png", filename=f"ticket-{reg['id']}.png")
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
        live = find_reg(reg["id"])
        if live:
            live["emailStatus"] = "sent"
            live["emailedAt"] = int(time.time() * 1000)
            _save(DB)
        app.logger.info("Ticket email sent to %s for %s", reg["email"], reg["id"])
    except Exception as e:
        live = find_reg(reg.get("id", ""))
        if live:
            live["emailStatus"] = "failed"
            live["emailError"] = str(e)[:140]
            _save(DB)
        app.logger.error("Ticket email failed for %s: %s", reg.get("id"), e)


# ---------------- Razorpay REST client (stdlib only) ----------------

def _rzp_request(method: str, path: str, body: dict | None = None, timeout: int = 20) -> dict:
    url = RZP_API + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    auth = base64.b64encode(f"{RZP_KEY_ID}:{RZP_KEY_SECRET}".encode()).decode()
    req.add_header("Authorization", "Basic " + auth)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def rzp_create_payment_link(reg: dict) -> dict:
    """Create a Razorpay Payment Link for this registration; returns the link entity."""
    persons = reg["persons"]
    return _rzp_request("POST", "/payment_links", {
        "amount": reg["expectedAmount"] * 100,  # paise
        "currency": CURRENCY,
        "accept_partial": False,
        "reference_id": reg["id"],
        "description": f"Aletheia · {persons} person(s) · Bangalore · ~1.5 hours",
        "customer": {"name": reg["name"], "email": reg["email"]},
        "notify": {"sms": False, "email": False},
        "reminder_enable": True,
        "notes": {"registrationId": reg["id"]},
    })


def rzp_fetch_payment_link(link_id: str) -> dict:
    return _rzp_request("GET", f"/payment_links/{link_id}")


def rzp_link_paid_amount(link: dict) -> float:
    """Best-effort rupee amount from a paid payment-link entity."""
    payments = link.get("payments") or {}
    for p in payments.values():
        if p.get("status") == "captured":
            return p.get("amount", 0) / 100
    return link.get("amount", 0) / 100


# ---------------- Stripe Checkout (stdlib only) ----------------

def stripe_create_checkout(reg: dict, base: str) -> dict:
    """Create a Stripe Checkout Session for Rs.69 x persons; returns the session."""
    data = {
        "mode": "payment",
        "success_url": f"{base}/pay/{reg['id']}?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{base}/pay/{reg['id']}",
        "client_reference_id": reg["id"],
        "metadata[registrationId]": reg["id"],
        "line_items[0][quantity]": str(reg["persons"]),
        "line_items[0][price_data][currency]": "inr",
        "line_items[0][price_data][unit_amount]": str(PER_PERSON_PRICE * 100),
        "line_items[0][price_data][product_data][name]": f"Aletheia ticket {reg['id']}",
    }
    req = urllib.request.Request("https://api.stripe.com/v1/checkout/sessions",
                                 data=urlencode(data).encode(), method="POST")
    req.add_header("Authorization", "Bearer " + STRIPE_SECRET_KEY)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def stripe_session_paid(session_id: str) -> bool:
    """Server-side truth: did Stripe actually receive the money for this session?"""
    req = urllib.request.Request(f"https://api.stripe.com/v1/checkout/sessions/{session_id}")
    req.add_header("Authorization", "Bearer " + STRIPE_SECRET_KEY)
    with urllib.request.urlopen(req, timeout=20) as resp:
        s = json.loads(resp.read().decode())
    return s.get("payment_status") == "paid"


# ---------------- static pages ----------------

def serve_page(filename):
    """Serve a page file; if the function bundle lacks it (serverless), fall back
    to the statically-served copy at the edge (public/ is a Vercel static dir)."""
    try:
        return send_from_directory(PUBLIC_DIR, filename)
    except Exception:
        return redirect(f"/{filename}")


# On Vercel the friendly page URLs are rewritten to their static files at the
# edge (vercel.json), so these handlers mainly serve the local/dev case.
@app.get("/")
def home():
    return serve_page("start.html")


@app.get("/start")
def start_page():
    return serve_page("start.html")


@app.get("/register")
def register_page():
    return serve_page("index.html")


@app.get("/door")
def door_page():
    return serve_page("door.html")


@app.get("/pay/<reg_id>")
def pay_page(reg_id):
    try:
        return send_from_directory(PUBLIC_DIR, "index.html")
    except Exception:
        # carry the ticket id in ?t= — index.html's JS picks it up and shows the ticket
        return redirect(f"/index.html?t={reg_id}")


@app.get("/admin.html")
def admin_page():
    return send_from_directory(PUBLIC_DIR, "admin.html")



@app.get("/api/qr")
def api_qr():
    """QR a given text/URL — used for the start-page QR."""
    text = request.args.get("text", "")[:800]
    if not text:
        return jsonify({"error": "text required"}), 400
    return jsonify({"qrDataUrl": make_qr(text)})


@app.get("/api/start-qr")
def api_start_qr():
    """QR of the PUBLIC start URL — always the public/tunnel URL, never localhost,
    so phones that scan it land on the real site."""
    url = public_base(request) + "/start"
    return jsonify({"url": url, "qrDataUrl": make_qr(url)})


@app.get("/api/event-info")
def api_event_info():
    """Booking deadline + price, so pages can show countdown / closed state."""
    info = deadline_info() or {"deadlineTs": None, "closed": False, "daysLeft": None}
    return jsonify({**info, "price": PER_PERSON_PRICE, "currency": CURRENCY})


# ---------------- registration + payment ----------------

@app.post("/api/register")
def api_register():
    dl = deadline_info()
    if dl and dl["closed"]:
        return jsonify({"error": "Registrations are closed — the booking deadline has passed. Already-registered tickets remain valid.", "closed": True}), 403
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()[:80]
    email = (body.get("email") or "").strip().lower()[:120]
    upi_name = (body.get("upiName") or "").strip()[:80]  # name shown on their UPI app — improves auto-match
    try:
        persons = max(1, min(20, int(body.get("persons", 1))))
    except (TypeError, ValueError):
        persons = 1

    if not name or not email:
        return jsonify({"error": "Name and email are required."}), 400
    if not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email):
        return jsonify({"error": "Please enter a valid email."}), 400

    _expire_stale_pendings()  # stale pendings must not block dedupe below

    # One pending booking per email: re-registering while a booking is still
    # pending returns the EXISTING ticket instead of piling up duplicates
    # (within 24h — families sharing an email can book next day or ask me).
    for prev in DB["registrations"]:
        if (prev.get("email") == email and prev.get("status") == "pending"
                and int(time.time() * 1000) - prev.get("createdAt", 0) < min(86_400_000, PAYMENT_TIMEOUT_MS)):
            if PAYMENT_MODE != "upi":
                break  # other payment modes manage their own payment state
            payment = next((p for p in DB["payments"] if p["ref"] == prev["id"]), None)
            if not payment:  # self-heal: pending row without a payment record
                payment = {
                    "ref": prev["id"], "upiId": UPI_ID,
                    "upiUri": upi_payment_uri(prev),
                    "amount": prev.get("expectedAmount", PER_PERSON_PRICE),
                    "currency": CURRENCY, "provider": "upi", "status": "pending",
                    "utr": "", "claimedAt": None, "createdAt": int(time.time() * 1000),
                }
                DB["payments"].append(payment)
            if not payment.get("upiUri") or (UPI_ID and payment.get("upiId") != UPI_ID):
                # self-heal: bookings minted before a VPA fix kept QR-deep-linking
                # to the dead old ID (the @ybl "Unable to scan QR" errors) — regenerate.
                payment["upiUri"] = upi_payment_uri(prev)
                payment["upiId"] = UPI_ID
            prev.setdefault("paymentMode", "upi")
            _save(DB)
            uri = payment["upiUri"]
            return jsonify({**prev, "paymentMode": "upi",
                            "qrDataUrl": make_qr(uri), "upiUri": uri, "upiId": UPI_ID,
                            "expiresAt": prev.get("expiresAt") or prev.get("createdAt", 0) + PAYMENT_TIMEOUT_MS,
                            "existing": True, "demoMode": False})

    amount = persons * PER_PERSON_PRICE
    now_ms = int(time.time() * 1000)
    reg = {
        "id": new_id(),
        "name": name,
        "upiName": upi_name,
        "email": email,
        "persons": persons,
        "expectedAmount": amount,
        "amountPaid": 0,
        "status": "pending",
        "checkedIn": 0,
        "createdAt": now_ms,  # ms — JS Date() expects milliseconds
        "expiresAt": now_ms + PAYMENT_TIMEOUT_MS,  # 10-min payment window
    }

    # ---- STRIPE MODE: attendee pays themselves on the same phone; auto-verified ----
    if PAYMENT_MODE == "stripe":
        try:
            session = stripe_create_checkout(reg, public_base(request))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode()[:200]
            except Exception:
                pass
            app.logger.error("Stripe error %s: %s", e.code, detail)
            msg = "Could not start the payment. Please try again."
            if e.code == 401:
                msg = "Stripe rejected the API key - check STRIPE_SECRET_KEY in .env."
            return jsonify({"error": msg}), 502
        except Exception as e:
            app.logger.error("Stripe error: %s", e)
            return jsonify({"error": "Could not reach the payment service. Check your internet and try again."}), 502
        reg["paymentMode"] = "stripe"
        DB["registrations"].append(reg)
        DB["payments"].append({
            "ref": reg["id"],
            "stripeSessionId": session["id"],
            "checkoutUrl": session.get("short_url") or session.get("url", ""),
            "amount": amount,
            "currency": CURRENCY,
            "provider": "stripe",
            "status": "pending",
            "createdAt": int(time.time() * 1000),
        })
        _save(DB)
        return jsonify({**reg, "checkoutUrl": session.get("short_url") or session.get("url", ""), "demoMode": False})

    # ---- UPI MODE: QR deep-links straight to any UPI app, you verify payment manually ----
    if PAYMENT_MODE == "stripe":
        print("STRIPE MODE - attendees pay on the same phone (cards/UPI via Stripe), tickets auto-issue.")
    if PAYMENT_MODE == "upi":
        reg["paymentMode"] = "upi"
        DB["registrations"].append(reg)
        DB["payments"].append({
            "ref": reg["id"],
            "upiId": UPI_ID,
            "upiUri": upi_payment_uri(reg),
            "amount": amount,
            "currency": CURRENCY,
            "provider": "upi",
            "status": "pending",
            "utr": "",
            "claimedAt": None,
            "createdAt": int(time.time() * 1000),
        })
        _save(DB)
        return jsonify({**reg, "qrDataUrl": make_qr(upi_payment_uri(reg)),
                        "upiUri": upi_payment_uri(reg), "upiId": UPI_ID, "demoMode": False})

    if RZP_READY:
        try:
            link = rzp_create_payment_link(reg)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode()[:300]
            except Exception:
                pass
            app.logger.error("Razorpay error %s: %s", e.code, detail)
            msg = "Could not start payment. Please try again."
            if e.code == 401:
                msg = "Razorpay rejected the API keys — check RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET in .env."
            return jsonify({"error": msg}), 502
        except Exception as e:
            app.logger.error("Razorpay error: %s", e)
            return jsonify({"error": "Could not reach Razorpay. Check your internet connection and try again."}), 502

        DB["registrations"].append(reg)
        DB["payments"].append({
            "ref": reg["id"],
            "paymentLinkId": link["id"],
            "shortUrl": link.get("short_url", ""),
            "amount": amount,
            "currency": CURRENCY,
            "provider": "razorpay",
            "status": "pending",
            "createdAt": int(time.time() * 1000),
        })
        _save(DB)

        payment_qr = make_qr(link["short_url"])  # scanning opens the Razorpay payment page
        return jsonify({**reg, "qrDataUrl": payment_qr,
                        "checkoutUrl": link.get("short_url", ""), "demoMode": False})

    # DEMO MODE — QR opens the local pay page; a button simulates payment success.
    reg["demoMode"] = True
    DB["registrations"].append(reg)
    _save(DB)
    pay_url = public_base(request) + f"/pay/{reg['id']}"
    return jsonify({**reg, "qrDataUrl": make_qr(pay_url), "demoMode": True})


@app.get("/api/status/<reg_id>")
def api_status(reg_id):
    reg = find_reg(reg_id)
    if not reg:
        return jsonify({"error": "Registration not found"}), 404

    # Auto-expire: no verified payment within the 10-minute window → expired.
    # (The webhook still revives expired bookings when real money arrives.)
    if reg["status"] == "pending" and int(time.time() * 1000) - reg.get("createdAt", 0) > PAYMENT_TIMEOUT_MS:
        reg["status"] = "expired"
        reg["expiredAt"] = int(time.time() * 1000)
        _save(DB)

    # Expose UPI claim state so the pay page can show the right panel
    payment = next((p for p in DB["payments"] if p["ref"] == reg_id), None)
    extra = {"utr": (payment or {}).get("utr", ""), "paymentMode": (payment or {}).get("provider", ""),
             "upiUri": (payment or {}).get("upiUri", ""), "checkoutUrl": (payment or {}).get("checkoutUrl", "")}

    # Stripe: poll the session - when Stripe confirms the money, ticket issues automatically.
    if PAYMENT_MODE == "stripe" and reg["status"] == "pending":
        payment = next((p for p in DB["payments"] if p["ref"] == reg_id), None)
        if payment and payment.get("stripeSessionId"):
            try:
                if stripe_session_paid(payment["stripeSessionId"]):
                    mark_paid(reg_id, reg["expectedAmount"], "stripe-auto")
                    app.logger.info("Stripe payment confirmed for %s", reg_id)
            except Exception:
                pass
        reg = find_reg(reg_id)

    # Live Razorpay: poll the payment link in case the webhook hasn't arrived yet.
    if RZP_READY and reg["status"] == "pending":
        payment = next((p for p in DB["payments"] if p["ref"] == reg_id), None)
        if payment and payment.get("paymentLinkId"):
            try:
                link = rzp_fetch_payment_link(payment["paymentLinkId"])
                if link.get("status") == "paid":
                    mark_paid(reg_id, rzp_link_paid_amount(link), "razorpay-poll")
            except Exception:
                pass
        reg = find_reg(reg_id)

    # The entry-ticket QR exists ONLY after the admin verifies payment (anti-scam).
    # The PIN is included so the ticket screen can display it next to the QR.
    resp = {**reg, **extra, "qrDataUrl": make_qr(public_base(request) + f"/pay/{reg_id}")}
    if reg["status"] == "paid":
        resp["ticketQrDataUrl"] = make_qr(public_base(request) + f"/checkin/{reg_id}")
    return jsonify(resp)


@app.post("/api/demo/pay/<reg_id>")
def api_demo_pay(reg_id):
    if PAYMENT_MODE != "demo":
        return jsonify({"error": "Demo payments only available in demo mode."}), 403
    if not mark_paid(reg_id, source="demo"):
        return jsonify({"error": "Registration not found"}), 404
    return jsonify({"ok": True})


@app.post("/api/admin/verify")
def api_admin_verify():
    """Admin confirms the money landed in the UPI account."""
    deny = require_admin()
    if deny:
        return deny
    body = request.get_json(silent=True) or {}
    reg = find_reg(body.get("id", ""))
    if not reg:
        return jsonify({"error": "Registration not found"}), 404
    if reg["status"] == "paid":
        return jsonify({"ok": True, "status": "paid"})  # idempotent
    mark_paid(reg["id"], source="upi-verified")
    reg["verifiedAt"] = int(time.time() * 1000)
    reg["verifiedReal"] = True  # admin confirmed this payment is real
    _save(DB)
    return jsonify({"ok": True, "status": "paid"})


@app.post("/api/admin/reject")
def api_admin_reject():
    """Admin can't find the payment — send back to pending so attendee can retry."""
    deny = require_admin()
    if deny:
        return deny
    body = request.get_json(silent=True) or {}
    reg = find_reg(body.get("id", ""))
    if not reg:
        return jsonify({"error": "Registration not found"}), 404
    if reg["status"] == "paid":
        return jsonify({"error": "Already paid — cannot reject."}), 400
    reg["status"] = "pending"
    payment = next((p for p in DB["payments"] if p["ref"] == reg["id"]), None)
    if payment:
        payment["status"] = "pending"
        payment["claimedAt"] = None
    reg["fakeFlags"] = reg.get("fakeFlags", 0) + 1  # admin flagged: no real payment found
    reg["lastRejectedAt"] = int(time.time() * 1000)
    _save(DB)
    return jsonify({"ok": True, "status": "pending"})


@app.post("/api/admin/refund")
def api_admin_refund():
    """Admin returns the money: builds the UPI pay-back link and invalidates the ticket."""
    deny = require_admin()
    if deny:
        return deny
    body = request.get_json(silent=True) or {}
    reg = find_reg(body.get("id", ""))
    if not reg:
        return jsonify({"error": "Registration not found"}), 404
    if reg["status"] != "paid":
        return jsonify({"error": "Only paid tickets can be refunded."}), 400
    vpa = (body.get("vpa") or "").strip().lower()
    if not re.match(r"^[a-z0-9.\-_]{2,50}@[a-z]{2,20}$", vpa):
        return jsonify({"error": "Enter the attendee's UPI ID in the form name@bank."}), 400
    amount = reg["amountPaid"] or reg["expectedAmount"]
    from urllib.parse import urlencode, unquote
    uri = "upi://pay?" + urlencode({
        "pa": vpa, "pn": reg["name"], "am": str(amount), "cu": "INR",
        "tn": f"Refund {reg['id']} Aletheia",
    })
    reg["status"] = "refunded"
    reg["refund"] = {"vpa": vpa, "amount": amount, "at": int(time.time() * 1000)}
    payment = next((p for p in DB["payments"] if p["ref"] == reg["id"]), None)
    if payment:
        payment["status"] = "refunded"
    _save(DB)
    return jsonify({"ok": True, "status": "refunded", "upiUri": uri,
                    "qrDataUrl": make_qr(uri), "amount": amount, "vpa": vpa})


# ---------------- auto-verify webhook (SMS/notification from your phone) ----------------
# Your phone (via MacroDroid/Tasker) POSTs the raw bank/Paytm message here the moment
# money arrives. We only auto-accept when the amount matches a pending booking and the
# sender name (if present) matches too — anything ambiguous stays manual.

def _name_consistent(sender_name: str, reg: dict) -> bool:
    """Soft check: the payer's UPI display name should share at least one word
    (>=2 letters) with the registered name or the self-reported UPI name.
    Names collide across people, so this is a fraud SIGNAL, not an identifier:
    no overlap at all => suspicious => leave for manual review instead of
    auto-accept. Missing/blank sender names never block (can't compare)."""
    if not sender_name:
        return True
    stop = {"miss", "mrs", "mr", "ms", "dr"}
    toks = lambda s: {w for w in re.findall(r"[a-z]+", (s or "").lower()) if len(w) >= 2 and w not in stop}
    sender = toks(sender_name)
    if not sender:
        return True
    known = toks(reg.get("name")) | toks(reg.get("upiName"))
    return bool(sender & known)


def _parse_txn_time(text):
    """Best-effort parse of the transaction time from bank/Paytm message text,
    e.g. 'on 29-09-26 at 5:04PM' / '29-09-2026 17:04:35'. Returns ms or None.
    Used to compare against the booking's creation time (a payment can't belong
    to a booking created after the money arrived)."""
    if not text:
        return None
    m = re.search(
        r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})\D{0,12}?(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([AaPp]\.?[Mm]\.?)?",
        text,
    )
    if not m:
        return None
    d, mo, y, hh, mm, ss, ampm = m.groups()
    try:
        y = int(y)
        if y < 100:
            y += 2000
        hh, mm = int(hh), int(mm)
        ss = int(ss or 0)
        if ampm:
            ampm = ampm.replace(".", "").upper()
            if ampm == "PM" and hh < 12:
                hh += 12
            if ampm == "AM" and hh == 12:
                hh = 0
        return int(datetime(y, int(mo), int(d), hh, mm, ss).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def _log_webhook(accepted, reason, *, amount=None, ticket=None, sender="",
                 upi_ref=None, text="", extra=None):
    """Audit trail for /api/upi-webhook: EVERY payment notification MacroDroid
    forwards — accepted or rejected, matched or not — lands in DB['webhookLog']
    (newest first, newest 200 kept) so the admin panel can show a receipt for
    each decision, even the ones that changed nothing."""
    entry = {
        "ts": int(time.time() * 1000),
        "accepted": bool(accepted),
        "reason": reason,
        "amount": amount,
        "ticket": ticket,
        "sender": sender or None,
        "upiRef": upi_ref or None,
        "snippet": (text or "")[:160],
    }
    if extra:
        entry.update(extra)
    log = DB.setdefault("webhookLog", [])
    log.insert(0, entry)
    del log[200:]
    _save(DB)


@app.post("/api/upi-webhook")
def api_upi_webhook():
    body = request.get_json(silent=True) or {}
    # Key can come in the JSON body OR in the URL (?key=...) — MacroDroid's URL
    # field is easier to paste into than a JSON body, so accept both.
    supplied = body.get("key") or request.args.get("key", "")
    text = (body.get("text") or "").strip()
    amount = body.get("amount")
    name_hint = (body.get("name") or "").strip()
    upi_ref = (body.get("ref") or "").strip()
    if supplied != ADMIN_KEY:
        _log_webhook(False, "unauthorized", text=text, sender=name_hint)
        return jsonify({"error": "unauthorized"}), 401

    # Parse amount out of the message: "Rs 69", "Rs.69.00", "INR 138", "₹69"
    if not amount and text:
        m = re.search(r"(?:rs\.?|inr|₹)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", text, re.I)
        if m:
            amount = m.group(1).replace(",", "")
    try:
        amount = int(float(amount))  # whole rupees — advertised price, no paise games
    except (TypeError, ValueError):
        _log_webhook(False, "unparseable-amount", amount=amount, text=text, sender=name_hint)
        return jsonify({"ok": False, "error": "could not parse amount from message"}), 400
    if amount <= 0:
        _log_webhook(False, "bad-amount", amount=amount, text=text, sender=name_hint)
        return jsonify({"ok": False, "error": "bad amount"}), 400

    # Name is display-only (names collide across UPI users).

    # Idempotency: same SMS forwarded twice must not double-issue; UTR = primary unique key
    if upi_ref:
        dup = next((r for r in DB["registrations"] if r.get("autoRef") == upi_ref), None)
        if dup:
            _log_webhook(False, "duplicate", amount=amount, ticket=dup["id"],
                         sender=name_hint, upi_ref=upi_ref, text=text)
            return jsonify({"ok": True, "accepted": False, "duplicate": True, "ticket": dup["id"]})

    # PRIMARY match: the payer's own payment NOTE — each guest is told to attach
    # their unique ticket code (ME-XXXXXX) while paying. The note rides on the
    # transaction and shows in Paytm + notifications, so it's exact even with
    # flat amounts. Names stay display-only.
    # Sender name: accept it from the JSON field if provided, otherwise pull it
    # out of the message text ("... from Varshini Raj on ..."). Display + soft check.
    if not name_hint and text:
        m2 = re.search(
            r"\bfrom\s+([A-Za-z][A-Za-z .]{1,30}?)" 
            r"(?=\s+(?:on|note|ref|with|via|upi|using|for)\b|[.,]|$)",
            text,
            re.I,
        )
        if m2:
            name_hint = m2.group(1).strip()

    txn_ms = _parse_txn_time(text)
    code = None
    m = re.search(r"\bME-[A-Z0-9]{6}\b", (text or "") + " " + name_hint, re.I)
    if m:
        code = m.group(0).upper()
        by_code = next((r for r in DB["registrations"] if r["id"] == code), None)
        # Settled tickets never re-match: a double-fired notification or a
        # second payment on an already-paid ticket must NOT bleed into a
        # different pending booking (busy-day mis-assignment guard).
        if by_code and by_code["status"] in ("paid", "refunded"):
            _log_webhook(False, "already-paid", amount=amount, ticket=code,
                         sender=name_hint, upi_ref=upi_ref, text=text)
            return jsonify({"ok": True, "accepted": False, "reason": "already-paid",
                            "ticket": code})
        # The note carries exactly one ticket code. If it names no live booking
        # (typo, or ticket was cleaned up), fail safe for manual review instead
        # of silently assigning the money to another guest.
        if by_code is None:
            _log_webhook(False, "unknown-ticket-code", amount=amount, ticket=code,
                         sender=name_hint, upi_ref=upi_ref, text=text)
            return jsonify({"ok": True, "accepted": False, "reason": "unknown-ticket-code",
                            "ticket": code})
        if by_code and by_code["status"] in ("pending", "expired"):
            if by_code.get("expectedAmount") == amount:
                # Time sanity: the transaction must not predate the booking
                # (payments can't belong to tickets created later).
                if txn_ms and by_code.get("createdAt") and txn_ms < by_code["createdAt"] - 120000:
                    _log_webhook(False, "txn-before-booking", amount=amount, ticket=code,
                                 sender=name_hint, upi_ref=upi_ref, text=text,
                                 extra={"txnTime": txn_ms, "bookedAt": by_code["createdAt"]})
                    return jsonify({"ok": True, "accepted": False, "reason": "txn-before-booking",
                                    "ticket": code, "txnTime": txn_ms,
                                    "bookedAt": by_code["createdAt"]})
                # Name-consistency signal: ticket code matched, but if the payer's
                # UPI name shares NO word with the registered/UPI name, don't blind-
                # accept — a copied note+amount would otherwise pass. Manual review.
                if not _name_consistent(name_hint, by_code):
                    _log_webhook(False, "name-mismatch", amount=amount, ticket=code,
                                 sender=name_hint, upi_ref=upi_ref, text=text,
                                 extra={"registered": by_code["name"]})
                    return jsonify({"ok": True, "accepted": False, "reason": "name-mismatch",
                                    "ticket": code, "sender": name_hint,
                                    "registered": by_code["name"]})
                mark_paid(code, amount, source="auto-upi")
                live = find_reg(code)
                if live:
                    live["autoRef"] = upi_ref or None
                    _save(DB)
                _log_webhook(True, "accepted", amount=amount, ticket=code,
                             sender=name_hint, upi_ref=upi_ref, text=text)
                return jsonify({"ok": True, "accepted": True, "ticket": code,
                                "name": by_code["name"], "amount": amount, "emailTo": by_code["email"]})
            _log_webhook(False, "amount-mismatch", amount=amount, ticket=code,
                         sender=name_hint, upi_ref=upi_ref, text=text,
                         extra={"expected": by_code.get("expectedAmount")})
            return jsonify({"ok": True, "accepted": False,
                            "reason": "amount-mismatch", "ticket": code,
                            "expected": by_code.get("expectedAmount"), "amount": amount})

    # FALLBACK: exact advertised amount, only when unambiguous —
    # several pending bookings at the same amount fall back to manual approval.
    cands = [r for r in DB["registrations"] if r["status"] in ("pending", "expired") and
             r.get("expectedAmount", 0) == amount]
    if len(cands) == 1 and txn_ms and cands[0].get("createdAt") and txn_ms < cands[0]["createdAt"] - 120000:
        _log_webhook(False, "txn-before-booking", amount=amount, ticket=cands[0]["id"],
                     sender=name_hint, upi_ref=upi_ref, text=text,
                     extra={"txnTime": txn_ms, "bookedAt": cands[0]["createdAt"]})
        return jsonify({"ok": True, "accepted": False, "reason": "txn-before-booking",
                        "ticket": cands[0]["id"], "txnTime": txn_ms,
                        "bookedAt": cands[0]["createdAt"]})
    if len(cands) == 1 and not _name_consistent(name_hint, cands[0]):
        _log_webhook(False, "name-mismatch", amount=amount, ticket=cands[0]["id"],
                     sender=name_hint, upi_ref=upi_ref, text=text,
                     extra={"registered": cands[0]["name"]})
        return jsonify({"ok": True, "accepted": False, "reason": "name-mismatch",
                        "ticket": cands[0]["id"], "sender": name_hint,
                        "registered": cands[0]["name"]})

    if len(cands) != 1:
        _log_webhook(False, "ambiguous" if len(cands) > 1 else "no-matching-pending-booking",
                     amount=amount, sender=name_hint, upi_ref=upi_ref, text=text,
                     extra={"candidates": [r["id"] for r in cands]})
        return jsonify({"ok": True, "accepted": False,
                        "reason": "ambiguous" if len(cands) > 1 else "no-matching-pending-booking",
                        "amount": amount, "candidates": [r["id"] for r in cands]})

    reg = cands[0]
    mark_paid(reg["id"], amount, source="auto-upi")
    live = find_reg(reg["id"])
    if live:
        live["autoRef"] = upi_ref or None
        _save(DB)
    _log_webhook(True, "accepted", amount=amount, ticket=reg["id"],
                 sender=name_hint, upi_ref=upi_ref, text=text)
    return jsonify({"ok": True, "accepted": True, "ticket": reg["id"],
                    "name": reg["name"], "amount": amount, "emailTo": reg["email"]})


# ---------------- Razorpay webhook ----------------

@app.post("/webhook")
def webhook():
    payload = request.get_data()
    sig = request.headers.get("X-Razorpay-Signature", "")
    try:
        if RZP_WEBHOOK_SECRET:
            expected = hmac.new(RZP_WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, sig):
                return jsonify({"error": "invalid signature"}), 400
        event = json.loads(payload)
    except Exception as e:
        app.logger.error("Webhook error: %s", e)
        return jsonify({"error": str(e)}), 400

    event_type = event.get("event", "")
    entity = (event.get("payload") or {}).get(event_type.split(".")[0], {}).get("entity", {}) if event_type else {}

    if event_type == "payment_link.paid":
        reg_id = (entity.get("notes") or {}).get("registrationId") or entity.get("reference_id")
        if reg_id:
            mark_paid(reg_id, rzp_link_paid_amount(entity), "razorpay-webhook")
            app.logger.info("Payment completed for %s", reg_id)
    elif event_type == "payment.captured":
        reg_id = (entity.get("notes") or {}).get("registrationId")
        if reg_id:
            mark_paid(reg_id, entity.get("amount", 0) / 100, "razorpay-webhook")
            app.logger.info("Payment captured for %s", reg_id)

    return jsonify({"received": True})


# ---------------- admin ----------------

def require_admin():
    key = request.headers.get("x-admin-key") or request.args.get("key")
    if key != ADMIN_KEY:
        return jsonify({"error": "Unauthorized"}), 401
    return None


@app.get("/api/admin/data")
def api_admin_data():
    deny = require_admin()
    if deny:
        return deny
    paid = [r for r in DB["registrations"] if r["status"] == "paid"]
    pending = [r for r in DB["registrations"] if r["status"] == "pending"]
    verifying = [r for r in DB["registrations"] if r["status"] == "verifying"]
    stats = {
        "total": len(DB["registrations"]),
        "paid": len(paid),
        "pending": len(pending),
        "verifying": len(verifying),
        "cancelled": len([r for r in DB["registrations"] if r["status"] == "cancelled"]),
        "refunded": len([r for r in DB["registrations"] if r["status"] == "refunded"]),
        "expired": len([r for r in DB["registrations"] if r["status"] == "expired"]),
        "personsConfirmed": sum(r["persons"] for r in paid),
        "amountCollected": sum(r["amountPaid"] for r in paid),
        "pendingAmount": sum(r["expectedAmount"] for r in pending) + sum(r["expectedAmount"] for r in verifying),
        "verifyingAmount": sum(r["expectedAmount"] for r in verifying),
        "expected": sum(r["expectedAmount"] for r in DB["registrations"]),
    }
    return jsonify({"registrations": DB["registrations"], "stats": stats,
                    "webhookLog": DB.get("webhookLog", [])})


@app.post("/api/admin/checkin")
def api_admin_checkin():
    deny = require_admin()
    if deny:
        return deny
    body = request.get_json(silent=True) or {}
    reg = find_reg(body.get("id", ""))
    if not reg:
        return jsonify({"error": "Registration not found"}), 404
    if reg["status"] == "refunded":
        return jsonify({"error": "This ticket was refunded - entry cancelled."}), 400
    if reg["status"] != "paid":
        return jsonify({"error": "Payment not completed for this ticket."}), 400
    pin = (body.get("pin") or "").strip()
    if pin != reg.get("pin"):
        return jsonify({"error": "Wrong PIN. Ask the attendee for the 4-digit PIN on their ticket.", "pinRequired": True}), 403
    reg["checkedIn"] = min(reg["checkedIn"] + 1, reg["persons"])
    _save(DB)
    return jsonify({"ok": True, "checkedIn": reg["checkedIn"], "persons": reg["persons"]})


@app.get("/checkin/<reg_id>")
def checkin_scan(reg_id):
    """URL encoded in the ticket QR — scanning at the door performs check-in (admin device).
    Requires the attendee's 4-digit PIN (typed by the organizer) so a copied ticket
    link alone can never check anyone in."""
    reg = find_reg(reg_id)
    if not reg:
        return "Ticket not found", 404
    if request.args.get("key") == ADMIN_KEY and reg["status"] == "paid":
        pin = (request.args.get("pin") or "").strip()
        if pin != reg.get("pin"):
            return f"""<h2>🔢 PIN required</h2>
<p>{reg['name']} · {reg['id']}</p>
<form method="get" action="/checkin/{reg_id}">
<input type="hidden" name="key" value="{ADMIN_KEY}" />
<input name="pin" inputmode="numeric" pattern="[0-9]*" maxlength="4" placeholder="4-digit PIN" style="font-size:20px; padding:8px; width:120px; text-align:center;" required />
<button style="font-size:16px; padding:8px 16px;">Check in →</button>
</form>
<p style="color:#888">Ask the attendee for the PIN shown on their ticket.</p>"""
        reg["checkedIn"] = min(reg["checkedIn"] + 1, reg["persons"])
        _save(DB)
        return f"""<h2>✅ Checked in {reg['checkedIn']}/{reg['persons']}</h2>
<p>{reg['name']} · {reg['id']}</p>
<p><a href="/admin.html?key={ADMIN_KEY}">Back to monitor</a> · <a href="/checkin/{reg_id}?key={ADMIN_KEY}">Scan next person</a></p>"""
    if reg["status"] == "refunded":
        return f"<h2>💸 Refunded</h2><p>{reg['name']} · {reg['id']}</p><p>This booking was refunded by the organizer - entry cancelled.</p>"
    if reg["status"] != "paid":
        return f"<h2>⏳ Payment pending</h2><p>{reg['name']} · {reg['id']}</p><p><a href='/pay/{reg_id}'>Complete payment</a></p>"
    # Never leak the admin key on public ticket pages — only link back to the monitor if the key was supplied.
    back_link = f"<p><a href='/admin.html?key={ADMIN_KEY}'>Back to monitor</a></p>" if request.args.get("key") == ADMIN_KEY else ""
    return f"<h2>🎟️ Valid ticket</h2><p>{reg['name']} · {reg['persons']} person(s) · checked in {reg['checkedIn']}/{reg['persons']}</p>{back_link}"


if __name__ == "__main__":
    import logging
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    logging.getLogger("flask.app").setLevel(logging.INFO)  # show ticket-email success/failure lines
    print(f"\nAletheia running at {PUBLIC_BASE_URL or f'http://localhost:{PORT}'}")
    print("   Start QR page:   /start")
    print("   Registration:    /register")
    print(f"   Admin monitor:   /admin.html (key: {ADMIN_KEY})")
    if DEADLINE_TS:
        state = "CLOSED" if time.time() >= DEADLINE_TS else "open"
        print(f"   Booking deadline: {REG_DEADLINE} (registrations {state})")
    if PAYMENT_MODE == "upi":
        print(f"UPI MODE - payments go directly to {UPI_ID}.")
        print("Attendees scan the QR (GPay/PhonePe/Paytm opens with amount pre-filled), pay,")
        print("then tap 'I've paid'. YOU verify each payment in the admin panel (/admin.html).\n")
    elif PAYMENT_MODE == "razorpay":
        mode = "TEST" if RZP_KEY_ID.startswith("rzp_test_") else "LIVE"
        print(f"Razorpay {mode} mode ready - Rs.69 x persons collected via Razorpay payment links (UPI/cards).\n")
    else:
        print("DEMO MODE - set UPI_ID (or Razorpay keys) in .env to collect real payments.")
        print("Payments simulated via 'Simulate payment' button until then.\n")
    app.run(host="0.0.0.0", port=PORT, debug=False)

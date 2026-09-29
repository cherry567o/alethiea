# 🤖 Automatic payment acceptance — setup on YOUR phone (10 minutes, once)

**How it works:** the moment money arrives, Paytm shows a notification on your
phone. A tiny free automation app reads that notification and forwards it to
your website. The site matches the amount (+ name when available) against
pending bookings and accepts **only when it's unambiguous**. Anything unclear
(two people paying ₹69 at the same time, weird formats) is left for you to
accept manually in the admin panel — safety first.

## One-time setup

1. Install **MacroDroid** from Play Store (free plan allows 5 macros — we need 1).
2. Open MacroDroid → **Add Macro** → name it `Aletheia UPI`.
3. **Trigger:** *Notifications → Notification Received*
   - App: **Paytm** (add GPay/bank apps too if you like)
   - Text content: contains `received`
4. **Action:** *Tasks → HTTP Request*
   - Request type: **POST (JSON body)**
   - URL:
     ```
     https://aletheia-event.vercel.app/api/upi-webhook
     ```
   - JSON body (paste exactly):
     ```json
     {"key":"ME0d0992548359092c3e511790588738","text":"{notification}","app":"paytm"}
     ```
     (`{notification}` is a MacroDroid magic-text placeholder — pick it via the
     ✓/{ } button so the app inserts the real notification text.)
5. Save the macro → enable it. Send yourself ₹1 from a friend as a test —
   within seconds the matching booking (₹1 won't match any, so nothing accepts,
   but check the MacroDroid log shows HTTP 200).

## What auto-accepts and what doesn't

| Situation | Result |
|---|---|
| ₹69 arrives, exactly one pending ₹69 booking | ✅ auto-accepted, ticket + email fire |
| ₹138 arrives, two pending ₹138 bookings | ⏸ left manual (admin panel decides) |
| Amount with no matching pending booking | ⏸ ignored (e.g. personal payments) |
| Same message forwarded twice | ✅ second one ignored (no double ticket) |

Every auto-accept shows a **🤖 auto** badge in the admin panel — you can always
see what was automatic vs manual, and refund anything wrong.

## Safety

- The webhook requires your admin key — nobody else can call it.
- It only ever accepts; it never refunds, never cancels.
- Ambiguous cases always fall back to you.

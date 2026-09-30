# 📱 MacroDroid setup — exact taps (10 minutes, one time)

**What this does:** when someone's money hits your Paytm, the notification text
(already containing their ticket code `ME-XXXXXX` — it rides inside the payment
note from the QR) gets forwarded to your website. The site matches the code,
accepts the payment automatically, and the guest's ticket + email fire instantly.
No code typed by guests, nothing for you to click except when something's odd.

---

## Part A — On your PC: allow notifications to reach you

Nothing needed — skip. (This setup is phone-only.)

## Part B — On YOUR phone (the one with Paytm)

1. **Install MacroDroid** from Play Store (free plan = 5 macros, we use 1).

2. Open MacroDroid → **Add Macro** (big + button) → name it `Aletheia UPI`.

3. **➕ Add Trigger** (what starts it):
   - Category: **Notifications**
   - Pick: **Notification Received**
   - App: select **Paytm** (you can also add GPay / your bank app later)
   - Content text: type `received`
   - ✅ tick "Sub-string match" if shown → OK

4. **➕ Add Action** (what it does):
   - Category: **Network / Macros** → **HTTP Request** (or "Web Request")
   - Method: **POST**
   - URL (paste this into the URL box — the key is already inside it):
     ```
     https://aletheia-event.vercel.app/api/upi-webhook?key=ME0d0992548359092c3e511790588738
     ```
   - Body type / Content type: **JSON**
   - Body (paste exactly):
     ```json
     {"text":"{notification}"}
     ```
   - ⚠️ Replace `{notification}` with MacroDroid's real magic-text:
     delete those exact characters, tap the **✓ / { } / tags** button in that
     text field, choose **Notification → Notification Text (Content)**.
     The field must end up sending the actual notification text.
   - (Old style also still works: key inside the JSON body —
     `{"key":"ME0d...","text":"{notification}"}` — either way, not both.)

5. **➕ Add Constraint** (optional but nice):
   - **Volume/Battery** → none needed. Skip.

6. Tap **✔ Save/Back** — the macro is live. MacroDroid will ask for
   Notification access permission → **Allow** (Settings prompt).

## Part C — Test it (2 minutes)

1. From any other UPI app, send yourself ₹1 to your Paytm UPI
   (`7795498451@ptyes`) with note `TEST`.
2. Watch MacroDroid's notification/log — it should fire and show HTTP 200.
3. Nothing will be accepted (₹1 matches no booking) — that's correct behavior.
4. Real test: register on the site, pay the real amount **without editing the
   pre-filled note**, watch the ticket appear in ~5 seconds.

## Part D — What to verify on the payment (already automatic)

✅ The QR **pre-fills the note** with the guest's ticket code — your webhook
matches **code first**, then falls back to amount. Your only job:
- 🟢 unambiguous → nothing to do, ticket auto-issued
- 🟡 ambiguous (two pending same amount, no code) → 1 click in admin
- 🔴 note missing + odd case → check Paytm, accept manually

## Troubleshooting

| Problem | Fix |
|---|---|
| Macro never fires | Notification access not granted: MacroDroid → Settings → Notification access → Allow |
| Fires but HTTP 401 | The key in the JSON body is wrong — copy it again from Part B step 4 |
| Fires but "no-matching-pending-booking" | Money arrived but no pending booking at that amount — did the guest pay the wrong amount? |
| Fires but "ambiguous" | Two+ pending at the same amount and no code in the note → accept manually in admin |
| Double ticket issued | Can't happen — UTR dedupe blocks repeats |

# User guide (for senders and scanners)

You can copy parts of this into your own welcome post. The bot speaks English only. It works in **private chat** - never add it to a group.

## The two roles

| | 🏪 QR Sender | 📷 QR Scanner |
|---|---|---|
| What you do | submit a ChatGPT URL that someone else completes | complete the URLs that arrive during your own time slots |
| Money | deposit USDT, pay per completed task | earn USDT, withdraw whenever you like |
| What you see of the other side | only the scanner's anonymous name, e.g. `user3` | only the URL and a task number |

**You are always anonymous to each other.** The admin knows who you are; the other user never does.

## Getting started (both roles)

1. Send **/start**, choose your role.
2. **Time zone.** Tap **📍 Detect my time zone** - a small window reads your device's time zone (never your location) and sends it back. Confirm the country it shows.
   If that is not possible, just type your country name (e.g. *India*), and pick your time zone if the country has several.
3. Your request goes to the admin. You get a message when it is decided:
   * **✅ Approved. Use /send.** (sender) or **✅ Approved. Choose your slots.** (scanner)
   * **❌ Rejected. Contact admin.** - get in touch with the support contact shown.

Change your time zone any time with **/timezone**. **/cancel** abandons whatever step you are in. **/help** lists the commands for your role.

## QR Sender

### Add funds - /deposit
* **BEP-20 USDT** - the bot shows **your personal address**. Send **USDT on the BNB Smart Chain (BEP-20)** network only - other tokens or networks cannot be recovered.
  Your balance is credited automatically after the network confirmations (about a minute). Below the minimum deposit it is held for the admin.
* **Binance Pay** - send USDT to the Binance Pay ID shown, tap **I've paid**, then send the amount and the **Order ID** Binance shows. You are credited once the payment is verified.

**/balance** shows *available* and *on hold* (money reserved for a running task). **/history** lists recent tasks.

### Send a URL - /send
1. `/send`. You need at least the price of one task (reward + commission) available.
2. Paste the ChatGPT URL within **2 minutes**. Only `https://` links on the allowed domains are accepted.
3. The bot shows **"Active user right now: ✅ user1 - Send URL?"** → **Yes** / **Cancel**.
   If nobody is on duty: *"No scanner is active right now"* - try again later.
4. After **Yes**, the price is put **on hold** and the URL goes to that scanner. They have **2 minutes** to accept or skip.
   * Skipped, no answer, or not finished in time → *"you were not charged"*, the hold is released, use `/send` again.
   * Accepted → *"user1 accepted your URL and is working on it."*
5. When the scanner presses **Done** you receive **"🔔 URL report: ✅ user1 → Done."** with **Confirm** / **Reject** (after a short delay, 2 minutes by default).
   * **Confirm** → the scanner is paid, you are charged. Thank you!
   * **Reject** → a dispute opens, your funds stay on hold, the scanner is asked for a screenshot and an admin decides. You may add **one message** describing what went wrong.
     If you win, everything is returned to your balance; if not, the payment stands.
   * No answer for a long time (default 60 minutes) → the task is **auto-confirmed**.

You can have one task running at a time.

## QR Scanner

### Your name
After approval the admin gives you an **anonymous name** (`user1`, `user2`, …) - that is all senders ever see. **Until you have a name you cannot receive URLs.**

### Your slots - /myslots, /addslot, /removeslot
* Slots are daily windows in **your local time**, e.g. `08-10, 10-12, 14-16`. Add as many as you like - there is no limit.
* After approval the bot opens a picker: tap a block to add ✅ or remove ⬜. Later use **/myslots** (view and edit), **/addslot**, **/removeslot**.
* Confirmation looks like: *"✅ Slots saved: 08-10, 10-12, 14-16 (IST). You can add/remove anytime."*
* Daylight-saving changes are handled for you.

### Before a slot
**10 minutes before** a slot you get *"🔔 Your slot starts in 10 minutes"* with **Ready** / **Busy**. Press **Busy** if you cannot work that time - you will not receive URLs for that slot (once). Ignoring it counts as ready.

### A task
1. **"🔔 New URL! … Respond within 2 minutes."** → **Accept** or **Skip**. No answer = the task expires (small reputation penalty); the sender is not charged.
2. After **Accept** the URL is shown again. Complete it, then tap **Done** (you have 30 minutes by default).
3. The sender confirms → **+0.5 USDT** on your balance. *(Reward and timeouts are set by the admin.)*
4. If the sender **rejects**, you are asked for a **screenshot** of the ChatGPT page showing the completed task - send it as a photo within the time limit (30 minutes by default).
   The screenshot is checked (automatically when enabled, otherwise by an admin) and you are told the outcome.

Only **one URL at a time** is sent to you. There is no backup scanner: if you do not respond, the sender simply tries again.

**Reputation (0-100)** decides who gets the next URL when several scanners are on duty: +1 per confirmed task, small penalties for ignored or unfinished tasks, a bigger one for a lost dispute. See it in **/myslots**.

### Getting paid - /wallet, /withdraw
1. **/wallet** → save a **BEP-20 address** (`0x…`, USDT on BNB Smart Chain) and/or your **Binance Pay ID**. Double-check it: payouts cannot be reversed.
2. **/withdraw** → choose BEP-20 or Binance, enter an amount (or `all`), confirm.
3. An admin reviews and pays; you get **"✅ Withdrawal #… paid"** with the reference. A rejected withdrawal returns the money to your balance.

You can have one withdrawal in progress at a time. Check **/balance** and **/history** any time.

## FAQ

**I did not receive any URL.** You need a name, an active slot in *your* time zone (check `/myslots`), and someone has to be sending. Quiet hours are normal.

**The sender says the task failed but I did it.** Send the screenshot when asked - it is your evidence.

**Can the other side find out who I am?** No. The bot never shows names, usernames, IDs, countries or time zones across roles. Do not share personal details with the other side by other means.

**I deposited but nothing arrived.** Check the network (must be BEP-20/BSC), the token (USDT) and the minimum deposit. Wait for the confirmations; if it still does not show, contact support with the transaction hash.

**I blocked the bot by mistake.** Unblock it and press **Start**; messages sent while it was blocked could not be delivered.

**Who do I contact?** The support contact shown in the rejection / withdrawal messages (set by the admin).

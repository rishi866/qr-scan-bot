# Admin guide

How to run the platform day to day. The panel is at `https://<your domain>`; the bot sends admins Telegram alerts for everything that needs a human.

## Two kinds of "admin"

| | Where | Can do |
|---|---|---|
| **Telegram admins** - numeric IDs in `ADMIN_TELEGRAM_IDS` | Telegram | receive *new approval request*, *dispute*, *withdrawal*, *failed payout* alerts; **Approve / Reject** users with the inline buttons; `/admin` shows open work |
| **Panel admins** - accounts in the web panel | browser | everything below. Create more under *Settings → Security & admins*. |

## Anonymity - what is visible to whom

| Who | Sees |
|---|---|
| **Sender** | only the scanner's anonymous name (`user1`). Never a real name, username, Telegram ID, country or time zone. |
| **Scanner** | only the URL and a task number. Nothing about the sender. |
| **You (admin)** | everything - real names, @usernames, Telegram IDs, countries, time zones, both sides of every task. Treat this as confidential. Every panel action is written to the audit log. |

## A daily routine (10 minutes)

1. **Dashboard** - red banners first: *bot worker offline*, *ledger mismatch*, *BSC node unreachable*.
2. **Users → Pending** - approve or reject new registrations (or tap the buttons in Telegram).
3. **Scanners → Needs a name** - approved scanners without a name cannot receive URLs. *Auto-name* gives the next free `userN`.
4. **Disputes** - anything *needs decision*.
5. **Wallets → Withdrawals / Deposits** - the red/amber badges in the sidebar show the counts.
6. **Slots** - look at the 24-hour strip: hours with **0** scanners mean senders will hear *"No scanner is active right now"*.

## Sections

### Dashboard
Live counters (users, today's tasks and volume, commission, open disputes, withdrawals to handle), a 14-day chart, the scanners that are *inside a slot right now*, and the system status
(bot worker, BEP-20 deposits, automatic payouts, AI check, Binance verification). The sidebar badges refresh every 30 s.

### Users
Search by name, @username, Telegram ID or `userN`; filter by role and status. *Pending* users are listed first.

* **Approve** - the user is told *"✅ Approved. Use /send."* (sender) or *"✅ Approved. Choose your slots."* (scanner, with a button that opens the slot picker). A wallet is created.
* **Reject** - *"❌ Rejected. Contact admin."* (with your *Support contact*). `/start` shows the same message again; the user cannot re-apply on their own, but you can still **Approve** them later from *Users → Rejected*.
* **Suspend / Reinstate** - a suspended user cannot use the bot. Tasks already running are not touched. The note you type is stored on the user.
* The details dialog also shows the wallet, recent tasks, slots, and lets you correct **country / time zone** and keep an **internal note**.
* *"blocked bot"* means the user blocked the bot: nothing can be delivered until they press Start again.

### Scanners
Everything about the people who do the work.

* **Name** - the anonymous name senders see. *Auto-name* picks the next free `userN`; you may type any 2-24 characters (letters, digits, `_`), unique. Renaming is allowed; past transactions keep the name used at the time.
* **Reputation (0-100)** - decides who gets the next URL when several scanners are live (higher first, then whoever waited longest). With *Settings → Automatic reputation* on it moves by itself:
  +1 confirmed task, −1 ignored URL, −2 task not finished, +1 dispute won, −5 dispute lost. You can overwrite it any time.
* **Slots** - the editor works in the **scanner's own local time**; blocks are converted to UTC (daylight-saving aware). Standard blocks follow *Settings → Slot duration* (default 2 h); *Custom window* allows any range, including ones across midnight (`22-02`).
* *Live* means: inside an active slot right now.

### Slots
* The **coverage strip** shows, for each UTC hour of today, how many named, approved scanners cover it - click an hour to see who. Use it to recruit where it is thin.
* The table lists every slot with its local window, time zone, today's UTC window and state: *live now*, *scheduled*, *paused* (you paused it), *scanner suspended*.
* **Pause / Resume** keeps the slot but takes it out of matching; **Delete** removes it (the scanner can add it again); **Edit scanner** opens the editor.
* Scanners get a reminder 10 min before a slot (*Settings → Pre-slot notification*) with **Ready / Busy**. *Busy* excludes them for that one occurrence and you receive a Telegram notice.

### Transactions
* **Transactions** - one row per finished task: sender and scanner (names, countries, time zones at the time), slot, reward, commission, status (*completed*, *disputed*, *refunded*), time. Filter by text, status, date range; totals above the table count completed tasks only.
  **Export CSV** exports exactly the current filter (UTC timestamps; cells that start with `= + - @` are neutralised against spreadsheet-formula injection).
* **Task monitor** - live view of tasks in flight (awaiting the scanner, accepted, done and waiting for the sender, disputed) and history by status, refreshed every 15 s.

### Disputes
A dispute starts when the sender presses **Reject** on a *"URL report"*. Funds (reward + commission) stay **held**; the scanner is asked for a screenshot (*Settings → Dispute proof window*, default 30 min);
the sender may add one free-text note.

1. If the AI check is configured it reads the screenshot. A **clear "completed" ≥ the confidence threshold** pays the scanner automatically (*Settings → AI auto-resolve*); an unclear or negative result is sent to you.
   Automatic *refunds* by AI are **off** by default - a human decides every refund.
2. Open the dispute: left the screenshot (click for full size) and the AI's reading; right the timeline (sent → accepted → Done → rejected), the sender's note, the URL.
3. Decide:
   * **Pay scanner (reject dispute)** - the task counts as completed: scanner +reward, platform +commission, scanner +1 reputation. Sender is told the payment stands.
   * **Refund sender (uphold dispute)** - sender gets reward + commission back; scanner −5 reputation. Both are notified.
4. Add a note (kept internally) and confirm. Decisions are final; they appear in the audit log.

What the AI is: a second pair of eyes that reads **only the image**. It does not know who is right, can be fooled by edited screenshots, and the system prompt tells it to ignore any text inside the image. Keep *auto-refund* off.
If a scanner never uploads a screenshot you decide on what exists (*Awaiting proof* turns into *Needs decision* when the window closes).
If the sender never answers a *URL report*, the task is **auto-confirmed** after *Seller confirmation timeout* (default 60 min; `0` = never).

### Wallets
* **Wallets** - balance (*available*), *held* (reserved for running tasks), earned, spent, deposited, withdrawn per user. Top cards: sender funds, scanner funds (what you owe), platform commission, totals, and the **ledger check**.
  The ledger check proves that every wallet equals the sum of its ledger entries and that money in = money out + balances. If it fails you get a red banner here and on the Dashboard: stop manual payouts and run `python -m app.cli reconcile`.
* **Ledger dialog** - every movement (deposit, reserve, release, payment, reward, commission, withdrawal lock/paid/fee, adjustment) with the balance after it.
* **Manual adjustment** - positive or negative amount with a mandatory reason. Use it for corrections only (a deposit that was missed, a mistaken payout). A removal can never push a balance below zero. It is recorded in the ledger and the audit log.
* **Deposits** - BEP-20 deposits are credited automatically after the confirmations. Here you only see: **Binance claims** ("I paid, order ID X") and **below-minimum** on-chain deposits.
  *Review → Credit to wallet* (you may change the amount to what you actually received) or *Reject*. With a Binance API key configured, claims are verified automatically.
* **Withdrawals** (scanners only) - request → **Approve** → money is sent → **Mark paid** (enter the tx hash / Binance reference; the scanner is notified) or **Reject** (funds go back to the scanner).
  The *Send exactly* amount in the dialog is amount − fee. With automatic payouts on, approved BEP-20 withdrawals up to the limit are sent by the bot; *failed* ones show a **Retry** button - check the chain explorer first so nobody is paid twice.
* **Payout status** - RPC node, last scanned block, deposit address mode (watch-only / mnemonic), hot-wallet balance, Binance / AI status.

### Reports
Date range (UTC) with quick presets. *Daily / monthly* (tasks, rewards, commission, refunds, open disputes), *Countries* (by scanner or sender country), *Scanners*, *Senders*, *Commission* (task commission + withdrawal fees from the ledger, with all-time total). Each has a **CSV** button.

### Settings
Changes apply immediately - no restart. Running tasks keep the price they were created with.

| Group | Setting | Default | Meaning |
|---|---|---|---|
| Money | Scanner payout per task | 0.5 USDT | what the scanner earns |
| | Commission | 0.1 % | of the reward, **charged to the sender on top** (0.0005 USDT at the defaults) |
| | Minimum deposit / withdrawal | 1 / 1 USDT | smaller on-chain deposits wait for your decision |
| | Withdrawal fee | 0 | deducted from the payout, kept by the platform |
| Timeouts | Seller URL input window | 120 s | time after `/send` to paste the URL |
| | Scanner response window | 120 s | Accept / Skip deadline |
| | Delay before the confirm prompt | 120 s | after the scanner taps Done |
| | Seller confirmation timeout | 60 min | then auto-confirm (0 = never) |
| | Scanner task timeout | 30 min | accepted but not Done → cancelled, sender not charged |
| | Dispute proof window | 30 min | screenshot deadline |
| | Pre-slot notification | 10 min | reminder before a slot |
| Slots | Slot duration | 2 h | length of the buttons in the scanner's picker (must divide 24) |
| | Concurrent tasks per scanner / sender | 1 / 1 | |
| Validation | Allowed URL domains | chatgpt.com, chat.openai.com, pay.openai.com, openai.com | senders may only submit `https://` links on these (subdomains included) |
| Payments | Binance Pay ID | - | shown to depositors who pick Binance |
| | Support contact | - | e.g. `@your_support`, shown in rejection messages |
| Reputation & AI | Automatic reputation | on | see Scanners |
| | AI auto-resolve | on | pay the scanner when the AI is confident the task was completed |
| | AI auto-refund | off | refund the sender when the AI is confident the screenshot is *not* proof - **leave off** |
| | AI confidence threshold | 0.85 | |

**Broadcast** - a message to everyone / senders / scanners (approved users who have not blocked the bot). The dialog shows the recipient count and the text; delivery is rate-limited (~25 users/s) and the history shows sent / failed / queued.
**Security & admins** - change your password (signs out other sessions), **enable 2FA** (QR code → 6-digit code), add or delete admins (you cannot delete yourself or the last admin).
**Audit log** - who did what, from which IP, with before/after values for settings. Filter by action text or admin.

## Playbooks

| Situation | What to do |
|---|---|
| **A scanner complains they got no URLs** | Scanners → check *status approved*, a **name**, slots in *their* time zone (Time zone column), not paused, reputation, *Busy* marks. Slots → is the hour covered? |
| **Sender: "no scanner is active"** | Slots → coverage strip for that hour. Recruit, or ask scanners to add slots. |
| **A sender says they were charged for nothing** | Users → sender → Recent tasks → status. *expired / skipped / timed out* are never charged (held funds are released). Wallets → Ledger shows reserve/release pairs. |
| **Dispute with no screenshot** | Wait for the proof window to end, then decide on the evidence; add a note. |
| **Withdrawal stuck in *processing*** | A transfer was broadcast but not confirmed. Look up the tx hash on BscScan. Confirmed → *Mark paid*. Not found after a long time → ask before retrying. Never pay twice. |
| **Withdrawal *failed*** | Read the reason in the Telegram alert / audit log; fix (gas, hot-wallet balance, address); *Retry*, or *Mark paid* if you paid manually, or *Reject* to return the funds. |
| **Deposit not credited** | Wallets → Payout status (RPC ok? last block close to latest?). Wrong network / token → not recoverable by the system. Otherwise `rescan --from-block N` or credit manually with a reason. |
| **User blocked the bot** | Users → *blocked bot* badge. They have to press Start again; nothing else can be done. |
| **Ledger mismatch banner** | Stop approving payouts. Run `python -m app.cli reconcile`, read the problems, compare with the last backup; do not "fix" by hand without understanding the cause. |
| **Lost 2FA device** | Another admin deletes and re-creates the account, or on the server: `create-admin --username NAME --reset --disable-2fa`. |

## Good practice

* Approve people deliberately: the approval request shows name, @username, Telegram ID, role, country and time zone. Ask for context through support if something looks off.
* Keep a second admin account and 2FA on every account; never share logins.
* Keep the float on the server small; settle with the treasury regularly (see [PAYMENTS.md](PAYMENTS.md)).
* Review *Audit log* weekly; export *Transactions* monthly for your books.

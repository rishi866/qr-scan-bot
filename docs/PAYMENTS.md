# Payments: wallets, deposits, withdrawals

All amounts are **USDT**, stored as `NUMERIC(20,8)` and handled as exact decimals (never floats).

## 1. The money model

Every user has a wallet with two numbers:

* **available** - what they can spend (sender) or withdraw (scanner);
* **held** - funds reserved for tasks that are still running.

Everything that changes a number is a **ledger entry** (`ledger_entries`): deposit, reserve (*hold*), release, payment, reward (*earning*), commission, withdrawal lock / unlock / paid / fee, manual adjustment.
The platform itself has an account in the same ledger (rows with `user_id = NULL`) that collects commission and withdrawal fees.
`python -m app.cli reconcile` (and the *ledger check* in the panel) proves that

1. every wallet (available and held) equals the sum of its own ledger entries,
2. across the whole system, all ledger movements except manual adjustments add up to **credited deposits − completed payouts** (money is neither created nor lost by the task logic),
3. users' balances + the platform's balance equal the ledger total.

### Life of a task's money (defaults: reward 0.5, commission 0.1 %)

| Moment | Sender | Scanner | Platform |
|---|---|---|---|
| Sender presses **Yes** → the URL is delivered to the scanner | available −0.5005, **held +0.5005** | | |
| Scanner skips / does not answer / does not finish | held −0.5005, available +0.5005 (released, nothing charged) | | |
| Sender confirms (or auto-confirm) | held −0.5005 (spent) | available **+0.5** | **+0.0005** |
| Sender rejects → dispute, admin pays scanner | same as confirmed | +0.5 | +0.0005 |
| Sender rejects → dispute, admin refunds | held −0.5005, available +0.5005 | - (reputation −5) | - |

The commission is **added on top** of the reward: the scanner always receives the full reward. If you prefer to deduct it from the scanner's payout, that is a code change in
`app/services/settlement.py` (`session_cost`, `pay`) - ask a developer; the setting alone only changes the numbers.

## 2. Deposits (senders)

### 2.1 BEP-20 USDT (automatic)

* Token: Binance-Peg **USDT on BNB Smart Chain**, contract `0x55d398326f99059fF775485246999027B3197955`, 18 decimals (configurable via `TOKEN_CONTRACT`, `TOKEN_DECIMALS`).
* Each sender gets **their own deposit address**, derived from your wallet's *account xpub* (BIP-44 path `m/44'/60'/0'/0/<user_id>`). The address is shown in the bot under `/deposit`.
  Because the index is the user's ID, the whole mapping can be recovered from the seed even if the database is lost.
* The bot's background worker scans `Transfer` events to those addresses. A deposit is credited only after `DEPOSIT_CONFIRMATIONS` (default 15, ≈ 45 s) blocks, which makes chain re-organisations harmless.
  Crediting is idempotent (unique `(method, tx_hash, log_index)`), so restarts and re-scans never double-credit.
* Below *Minimum deposit* → held as **below_min** for your decision (*Wallets → Deposits*). Wrong token or wrong network → not recoverable by the system.
* Use a **dedicated RPC endpoint** (Ankr, QuickNode, NodeReal, …). The public `bsc-dataseed` endpoints rate-limit and sometimes lag. *Wallets → Payout status* shows the RPC state and how far behind the scanner is.

#### Setting up the wallet (once)

```bash
# on a safe, preferably offline machine, inside the project's virtualenv:
python -m app.cli gen-wallet --words 12
```

It prints a **mnemonic** and the **account xpub**.

* Write the mnemonic on paper, store it in two places. It controls every deposit address - **never** put it into chat, e-mail, a screenshot or the repository.
* Put only the **xpub** into `deploy/.env` as `HD_XPUB=`. With a watch-only xpub, a stolen server can *see* addresses but can **not** move funds.
* (Alternatively create the account in a hardware wallet that exposes the account xpub for `m/44'/60'/0'`.)
* The application remembers the xpub it first used and refuses to run with a different one (`The deposit address configuration changed`) so addresses can never silently change.

#### Moving deposits to your treasury (sweeping)

Deposits stay on the per-user addresses until you sweep them. Sweeping needs (a) the mnemonic, (b) a small **gas wallet** holding a little BNB, (c) your treasury address:

```bash
cd /opt/qr-scan-bot/deploy
docker compose run --rm api python -m app.cli sweep --treasury 0xYourTreasury --min 5 --dry-run   # plan only
docker compose run --rm api python -m app.cli sweep --treasury 0xYourTreasury --min 5             # asks for the mnemonic and the gas key (hidden prompts), then "sweep" to confirm
```

The mnemonic and the gas key are typed at the prompt and exist only for the duration of the command - they are not stored. The command tops up each deposit address with exactly the BNB it needs for its transfer, then moves the USDT.
(Setting `HD_MNEMONIC` permanently in `.env` also works but keeps the key on the server - not recommended.)

### 2.2 Binance Pay (semi-automatic)

1. You put your **Binance Pay ID / UID** into *Settings → Payments*.
2. A sender who picks *Binance* in `/deposit` is told to send USDT to that ID, then taps **I've paid**, enters the amount and the **Order ID** Binance shows.
3. That creates a **claim** (`pending`). You get a Telegram alert. In the panel (*Wallets → Deposits*) check the Binance history for an incoming transfer with that reference and amount, then **Credit** (adjust the amount if needed) or **Reject**.
4. *Optional automation*: with `BINANCE_API_KEY/SECRET` the worker looks the order up and credits matching claims by itself.
   **The API key must be read-only** (*Enable Reading* only; never trading or withdrawals) and IP-restricted to your server. `python -m app.cli check` fails loudly if withdrawals are enabled on the key.
   The Binance response formats were implemented from Binance's public documentation and could **not** be tested against a live account during development; anything unexpected falls back to manual confirmation instead of crediting.

## 3. Withdrawals (scanners)

* A scanner saves a payout address with `/wallet` (BEP-20 address, or Binance Pay ID) and requests a payout with `/withdraw` (amount ≥ *Minimum withdrawal*, or `all`). Only **approved scanners** can withdraw, one request at a time.
* The amount is **locked** immediately (available → held) so it cannot be spent twice. You get an alert.
* Status flow: `pending` → **approve** → `approved` → `processing` (automatic only) → `completed`; or `rejected` (funds return) or `failed` (needs a human).

### 3.1 Manual payouts (default)

1. *Wallets → Withdrawals → Approve*.
2. Send exactly the **net** amount (requested − fee) to the shown address from your own wallet or Binance.
3. *Mark paid*, paste the **tx hash / reference**. The scanner is notified, the lock is consumed, the fee goes to the platform.

### 3.2 Automatic BEP-20 payouts (optional)

Enable only when you are comfortable operating a **hot wallet**:

```ini
AUTO_PAYOUT_ENABLED=true
AUTO_PAYOUT_MAX_AMOUNT=50          # bigger payouts stay manual
PAYOUT_PRIVATE_KEY=0x...           # a dedicated wallet: some USDT float + a little BNB for gas
```

Safety rules built into the payer:

* only withdrawals **you already approved**, only BEP-20, only up to `AUTO_PAYOUT_MAX_AMOUNT`;
* the row is committed as `processing` **before** anything is broadcast; if the process dies between broadcast and saving the hash, the row is escalated as `failed` and **never re-sent automatically**;
* failures that provably happened before broadcast (node rejected it, gas too high, wallet empty) return the row to `approved` and alert you once;
* confirmations are awaited before marking `completed`.

Operate it like cash in a till: keep a small float, top it up by hand, watch *Payout status* (hot wallet token and BNB balance), and review every *failed* withdrawal on the chain explorer before pressing **Retry**.
The key lives in `deploy/.env` (mode 600) on the server - anyone who can read that file can spend the float.

## 4. Fees and limits you can tune

| Setting (panel) | Default |
|---|---|
| Scanner payout per task | 0.5 |
| Commission % (of the payout, on top, paid by the sender) | 0.1 |
| Minimum deposit / withdrawal | 1 / 1 |
| Withdrawal fee (kept by the platform) | 0 |
| `DEPOSIT_CONFIRMATIONS`, `AUTO_PAYOUT_MAX_AMOUNT` (environment) | 15 / 50 |

## 5. Reconciliation and incident handling

* Run `python -m app.cli reconcile` after incidents and periodically (cron + mail is a good idea). A healthy system prints *OK*.
* **Never edit balances directly in the database.** Use *Manual adjustment* (ledger + audit trail) so the books keep adding up.
* Missed deposit: *Payout status* → is the scanner behind? `python -m app.cli rescan --from-block <block before the payment>` (idempotent). Still nothing → credit it manually with the tx hash as the reason.
* Double payment suspected: compare the withdrawal's tx hash with the chain explorer; the ledger lock (*withdrawal paid*) exists once per withdrawal.
* Database restore: restore from `./backup.sh` output, then `reconcile`, then `rescan` from the block of the backup so no on-chain deposit is lost.

## 6. Compliance reminder

You are holding customers' funds and moving them between strangers. Depending on where you and your users are, that can fall under money-transmission, e-money, KYC / AML and sanctions rules, and
the services you rely on (Binance, your VPS provider, Telegram, OpenAI) have their own terms. This software gives you approvals, limits, an append-only ledger and audit logs; it is **not legal advice** and does not
decide whether your use is permitted. Take advice for your jurisdiction before you take real money.

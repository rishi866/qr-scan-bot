# Local setup (development & testing)

You need: **Python 3.11**, **PostgreSQL 14+** (16 recommended), **Node.js 22** and a Telegram bot token from [@BotFather](https://t.me/BotFather).
Nothing here needs Docker; for the production stack see [DEPLOYMENT.md](DEPLOYMENT.md).

## 1. Database

```sql
-- psql as a superuser
CREATE ROLE qrbot LOGIN PASSWORD 'qrbot' CREATEDB;
CREATE DATABASE qrbot_dev  OWNER qrbot;   -- the app you click around in
CREATE DATABASE qrbot_test OWNER qrbot;   -- used (and wiped!) by the test suite
```

## 2. Backend

```bash
cd backend
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt          # the dev file adds pytest, ruff and an in-memory EVM (eth-tester + Vyper)
```

Create `backend/.env`:

```ini
ENVIRONMENT=development
SECRET_KEY=dev-secret-dev-secret-dev-secret-dev-secret
DATABASE_URL=postgresql+asyncpg://qrbot:qrbot@127.0.0.1:5432/qrbot_dev
UPLOAD_DIR=./data/uploads
COOKIE_SECURE=false                           # plain http on localhost
TELEGRAM_BOT_TOKEN=123456:ABC...              # from @BotFather
ADMIN_TELEGRAM_IDS=111111111                  # your numeric Telegram id (ask @userinfobot)
```

All variables are documented in [`deploy/.env.example`](../deploy/.env.example). Then:

```bash
python -m app.cli migrate                     # create / upgrade the tables (Alembic)
python -m app.cli create-admin                # first web-panel admin (prompts for a strong password)
python -m app.cli check                       # prints what is configured and what is not
```

Run the two processes (two terminals):

```bash
uvicorn app.api.main:app --reload --port 8000     # admin API   (docs at http://127.0.0.1:8000/api/docs in development)
python -m app.bot.main                            # Telegram bot + background worker
```

> **Only one process may poll a bot token.** If you also run the production bot with the same token you will see `409 Conflict`
> errors - create a second bot with BotFather for development.

### The time-zone Mini App needs HTTPS

Telegram never tells a bot the user's time zone or country. The bot therefore opens a tiny Mini App (`/tz.html`) that reads the
device's time zone and sends it back. Telegram only opens `https://` Mini Apps, so locally you either

* skip it - the bot automatically falls back to *"type your country"* when `PUBLIC_BASE_URL` is empty or not `https://`, or
* expose port 8000 with a tunnel (`cloudflared tunnel --url http://localhost:8000`) and set `PUBLIC_BASE_URL=https://<tunnel-host>` plus
  `ADMIN_STATIC_DIR=../frontend/out` so the API serves `/tz.html`.

## 3. Admin panel

**Option A - serve the built panel from the API (closest to production, no CORS):**

```bash
cd frontend
npm ci
npm run build                                  # static export into frontend/out
# restart the API with:  ADMIN_STATIC_DIR=../frontend/out   → http://127.0.0.1:8000
```

**Option B - hot reload while editing the UI:**

```bash
# backend/.env:   CORS_ORIGINS=http://localhost:3000
cd frontend && NEXT_PUBLIC_API_BASE=http://localhost:8000 npm run dev      # → http://localhost:3000
```

Use `localhost` (not `127.0.0.1`) for both so the login cookie is treated as same-site.

Checks: `npx tsc --noEmit` and `npm run build`.

## 4. Demo data

```bash
python -m app.cli seed-demo             # senders, scanners with slots, 40+ finished tasks, open disputes with screenshots, deposits, withdrawals
python -m app.cli seed-demo --purge     # remove it again
```

Demo users have Telegram IDs ≥ 9 000 000 000. They are not real chats, so if the bot is running, Telegram rejects the messages addressed to them and those outbox
rows end up as *failed* (harmless). The seeder refuses to run when `ENVIRONMENT=production`. It uses the real services, so the ledger it produces reconciles
(`python -m app.cli reconcile`). It does not create a panel login - run `create-admin` first.

## 5. Tests

```bash
cd backend
pytest -q                 # 226 tests, ~40 s
ruff check .
```

The suite uses a **real PostgreSQL** (`qrbot_test`, or whatever `TEST_DATABASE_URL` points to). **It drops and recreates every table in that
database** - never point it at data you care about. What is covered:

| File | Covers |
|---|---|
| `test_timeutil.py` | time zones, DST, wrap-around slots (22-00), country guessing |
| `test_urls_money_settings.py` | URL allow-list, decimal math, typed settings |
| `test_sessions.py` | task state machine, holds / releases / payments, concurrency, timers, ledger conservation |
| `test_disputes.py`, `test_ai_proofs.py` | disputes, proof upload, AI verdict policy (with a fake model) |
| `test_chain.py` | BEP-20 deposits and payouts against an in-memory chain with a real ERC-20 contract: confirmations, re-orgs, idempotency, sweeping |
| `test_scheduler.py` | all timers, outbox delivery, retries |
| `test_bot_e2e.py` | the real python-telegram-bot application against a simulated Telegram: complete journeys of sender, scanner and admin, **including the anonymity assertions** |
| `test_api_*.py` | authentication, 2FA, lockout, CSRF, security headers, every admin endpoint |
| `test_cli.py` | first-admin creation, password rules, 2FA recovery |

## 6. Everyday commands

| Command | Purpose |
|---|---|
| `python -m app.cli migrate` | apply migrations |
| `python -m app.cli create-admin [--username u] [--reset [--disable-2fa]]` | create a panel admin / reset a password (and switch 2FA off after a lost authenticator) |
| `python -m app.cli gen-wallet [--words 12\|24]` | generate the deposit HD wallet (prints the **xpub** for `.env` and the mnemonic to store **offline**) |
| `python -m app.cli check` | configuration report (database, bot, RPC, deposits, payouts, Binance, AI) |
| `python -m app.cli reconcile` | verify that every wallet equals the sum of its ledger entries and that the ledger matches deposits / withdrawals |
| `python -m app.cli rescan --from-block N` | re-read the chain from block N (idempotent) |
| `python -m app.cli sweep --dry-run` | plan moving deposit-address funds to your treasury wallet |
| `alembic revision --autogenerate -m "..."` | create a migration after changing `app/models.py` (run `alembic check` afterwards) |

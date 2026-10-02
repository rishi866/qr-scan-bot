# QR Exchange - anonymous URL exchange on Telegram

A Telegram bot plus a web admin panel for an **anonymous exchange platform**:

* a **QR Sender** (seller) submits a ChatGPT checkout URL and pays a small fee per completed task,
* a **QR Scanner** works through it during the time slots they chose and earns USDT,
* the sender confirms (or rejects → dispute), the money moves, and **neither side ever learns who the other is**,
* an **admin** approves every user, names every scanner (`user1`, `user2`, …), resolves disputes and handles payouts in the web panel.

```
 Telegram users ──► Telegram Bot (python-telegram-bot) ─┐
                                                        ├──► PostgreSQL  ◄── FastAPI admin API ◄── Admin panel (Next.js)
 BNB Smart Chain ──► deposit watcher / payouts ─────────┘         ▲
 Binance (optional) · Anthropic (optional, dispute screenshots) ──┘
```

| | |
|---|---|
| **Bot** (English) | onboarding with time-zone detection, admin approval, slots, `/send`, accept / skip / done, confirm / reject, disputes with screenshot proof, wallet, deposits, withdrawals |
| **Admin panel** | dashboard · users · scanners & names · slots (UTC coverage) · transactions (+CSV) · disputes (proof viewer) · wallets, deposits, withdrawals, payout status · reports · settings, broadcast, 2FA, audit log |
| **Money** | USDT on BNB Smart Chain (BEP-20) and Binance Pay; double-entry-style ledger that is reconciled against wallets; commission 0.1 % (configurable) |
| **Stack** | FastAPI · python-telegram-bot · PostgreSQL · SQLAlchemy/Alembic · Web3.py · pytz · Next.js + Tailwind + Chart.js · Docker + Caddy |

## Documentation

| Guide | Read it when |
|---|---|
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | you want it running on a **Hostinger VPS** (or any Ubuntu server) |
| [docs/SETUP.md](docs/SETUP.md) | you want to develop / test on your own machine |
| [docs/ADMIN_GUIDE.md](docs/ADMIN_GUIDE.md) | you run the platform day to day |
| [docs/USER_GUIDE.md](docs/USER_GUIDE.md) | you explain the bot to senders and scanners |
| [docs/PAYMENTS.md](docs/PAYMENTS.md) | deposits, withdrawals, wallets, keys and what can go wrong |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | you want to understand or change the code |

## Quick start (production, ~15 minutes)

```bash
# on a fresh Ubuntu 22.04/24.04 VPS, after pointing a DNS A record at it:
git clone <this repository> /opt/qr-scan-bot
cd /opt/qr-scan-bot/deploy
sudo ./install_vps.sh        # Docker, firewall, secrets, build, start, first admin
```

Then open `https://<your domain>`, sign in, enable 2FA and follow [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for the BotFather
setup and for turning on payments.

## Quick start (local development)

```bash
cd backend && python3.11 -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt
cp ../deploy/.env.example .env      # set ENVIRONMENT=development, SECRET_KEY, DATABASE_URL, TELEGRAM_BOT_TOKEN ...
python -m app.cli migrate && python -m app.cli create-admin
uvicorn app.api.main:app --reload   # API  → http://127.0.0.1:8000/api/docs
python -m app.bot.main              # bot
cd ../frontend && npm ci && npm run dev   # panel → http://localhost:3000 (set NEXT_PUBLIC_API_BASE=http://127.0.0.1:8000)
```

Details, the test database and demo data are in [docs/SETUP.md](docs/SETUP.md).

## Repository layout

```
backend/    FastAPI API, Telegram bot, services, blockchain layer, CLI, Alembic migrations, tests
frontend/   Next.js admin panel (static export) and the time-zone Mini App (public/tz.html)
deploy/     docker-compose.yml, Caddyfile, .env.example, install / update / backup / restore scripts, systemd units
docs/       guides
```

## Things you must decide before launch

* **Who bears the commission.** Default: the sender pays *reward + commission* (0.5 + 0.0005 USDT) and the scanner receives the full reward.
  Change the numbers in *Settings → Money*; no restart needed.
* **How payouts happen.** Default: manual - an admin sends the money and presses *Mark paid*. Automatic BEP-20 payouts exist but are **off** until you
  configure a hot wallet ([docs/PAYMENTS.md](docs/PAYMENTS.md)).
* **How disputes are decided.** Default: if the optional AI check is configured and is confident that the screenshot shows a completed task, the scanner is paid
  automatically; refunds are always decided by a human. Both switches are in *Settings → Reputation & AI*.
* **Legal and platform rules.** You operate a service that holds and moves other people's money and relays third-party checkout links. Make sure that is
  permitted where you and your users are (money-transmission / KYC / AML rules) and by the terms of the services involved (OpenAI, Telegram, Binance, your VPS provider).
  The software gives you approvals, audit logs and limits; it does not make that judgement for you.

## Tests

```bash
cd backend && pytest -q            # 226 tests against a real PostgreSQL + an in-memory EVM chain + a simulated Telegram
cd frontend && npx tsc --noEmit && npm run build
```

CI (GitHub Actions) runs the same on every push.

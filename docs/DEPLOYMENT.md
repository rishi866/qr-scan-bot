# Deploying on a Hostinger VPS

This guide takes you from "I have nothing" to a running bot and panel on **one VPS**. The same steps work on any Ubuntu server.

> **Status of the deployment files.** What was verified by the authors: `docker compose config` accepts the compose file, the shell scripts pass `bash -n` and their
> `.env` helper functions were exercised in isolation, and the backend image was *simulated* - a clean virtualenv with only `requirements.txt`, then `migrate`, the bot's handler registration and the API (production mode,
> `/api/health`, docs disabled) started from only the files the Dockerfile copies. What was **not** possible: building the images (no Docker daemon was available) and running the stack on a real VPS with a real domain.
> Do the first install on a throw-away server (or on the real one before any user exists) and read the output of every step.

## 0. What you need

| Thing | Notes |
|---|---|
| **Hostinger VPS** | *KVM 2* (2 vCPU / 8 GB) is comfortable; *KVM 1* (1 vCPU / 4 GB) works - the installer adds swap for the one-time panel build. OS template: **Ubuntu 24.04 LTS** (the "Ubuntu 24.04 with Docker" template also works). |
| **A domain** | e.g. `admin.example.com`. Needed for HTTPS (Let's Encrypt) and for the time-zone Mini App, which Telegram only opens over HTTPS. |
| **Telegram bot token** | from [@BotFather](https://t.me/BotFather) → `/newbot`. |
| **Your numeric Telegram ID** | from [@userinfobot](https://t.me/userinfobot). Everybody listed in `ADMIN_TELEGRAM_IDS` receives approval requests and alerts. |
| *(later)* a BSC RPC endpoint, an HD wallet xpub, optional Binance / Anthropic keys | see [PAYMENTS.md](PAYMENTS.md). Not needed to start. |

## 1. Create the server (hPanel)

1. *VPS → Add / Manage → Operating system*: choose **Ubuntu 24.04 64-bit** and set a root password or paste your **SSH public key** (a key is much safer).
2. Note the server's **IPv4 address**.
3. *(Recommended)* turn on Hostinger's weekly **backups / snapshots** for the VPS - in addition to the database backups below.
4. *(Optional)* Hostinger's firewall in hPanel can mirror the rules the installer sets with `ufw`: allow TCP 22, 80, 443 and UDP 443, nothing else.

> Menu names in hPanel change from time to time; what matters is: Ubuntu 24.04, root access, the IPv4 address.

## 2. Point the domain at the server

At your DNS host (Hostinger → *Domains → DNS / Nameservers*, or your registrar) add:

| Type | Name | Value | TTL |
|---|---|---|---|
| `A` | `admin` (→ `admin.example.com`) | the VPS IPv4 | 300 |

Check from your computer: `dig +short admin.example.com` must print the VPS address. **Do this before the install**: Caddy asks Let's Encrypt for the certificate
on the first start and Let's Encrypt must be able to reach the server by that name.

## 3. First login and basic hardening

```bash
ssh root@<VPS-IP>
adduser deploy && usermod -aG sudo deploy          # a normal sudo user
rsync --archive --chown=deploy:deploy ~/.ssh /home/deploy   # reuse your SSH key
```

Open a **second** terminal and check `ssh deploy@<VPS-IP>` works, then optionally disable root / password SSH logins
(`PermitRootLogin no`, `PasswordAuthentication no` in `/etc/ssh/sshd_config`, then `systemctl reload ssh`). The installer enables `ufw` (SSH, 80, 443)
and `fail2ban`, and `unattended-upgrades` keeps the OS patched.

## 4. Install

```bash
sudo mkdir -p /opt/qr-scan-bot && sudo chown deploy:deploy /opt/qr-scan-bot
git clone <URL of this repository> /opt/qr-scan-bot
cd /opt/qr-scan-bot/deploy
sudo ./install_vps.sh
```

The script (safe to re-run):

1. installs Docker from Docker's official apt repository, `ufw`, `fail2ban`, `unattended-upgrades`;
2. adds a 2 GB swap file when the machine has < 3 GB RAM;
3. opens only SSH / 80 / 443;
4. asks for the domain, an e-mail for Let's Encrypt, the bot token (hidden input) and the admin Telegram ID(s); generates `SECRET_KEY` and the database password;
   writes everything to `deploy/.env` (mode 600);
5. `docker compose up -d --build` - PostgreSQL, one-shot migration, API, bot, and Caddy with the compiled panel;
6. waits for the API to be healthy, asks you to create the **first panel admin** (strong password, 12+ characters) and prints `python -m app.cli check`.

Prefer to do it by hand? `cp deploy/.env.example deploy/.env`, edit it, then `cd deploy && docker compose up -d --build` and
`docker compose run --rm api python -m app.cli create-admin`.

### Verify

```bash
cd /opt/qr-scan-bot/deploy
docker compose ps                      # db healthy, api healthy, bot / web running, migrate exited (0)
docker compose logs --tail=50 bot      # "bot @yourbot started", "starting long polling"
curl -s https://admin.example.com/api/health      # {"status":"ok","database":true,...}
```

Open `https://admin.example.com`, sign in, and look at the **Dashboard**: *Bot worker: online* means the bot process is running and talking to the database.

## 5. First steps in the panel and the bot

1. **Settings → Security**: turn on **2FA** and create a second admin account (so you can never lock yourself out).
2. **Settings → General**: review the defaults (reward 0.5 USDT, commission 0.1 %, timeouts, slot length, allowed URL domains, support contact).
3. In Telegram open your bot and send `/start` as a test sender, pick your country (or let the Mini App detect it). The admin(s) get an
   *"🆕 New approval request"* message with **Approve / Reject** buttons - or use *Users → Pending*.
4. Register a second Telegram account (or a friend) as a scanner, approve it, then **Scanners → Auto-name** to give it `user1`. The scanner picks slots with `/myslots`.
5. Top up the test sender's wallet (*Wallets → Ledger → Manual adjustment*, reason "test") and run a full `/send` → accept → done → confirm cycle.

## 6. BotFather polish (optional)

* `/setdescription`, `/setabouttext`, `/setuserpic` - what people see before pressing Start.
* `/setcommands` - paste:

```
start - Start / register
send - Submit a ChatGPT URL (QR Sender)
deposit - Add funds (QR Sender)
myslots - Your time slots (QR Scanner)
addslot - Add slots (QR Scanner)
removeslot - Remove slots (QR Scanner)
balance - Your wallet
withdraw - Cash out (QR Scanner)
wallet - Save payout addresses (QR Scanner)
history - Recent tasks
timezone - Change time zone
cancel - Cancel the current step
help - Help
```

* Do **not** add the bot to groups; it only answers private chats.

## 7. Turn on payments

Nothing in this section is needed to test the platform; do it before real money flows. Full detail: [PAYMENTS.md](PAYMENTS.md).

1. **BEP-20 deposits.** On a *safe* machine run `python -m app.cli gen-wallet` (or create an account in a hardware wallet and export its xpub), write the **mnemonic on paper**, and put
   only the **xpub** into `deploy/.env` (`HD_XPUB=`). Set `BSC_RPC_URL` to a dedicated RPC (Ankr, QuickNode, NodeReal… - the public endpoint is rate limited).
   `docker compose up -d` and check *Wallets → Payout status*.
2. **Withdrawals** are manual by default: *Wallets → Withdrawals → Approve → (send the money yourself) → Mark paid*.
   Automatic payouts need a hot wallet (`PAYOUT_PRIVATE_KEY`, `AUTO_PAYOUT_ENABLED=true`) - read the warnings first.
3. **Binance** (optional): create a **read-only** API key (never enable withdrawals), set `BINANCE_API_KEY/SECRET`, and put your Binance Pay ID into *Settings → Payments*.
4. **AI screenshot check** (optional): `ANTHROPIC_API_KEY=` in `.env`. Without it every dispute is decided by you.

After editing `.env`: `cd /opt/qr-scan-bot/deploy && docker compose up -d` (containers whose environment changed are re-created).

## 8. Operations

| Task | Command (in `/opt/qr-scan-bot/deploy`) |
|---|---|
| Status | `docker compose ps` |
| Logs | `docker compose logs -f --tail=100 bot` (also `api`, `web`, `db`) |
| Update to the latest code | `./update.sh` (backup → `git pull` → build → migrate → restart) |
| Backup now | `./backup.sh` → `deploy/backups/db-*.sql.gz` + `uploads-*.tar.gz` |
| Restore | `./restore.sh backups/db-YYYYMMDD-HHMMSS.sql.gz` (asks you to type RESTORE) |
| Ledger self-check | `docker compose run --rm api python -m app.cli reconcile` |
| Configuration check | `docker compose run --rm api python -m app.cli check` |
| Reset a panel admin password | `docker compose run --rm api python -m app.cli create-admin --username NAME --reset` (`--disable-2fa` too if the authenticator is lost) |
| Re-read the chain | `docker compose run --rm api python -m app.cli rescan --from-block N` |

### Automatic backups (cron)

```bash
sudo crontab -u deploy -e
17 3 * * *  cd /opt/qr-scan-bot/deploy && ./backup.sh >> backups/backup.log 2>&1
```

A backup that only exists on the server is not a backup: copy `deploy/backups/` off the machine (`rclone`, `scp`, a second VPS, object storage) - **encrypted** (`gpg --symmetric --cipher-algo AES256 file` or `age`), because the dump contains personal data (names, Telegram IDs, payout addresses). Also keep an
offline copy of **`deploy/.env`** - without `SECRET_KEY` the 2FA secrets in a restored database cannot be decrypted. **The deposit mnemonic is never on the server**; store it separately.

### Monitoring

* Add `https://admin.example.com/api/health` to an uptime monitor (UptimeRobot, Better Stack…). It returns **503** when the database is unreachable.
* The Dashboard shows *Bot worker online/offline* (the bot writes a heartbeat every 15 s; it counts as offline after 90 s), the ledger check and open work. Admins also receive Telegram alerts for approvals,
  disputes, withdrawals and failed payouts.
* `docker compose ps` should show everything `Up`; containers restart automatically (`restart: unless-stopped`) after crashes and reboots.

### Updating and rolling back

`./update.sh` backs up first. To go back: `git -C .. checkout <previous commit or tag> && docker compose up -d --build`. Migrations are additive; if a release changed the schema and you must return
to the old schema, restore the backup taken by `update.sh` with `./restore.sh`.

## 9. Without Docker (systemd)

Possible, but Docker is the tested path. Outline:

```bash
sudo apt install -y python3.11 python3.11-venv postgresql caddy nodejs npm    # Node 22 from NodeSource if the distro one is older
sudo useradd --system --create-home --home-dir /var/lib/qrbot qrbot
sudo -u postgres createuser qrbot && sudo -u postgres createdb -O qrbot qrbot
cd /opt/qr-scan-bot/backend && python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
cd ../frontend && npm ci && npm run build                          # → frontend/out
sudo install -d -m 750 -o root -g qrbot /etc/qrbot && sudo cp ../deploy/.env.example /etc/qrbot/qrbot.env   # edit; add DATABASE_URL=postgresql+asyncpg://qrbot:...@127.0.0.1/qrbot, UPLOAD_DIR=/var/lib/qrbot/uploads, PUBLIC_BASE_URL=https://admin.example.com
cd ../backend && set -a && . /etc/qrbot/qrbot.env && set +a && .venv/bin/python -m app.cli migrate
sudo cp ../deploy/systemd/*.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now qrbot-api qrbot-bot
sudo cp ../deploy/systemd/Caddyfile.baremetal /etc/caddy/Caddyfile      # edit domain / e-mail
sudo systemctl reload caddy
```

## 10. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Browser shows a certificate error, Caddy logs `acme` failures | the DNS A record does not point at the server yet, or port 80/443 is blocked (hPanel firewall / ufw). Fix DNS, then `docker compose restart web`. |
| `502 Bad Gateway` on `/api` | the API container is unhealthy: `docker compose logs api migrate db`. Typical: wrong `POSTGRES_PASSWORD` after editing `.env` (the database keeps the *old* password - revert it or reset the volume on a fresh install). |
| Bot does not answer | `docker compose logs bot`. `Unauthorized` → wrong token. `Conflict: terminated by other getUpdates` → the same token is running somewhere else (your laptop?). |
| Dashboard says *bot worker is not reporting* | the `bot` container is stopped or crash-looping - see its logs. |
| Users are not asked "Detect my time zone" | `PUBLIC_BASE_URL` is derived from `DOMAIN` (https). Check `https://<domain>/tz.html` opens; the Mini App needs HTTPS. Manual country entry is the fallback and always works. |
| Deposits are not credited | *Wallets → Payout status*: RPC unreachable? last scanned block far behind? `HD_XPUB` missing? Run `python -m app.cli rescan --from-block <block before the payment>`. |
| *Ledger does not add up* alert | stop manual payouts, run `python -m app.cli reconcile` and read the listed problems; restore from backup if unexplained. |
| Locked out of the panel | 5 wrong passwords lock an account for 15 min. A lost password: `docker compose run --rm api python -m app.cli create-admin --username NAME --reset`. A lost authenticator: add `--disable-2fa` to that command (or let another admin delete and re-create the account), then enrol 2FA again. |
| Out of disk | `docker system df`; old images: `docker image prune -a`; logs are rotated (10 MB × 5); `deploy/backups/` is yours to prune. |

## 11. Security checklist before launch

- [ ] DNS + HTTPS work, `http://` redirects to `https://`.
- [ ] `deploy/.env` is mode 600, backed up offline, never committed.
- [ ] 2FA enabled for every panel admin; the bootstrap password is not left in `.env`.
- [ ] SSH: key-only login, `ufw status` shows only 22/80/443, `fail2ban-client status sshd` works.
- [ ] Only the **xpub** is on the server (no deposit mnemonic). If auto payouts are on: the hot wallet holds a small float and `AUTO_PAYOUT_MAX_AMOUNT` is low.
- [ ] Binance key (if any) is read-only; IP-restrict it to the VPS address in Binance.
- [ ] `./backup.sh` runs from cron and the files are copied off the server; you have tested `./restore.sh` once.
- [ ] You tested the whole journey (register → approve → slot → send → accept → done → confirm; a rejection with screenshot; a withdrawal) with test accounts.

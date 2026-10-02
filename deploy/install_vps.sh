#!/usr/bin/env bash
# One-shot installer for a fresh Ubuntu 22.04 / 24.04 (or Debian 12) VPS, e.g. Hostinger.
#
#   git clone <your repo> /opt/qr-scan-bot && cd /opt/qr-scan-bot/deploy && sudo ./install_vps.sh
#
# What it does (safe to run again - every step checks first):
#   1. installs Docker + the compose plugin, ufw and fail2ban
#   2. adds a swap file on small VPSes (the panel build needs ~1.5 GB of memory)
#   3. firewall: allow SSH, HTTP, HTTPS - nothing else (the database is never published)
#   4. creates deploy/.env (asks for domain, e-mail, bot token, admin IDs and where the database lives - a container on
#      this server or Supabase; generates the secrets)
#   5. builds and starts the stack, creates the first panel admin, runs `app.cli check`
set -euo pipefail

cd "$(dirname "$0")"
[[ $EUID -eq 0 ]] || { echo "Run as root: sudo ./install_vps.sh"; exit 1; }
. /etc/os-release
case "${ID:-}" in ubuntu|debian) ;; *) echo "Only Ubuntu / Debian are supported (found: ${ID:-unknown})"; exit 1 ;; esac

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

# ── 1. packages ──────────────────────────────────────────────────────────────
say "Installing base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg ufw fail2ban unattended-upgrades openssl >/dev/null

if ! command -v docker >/dev/null 2>&1; then
  say "Installing Docker (official apt repository)"
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/${ID}/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
fi
docker compose version >/dev/null 2>&1 || { echo "docker compose plugin is missing"; exit 1; }
systemctl enable --now docker >/dev/null 2>&1 || true

# ── 2. swap on small machines ────────────────────────────────────────────────
mem_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
if (( mem_mb < 3000 )) && [[ -z "$(swapon --show --noheadings)" ]]; then
  say "Only ${mem_mb} MB of RAM: adding a 2 GB swap file"
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# ── 3. firewall ──────────────────────────────────────────────────────────────
say "Configuring the firewall (ssh, http, https)"
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw allow 443/udp >/dev/null
ufw --force enable >/dev/null
systemctl enable --now fail2ban >/dev/null 2>&1 || true

# ── 4. .env ──────────────────────────────────────────────────────────────────
get() { grep -E "^$1=" .env 2>/dev/null | head -n1 | cut -d= -f2- || true; }
put() { # put KEY VALUE - rewrites one line of .env without any shell/sed interpretation of the value
  local tmp; tmp=$(mktemp)
  grep -v -E "^$1=" .env > "$tmp" || true
  printf '%s=%s\n' "$1" "$2" >> "$tmp"
  cat "$tmp" > .env; rm -f "$tmp"
}
drop() { # drop KEY - removes a line from .env
  local tmp; tmp=$(mktemp)
  grep -v -E "^$1=" .env > "$tmp" || true
  cat "$tmp" > .env; rm -f "$tmp"
}
ask() { # ask VAR "prompt" regex [silent]
  local var=$1 prompt=$2 re=$3 silent=${4:-} val=""
  while true; do
    if [[ -n $silent ]]; then read -r -s -p "$prompt: " val; echo; else read -r -p "$prompt: " val; fi
    [[ $val =~ $re ]] && break
    echo "  that does not look right, try again"
  done
  put "$var" "$val"
}

if [[ ! -f .env ]]; then
  say "Creating deploy/.env"
  cp .env.example .env
  chmod 600 .env
  ask DOMAIN "Domain of the admin panel (DNS A record must already point to this server), e.g. admin.example.com" '^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$'
  ask ACME_EMAIL "E-mail for Let's Encrypt notices" '^[^@ ]+@[^@ ]+\.[^@ ]+$'
  ask TELEGRAM_BOT_TOKEN "Telegram bot token from @BotFather (input hidden)" '^[0-9]{6,}:[A-Za-z0-9_-]{30,}$' silent
  ask ADMIN_TELEGRAM_IDS "Numeric Telegram ID(s) of the admin(s), comma separated (ask @userinfobot)" '^[0-9]+(,[0-9]+)*$'
  put SECRET_KEY "$(openssl rand -hex 32)"

  echo
  echo "Where should the database live?"
  echo "  1) a PostgreSQL container on this server (default, simplest)"
  echo "  2) Supabase (managed Postgres) - read docs/SUPABASE.md first and have the session-pooler connection string ready"
  read -r -p "Choose 1 or 2 [1]: " db_choice
  if [[ ${db_choice:-1} == 2 ]]; then
    ask DATABASE_URL "Supabase connection string, session pooler, port 5432 (input hidden)" '^postgres(ql)?(\+asyncpg)?://[^[:space:]$#]+$' silent
    # the application itself adds the asyncpg driver and SSL, and drops options asyncpg does not know
    [[ $(get DATABASE_URL) == *":6543/"* ]] && echo "WARNING: port 6543 is the transaction pooler - it breaks prepared statements. Use port 5432 (session pooler)."
    read -r -p "Schema for the tables [qrbot]: " schema
    put DB_SCHEMA "${schema:-qrbot}"
    put DB_POOL_SIZE 3
    put DB_MAX_OVERFLOW 2
    drop COMPOSE_PROFILES     # no database container
    drop POSTGRES_PASSWORD
  else
    put COMPOSE_PROFILES local-db
    put POSTGRES_PASSWORD "$(openssl rand -hex 24)"
  fi
  echo "Secrets were generated and stored in deploy/.env (mode 600). Back this file up somewhere safe."
else
  say "deploy/.env already exists - keeping it"
  chmod 600 .env
fi
for required in DOMAIN ACME_EMAIL TELEGRAM_BOT_TOKEN ADMIN_TELEGRAM_IDS SECRET_KEY; do
  [[ -n "$(get $required)" ]] || { echo "deploy/.env: $required is empty - edit the file and re-run"; exit 1; }
done
[[ -n "$(get DATABASE_URL)" || -n "$(get POSTGRES_PASSWORD)" ]] || { echo "deploy/.env: set POSTGRES_PASSWORD (own container) or DATABASE_URL (Supabase) - then re-run"; exit 1; }
DOMAIN=$(get DOMAIN)

# ── 5. build & start ─────────────────────────────────────────────────────────
say "Building and starting the stack (the first build takes a few minutes)"
docker compose up -d --build

say "Waiting for the API to become healthy"
for _ in $(seq 1 60); do
  if [[ "$(docker compose ps --format '{{.Health}}' api 2>/dev/null)" == "healthy" ]]; then ok=1; break; fi
  sleep 3
done
[[ ${ok:-0} -eq 1 ]] || { echo "The API did not become healthy. Look at: docker compose logs migrate api"; exit 1; }

if ! docker compose run --rm -T api python -m app.cli admin-exists >/dev/null 2>&1; then
  say "Create the first panel admin (pick a strong password, 12+ characters)"
  docker compose run --rm api python -m app.cli create-admin --username admin
fi

say "Configuration check"
docker compose run --rm -T api python -m app.cli check || true

cat <<DONE

✅ Installed.

   Admin panel : https://${DOMAIN}
   Bot         : open your bot in Telegram and send /start (admins listed in ADMIN_TELEGRAM_IDS get approval requests)
   Logs        : docker compose logs -f bot api web
   Update      : ./update.sh      Backup : ./backup.sh

Next: sign in, turn on 2FA (Settings → Security), then read docs/DEPLOYMENT.md (BotFather, payments).
DONE

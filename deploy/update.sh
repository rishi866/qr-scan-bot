#!/usr/bin/env bash
# Pull the latest code, rebuild, migrate the database and restart. Takes a backup first.
set -euo pipefail
cd "$(dirname "$0")"

echo "==> backup"
./backup.sh
echo "==> pulling"
git -C .. pull --ff-only
echo "==> building"
docker compose build
echo "==> migrating the database"
docker compose run --rm migrate
echo "==> restarting"
docker compose up -d
docker compose ps

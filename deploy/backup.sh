#!/usr/bin/env bash
# Database dump + dispute screenshots into deploy/backups (override with BACKUP_DIR). Keeps KEEP_DAYS days.
# Run it from cron, e.g.:  17 3 * * *  cd /opt/qr-scan-bot/deploy && ./backup.sh >> backups/backup.log 2>&1
# Copy the files OFF this server too (rclone / scp / Hostinger snapshots) - a backup on the same disk is not a backup.
set -euo pipefail
cd "$(dirname "$0")"

BACKUP_DIR="${BACKUP_DIR:-$PWD/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
umask 077
mkdir -p "$BACKUP_DIR"
stamp=$(date -u +%Y%m%d-%H%M%S)

docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --clean --if-exists' | gzip -9 > "$BACKUP_DIR/db-$stamp.sql.gz"
docker compose run --rm -T --no-deps --entrypoint tar api czf - -C /data uploads > "$BACKUP_DIR/uploads-$stamp.tar.gz"

find "$BACKUP_DIR" -type f \( -name 'db-*.sql.gz' -o -name 'uploads-*.tar.gz' \) -mtime +"$KEEP_DAYS" -delete
echo "backup written: $BACKUP_DIR/db-$stamp.sql.gz ($(du -h "$BACKUP_DIR/db-$stamp.sql.gz" | cut -f1))"

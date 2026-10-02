#!/usr/bin/env bash
# Database dump + dispute screenshots into deploy/backups (override with BACKUP_DIR). Keeps KEEP_DAYS days.
# Run it from cron, e.g.:  17 3 * * *  cd /opt/qr-scan-bot/deploy && ./backup.sh >> backups/backup.log 2>&1
# Copy the files OFF this server too, ENCRYPTED (rclone / scp / Hostinger snapshots) - a backup on the same disk is not a backup.
#
# Own PostgreSQL container: dumps the whole database.
# Supabase (no local db container): dumps only this app's schema (DB_SCHEMA) with a pg_dump of the same major version as the
# server (PG_CLIENT_VERSION, default 17). Supabase's own daily backups / point-in-time recovery are in addition to this.
set -euo pipefail
cd "$(dirname "$0")"

BACKUP_DIR="${BACKUP_DIR:-$PWD/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
umask 077
mkdir -p "$BACKUP_DIR"
stamp=$(date -u +%Y%m%d-%H%M%S)
db_out="$BACKUP_DIR/db-$stamp.sql.gz"
files_out="$BACKUP_DIR/uploads-$stamp.tar.gz"
trap 'rm -f "$db_out.part" "$files_out.part"' EXIT   # a failed run leaves no half-written file that looks like a backup

# The mode follows the configuration (is there a db service in this stack?), not what happens to be running: an old db
# container left over after switching to Supabase must never be mistaken for the live database.
if docker compose config --services 2>/dev/null | grep -qx db; then
  docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --clean --if-exists' | gzip -9 > "$db_out.part"
else
  schema=$(docker compose run --rm -T --no-deps api python -c "from app.config import get_settings; print(get_settings().db_schema)" 2>/dev/null | tail -n1)
  [[ $schema =~ ^[a-z_][a-z0-9_]{0,62}$ ]] || { echo "DB_SCHEMA is not set: refusing to dump a whole shared database (docs/SUPABASE.md)"; exit 1; }
  url=$(docker compose run --rm -T --no-deps api python -m app.cli dump-url 2>/dev/null | tail -n1)   # contains the password
  # PGURL travels through the environment, so the password never shows up in `ps`
  PGURL="$url" docker run --rm -e PGURL -v "$PWD/certs:/certs:ro" "postgres:${PG_CLIENT_VERSION:-17}-alpine" sh -c "pg_dump \"\$PGURL\" --schema=$schema --no-owner --no-privileges" | gzip -9 > "$db_out.part"
fi
[[ $(gzip -dc "$db_out.part" | head -c 200 | wc -c) -gt 100 ]] || { echo "the dump is empty - check the database connection"; exit 1; }
mv "$db_out.part" "$db_out"

docker compose run --rm -T --no-deps --entrypoint tar api czf - -C /data uploads > "$files_out.part"
mv "$files_out.part" "$files_out"

find "$BACKUP_DIR" -type f \( -name 'db-*.sql.gz' -o -name 'uploads-*.tar.gz' \) -mtime +"$KEEP_DAYS" -delete
echo "backup written: $db_out ($(du -h "$db_out" | cut -f1))"

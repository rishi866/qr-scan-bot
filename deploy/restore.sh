#!/usr/bin/env bash
# Restore a database dump made by backup.sh. DESTROYS the current database content.
#   ./restore.sh backups/db-20261002-031700.sql.gz
set -euo pipefail
cd "$(dirname "$0")"

file=${1:?usage: ./restore.sh backups/db-YYYYMMDD-HHMMSS.sql.gz}
[[ -f $file ]] || { echo "no such file: $file"; exit 1; }
read -r -p "This replaces the current database with $file. Type RESTORE to continue: " answer
[[ $answer == RESTORE ]] || { echo "aborted"; exit 1; }

docker compose stop api bot web
docker compose up -d db
docker compose exec -T db sh -c 'until pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; do sleep 1; done'
docker compose exec -T db sh -c 'dropdb -U "$POSTGRES_USER" --force --if-exists "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'
gunzip -c "$file" | docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -q'
docker compose run --rm migrate
docker compose up -d
docker compose run --rm -T api python -m app.cli reconcile

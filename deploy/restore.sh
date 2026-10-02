#!/usr/bin/env bash
# Restore a database dump made by backup.sh. DESTROYS the current database content (Supabase: only this app's schema).
#   ./restore.sh backups/db-20261002-031700.sql.gz
set -euo pipefail
cd "$(dirname "$0")"

file=${1:?usage: ./restore.sh backups/db-YYYYMMDD-HHMMSS.sql.gz}
[[ -f $file ]] || { echo "no such file: $file"; exit 1; }
read -r -p "This replaces the current database with $file. Type RESTORE to continue: " answer
[[ $answer == RESTORE ]] || { echo "aborted"; exit 1; }

hosted=0
if ! docker compose config --services 2>/dev/null | grep -qx db; then hosted=1; fi   # no db container in this stack => Supabase

on_error() {
  echo; echo "restore FAILED."
  if [[ $hosted -eq 1 ]]; then
    echo "The schema is reloaded in ONE transaction, so your current data is untouched. Start the stack again with: docker compose up -d"
  else
    echo "The local database was re-created before the failure: fix the problem and run ./restore.sh again (with a good dump)."
  fi
}
trap on_error ERR

docker compose stop api bot web
if [[ $hosted -eq 0 ]]; then
  docker compose up -d db
  docker compose exec -T db sh -c 'until pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; do sleep 1; done'
  docker compose exec -T db sh -c 'dropdb -U "$POSTGRES_USER" --force --if-exists "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'
  gunzip -c "$file" | docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -q -o /dev/null'
else
  schema=$(docker compose run --rm -T --no-deps api python -c "from app.config import get_settings; print(get_settings().db_schema)" 2>/dev/null | tail -n1)
  [[ $schema =~ ^[a-z_][a-z0-9_]{0,62}$ ]] || { echo "DB_SCHEMA is not set: refusing to touch a shared database (docs/SUPABASE.md)"; exit 1; }
  url=$(docker compose run --rm -T --no-deps api python -m app.cli dump-url 2>/dev/null | tail -n1)   # contains the password
  # One transaction: drop the schema and reload it. If anything fails, everything rolls back and the current data stays as it is.
  # (PGURL travels through the environment, so the password never shows up in `ps`.)
  { echo "SET client_min_messages = warning;"; echo "DROP SCHEMA IF EXISTS $schema CASCADE;"; gunzip -c "$file"; } \
    | PGURL="$url" docker run --rm -i -e PGURL -v "$PWD/certs:/certs:ro" "postgres:${PG_CLIENT_VERSION:-17}-alpine" sh -c 'psql "$PGURL" --single-transaction -v ON_ERROR_STOP=1 -q -o /dev/null'
fi
trap - ERR
docker compose run --rm migrate
docker compose up -d
docker compose run --rm -T api python -m app.cli reconcile

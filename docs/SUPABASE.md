# Using Supabase as the database

QR Exchange runs on any PostgreSQL 14+. The stack in `deploy/` ships its own PostgreSQL container, and for most installs that is the right
choice. This page is for running the database on **Supabase** (managed PostgreSQL) instead.

The application uses Supabase purely as a PostgreSQL server: it connects directly with a database role and speaks SQL. It does **not** use
Supabase Auth, Storage, Realtime, Edge Functions or the HTTP "Data API" - and none of those may be able to reach its tables (see section 3).

> Database passwords and connection strings are secrets. They belong in `deploy/.env` on the server (mode 600) and nowhere else - not in a chat,
> an issue, a screenshot or a commit.

## 1. Container or Supabase?

| | PostgreSQL container (default) | Supabase |
|---|---|---|
| Cost | included in the VPS | a paid project (see below) |
| Speed | same machine | one network round trip per query - pick the region closest to the VPS |
| Backups | `backup.sh` + VPS snapshots | Supabase's backups (paid plans), optional point-in-time recovery, **plus** `backup.sh` |
| If the VPS is lost | the data is lost unless you have an off-site backup | the data survives, only the app server has to be rebuilt |
| Personal data lives at | your server | your server **and** Supabase |
| Extra moving parts | none | connection limits, an external service to keep paid and healthy |

Stay with the container unless you want managed backups and a database that outlives the VPS.

**Do not use the Free plan** for this system: free projects are paused after a week of inactivity and are not backed up. The bot would go down with
them, and the ledger holds real money. Check Supabase's pricing page for what a paid project costs today; the workload (a chat bot and an admin panel)
is light, so start with the smallest compute and watch *Reports* in the dashboard.

## 2. A new project, or a schema in an existing one?

| | New dedicated project (recommended) | A schema in a project you already have |
|---|---|---|
| Isolation | own compute, connection limit, backups, network rules, credentials | shares all of them with the other application |
| Restoring a backup / point in time | affects only this app | rolls back **everything** in the project |
| Who can read the ledger | whoever has access to this project | everyone with dashboard or `postgres` access to the shared project |
| Cost | one more paid project | nothing extra |
| Setup | section 4 | section 4, with the dedicated role of 4.2 (not optional) |

Both work: `DB_SCHEMA` keeps this app's tables apart (the app never touches `public`). But if the existing project is a live application, think twice -
a bad migration, a runaway query or a restore then affects both.

## 3. What keeps the data private

Supabase serves the `public` schema - and any schema listed under *Exposed schemas* - over HTTP to the roles `anon`, `authenticated` and
`service_role`. This app never needs that, so three independent locks keep it out of reach:

1. **Its own schema (`DB_SCHEMA=qrbot`).** All tables, including Alembic's `alembic_version`, live there. The app pins every connection's `search_path` to it,
   so it cannot write to `public` by accident, and a missing or misspelled schema fails loudly instead of quietly falling back to `public`.
   Never add the schema to *Exposed schemas*.
2. **Row Level Security on every table.** `python -m app.cli migrate` switches it on (only on Supabase-style databases; it changes nothing elsewhere).
   With RLS on and no policies, the API roles can read nothing. The role the app connects with owns the tables, and owners are exempt from RLS,
   so the app itself is unaffected.
3. **No grants.** Supabase's automatic grants to the API roles cover `public` (and its own system schemas). A schema you create gets none, and the
   API roles have no `USAGE` on it.

`python -m app.cli check` verifies all three and exits non-zero if any is missing; when everything is right it prints
`exposed over HTTP - no ...`. If you forget `DB_SCHEMA`, the tables would land in `public`: `migrate` still switches RLS on, but `check` flags it as a
problem and you should fix it rather than rely on that.

## 4. Set it up

### 4.1 Create or pick the project

* Dashboard → **New project**; choose the region closest to your VPS and save the database password somewhere safe (it can be reset later under
  *Database settings*).
* Note the **project ref** - the short id in the project's URL (`https://supabase.com/dashboard/project/<project-ref>`).

### 4.2 Create a database role for the app (recommended)

In the **SQL editor** (it runs as `postgres`):

```sql
create role qrbot login password 'PASTE-A-LONG-RANDOM-PASSWORD';   -- `openssl rand -hex 24` keeps the connection URL simple
grant create on database postgres to qrbot;                        -- lets the app create its schema (and restore.sh re-create it)
```

The pooler addresses this role as `qrbot.<project-ref>` (section 4.3).

Why a dedicated role: `postgres` is the project's master role. If `deploy/.env` ever leaks, a dedicated role exposes only this app's schema, not the whole
project - and in a shared project it keeps the app away from the other application's tables. (The grant also lets the role create further schemas; it cannot
touch objects it does not own.)

You may use `postgres` directly instead - skip this step and use `postgres.<project-ref>` below. It works, but see the reasoning above.
Whichever you choose, **use the same role from the very first migration on**: it becomes the owner of the schema and its tables
(to switch later, see Troubleshooting).

### 4.3 Build the connection string

Dashboard → **Connect** → **Session pooler** → copy the string. It looks like:

```
postgresql://qrbot.<project-ref>:[YOUR-PASSWORD]@aws-0-<region>.pooler.supabase.com:5432/postgres
```

* **Copy the host from the dashboard** - it differs per project (`aws-0-…`, `aws-1-…`).
* **Session pooler, port 5432.** Not the *transaction* pooler (port 6543): it does not support the prepared statements the database driver uses, and
  queries fail. Not the *direct* host (`db.<project-ref>.supabase.co`) either, unless your server has working IPv6: it is IPv6-only without Supabase's paid
  IPv4 add-on.
* **Percent-encode the password** if it contains anything but letters and digits (`@ : / ? # %` …). A one-liner that does it without echoing:
  `python3 -c 'import getpass,urllib.parse as u; print(u.quote(getpass.getpass(), safe=""))'`
* Write `postgresql+asyncpg://` at the front and `?ssl=require` at the end:

```
DATABASE_URL=postgresql+asyncpg://qrbot.<project-ref>:<encoded-password>@aws-0-<region>.pooler.supabase.com:5432/postgres?ssl=require
```

(The application is forgiving: it also accepts the plain `postgresql://…` string, adds the driver, turns `sslmode=` into `ssl=`, ignores `pgbouncer=true`, and
switches SSL on by itself for `*.supabase.co` / `*.supabase.com` hosts. `ssl=require` encrypts the connection but does not check the server's
certificate - see "Verify the server certificate" below.)

### 4.4 Configure and start

**With the installer** (`sudo ./install_vps.sh`): answer **2** when it asks where the database should live and paste the string (input is hidden).

**By hand**, in `deploy/.env`:

```ini
# delete or comment out these two lines of the container setup:
#   COMPOSE_PROFILES=local-db
#   POSTGRES_PASSWORD=...
DATABASE_URL=postgresql+asyncpg://qrbot.<project-ref>:<encoded-password>@aws-0-<region>.pooler.supabase.com:5432/postgres?ssl=require
DB_SCHEMA=qrbot
DB_POOL_SIZE=3
DB_MAX_OVERFLOW=2
```

```bash
cd /opt/qr-scan-bot/deploy
docker compose up -d --build     # `migrate` creates the schema and tables and switches RLS on; then api, bot and web start
```

Without `COMPOSE_PROFILES=local-db` the `db` container is not part of the stack. (This needs Docker Compose 2.20 or newer; the installer installs a current one.)

### 4.5 Verify

```bash
docker compose run --rm api python -m app.cli check
```

The lines that matter:

```
✅ database - migration 0001, schema 'qrbot'
✅ exposed over HTTP - no - schema 'qrbot' is not reachable through Supabase's Data API and Row Level Security is on
```

plus a ⚠️ *connection pool* note (informational, section 6). Anything marked ❌ names its own fix. Then, in the dashboard, **Advisors → Security Advisor**
should not list your tables as exposed (an informational *RLS enabled, no policy* hint is expected and intended: no policies means the API roles read nothing).

## 5. Hardening checklist

* [ ] **Do not expose the schema.** Leave `qrbot` out of *Exposed schemas*; `check` complains if it is listed.
* [ ] **Switch the Data API off** if this is a dedicated project that nothing else uses: Dashboard → *Data API* integration → turn **Enable Data API** off.
  Then none of the auto-generated REST endpoints respond, whatever the grants. (Leave it on in a shared project that needs it.)
* [ ] **Enforce SSL**: *Database settings → SSL Configuration → Enforce SSL on incoming connections*. The app already connects with SSL. (Applying it
  reboots the database briefly - do it at a quiet moment.)
* [ ] **Network Restrictions** (*Database settings*, bottom of the page; needs the Owner/Admin role): allow only your VPS's public IPv4 address as `x.x.x.x/32`
  (`curl -4 https://ifconfig.me` on the server shows it). The restrictions apply to pooled **and** direct database connections, not to Supabase's HTTPS APIs.
  The form also asks for IPv6 networks - add the VPS's if it has a public IPv6 address. After saving, run `check` again: a wrong address locks the app out
  (you can correct it in the dashboard).
* [ ] **Rotate the database password** if it was ever shown anywhere it should not have been: reset it (for the dedicated role: `alter role qrbot password '…';`),
  update `deploy/.env`, then `docker compose up -d` (a plain `restart` does not re-read `.env`).
* [ ] Keep `deploy/.env` at mode 600 and keep an offline copy. The Supabase `anon` / `service_role` keys are not used by this application at all - never put them in `.env`.

### Verify the server certificate (optional)

`ssl=require` stops passive eavesdropping but does not authenticate the server, so it does not stop an active man-in-the-middle. Supabase recommends `verify-full`
together with SSL enforcement. To use it:

1. Dashboard → *Database settings → SSL Configuration* → download the CA certificate; save it as `deploy/certs/supabase-ca.crt` (it is public, not a secret).
2. In `deploy/.env`: end `DATABASE_URL` with `?ssl=verify-full` and add `PGSSLROOTCERT=/certs/supabase-ca.crt`. (`deploy/certs/` is mounted read-only at `/certs`
   in the api, bot and migrate containers, and in the containers `backup.sh` / `restore.sh` start.)
3. `docker compose up -d`, then `check`.

If the certificate does not verify the connection fails with an error naming the certificate - go back to `ssl=require` rather than guessing. This setup was
tested against a private CA; it has not been exercised against Supabase's own certificates, so test it before relying on it.

## 6. Connections

* The API and the bot each keep their own pool: up to `DB_POOL_SIZE + DB_MAX_OVERFLOW` connections each (3 + 2 = 5), so up to 10 together; `migrate` and CLI commands add
  one for a few seconds.
* In **session mode** every open client connection occupies one pooler slot, and the cap is the **Pool size** under *Database settings → Connection pooling* (per
  database role). Keep `2 × (DB_POOL_SIZE + DB_MAX_OVERFLOW) + 2` below it, with room to spare for the dashboard and any other application using the same role. `check` prints the
  number for your settings.
* Connections are recycled every `DB_POOL_RECYCLE_SECONDS` (default 1800) and checked before use, so a connection dropped by a firewall is replaced instead of failing a request.
* If the logs show `QueuePool limit … reached`, raise `DB_POOL_SIZE` a little - and the pooler's Pool size with it.

## 7. Backups and restore

* **Supabase's own backups** (*Database → Backups*): daily on paid plans; point-in-time recovery is a paid add-on. Restoring one rolls back the whole project's database, not just this app's schema.
* **`./backup.sh`** (cron: [DEPLOYMENT.md](DEPLOYMENT.md#automatic-backups-cron)) dumps this app's schema - and only that - with `pg_dump` run from a `postgres:17-alpine` container, plus the dispute
  screenshots. Use a client of the **same major version as your project** (*Settings → Infrastructure* shows it): a newer `pg_dump` writes statements an older server rejects, an older one refuses
  to dump. Set `PG_CLIENT_VERSION` (default `17`) in the environment if your project differs. The app's role owns its tables, so Row Level Security does not hide any rows from the dump.
* The **screenshots live on the VPS** (the `uploads` volume), not in Supabase - VPS snapshots and `backup.sh` still matter.
* **`./restore.sh backups/db-….sql.gz`** drops and reloads **only the app's schema**, in **one transaction**: if anything fails, everything rolls back and the current data is untouched.
  (The dump re-creates the schema, which is why the role needs the `grant create on database` of 4.2.) Then it migrates, restarts and runs the ledger check.
  Rehearse a restore once before you need it.
* Copy backups off the server **encrypted**, and keep an offline copy of `deploy/.env` (`SECRET_KEY` decrypts the 2FA secrets) - see [DEPLOYMENT.md](DEPLOYMENT.md#automatic-backups-cron).

## 8. Moving an existing installation to Supabase

For a running stack that uses the container and should move. The container's database keeps its tables in `public`; Supabase's must not, so the dump is converted
through a throw-away database. (Rehearse with a copy first. The steps were tested with local PostgreSQL tools; Docker itself was not part of the test. They assume a
PostgreSQL 17 project - for another major version use `postgres:<major>-alpine` below and set `PG_CLIENT_VERSION` accordingly.)

1. Do 4.1 - 4.3, but do not touch `.env` yet.
2. **Stop the app and take the final backup** (nothing may change after it):
   ```bash
   cd /opt/qr-scan-bot/deploy
   docker compose stop api bot web
   ./backup.sh                                     # -> backups/db-<stamp>.sql.gz (whole container database)
   ```
3. **Convert** it (replace `qrbot` with your `DB_SCHEMA`, and the file name with the backup you just made):
   ```bash
   docker run -d --name qr-convert -e POSTGRES_PASSWORD=convert postgres:17-alpine
   until docker exec qr-convert pg_isready -U postgres >/dev/null 2>&1; do sleep 1; done
   gunzip -c backups/db-<stamp>.sql.gz | docker exec -i qr-convert psql -U postgres -v ON_ERROR_STOP=1 -q -o /dev/null
   docker exec qr-convert psql -U postgres -v ON_ERROR_STOP=1 -c 'ALTER SCHEMA public RENAME TO qrbot'
   docker exec qr-convert pg_dump -U postgres --schema=qrbot --no-owner --no-privileges | gzip -9 > backups/db-moved.sql.gz
   docker rm -f qr-convert
   ```
4. **Switch `.env`** to Supabase as in 4.4 (remove `COMPOSE_PROFILES` and `POSTGRES_PASSWORD`, add `DATABASE_URL`, `DB_SCHEMA`, pool sizes). Stop the old container:
   `docker compose --profile local-db stop db` - and keep its `pgdata` volume for a few days as a fallback.
5. **Load the data**: `./restore.sh backups/db-moved.sql.gz` (type `RESTORE`). It creates the schema from the dump, migrates, restarts everything and reconciles the ledger -
   check that its last lines say the ledger adds up.
6. `docker compose run --rm api python -m app.cli check`, open the panel and the bot, then - after a few quiet days - delete the old volume
   (`docker volume ls`, `docker volume rm qrbot_pgdata`).

**Going back:** during the first days the old `pgdata` volume is untouched, so switching `.env` back (restore `COMPOSE_PROFILES=local-db`, `POSTGRES_PASSWORD`, remove `DATABASE_URL` /
`DB_SCHEMA`) and `docker compose up -d` returns to the old database - anything written to Supabase since the switch is not carried back.

## 9. Troubleshooting

| What you see | Cause and fix |
|---|---|
| `FATAL: Tenant or user not found` | The pooler does not know that user/project. The user must be `<role>.<project-ref>` and the host must be *your* project's pooler host - copy the string from *Connect → Session pooler*. |
| `password authentication failed for user …` | Wrong password, a special character that is not percent-encoded, or the password was reset. Fix `deploy/.env`, then `docker compose up -d`. |
| `Max client connections reached` | More connections than the pooler's Pool size allows (section 6): lower `DB_POOL_SIZE` / `DB_MAX_OVERFLOW`, or raise the Pool size. |
| `prepared statement … does not exist` / `… already exists` | You are on port **6543** (transaction pooler). Use the session pooler on **5432**. `check` flags this. |
| `Network is unreachable` / timeout on `db.<project-ref>.supabase.co` | The direct host is IPv6-only. Use the session pooler host. |
| Everything times out after enabling Network Restrictions | The VPS's address is not on the list (IPv4 *and* IPv6 if it has one), or it changed. |
| ❌ `the database role '…' may not create the schema …` | The role lacks `grant create on database postgres to <role>` (4.2) - or ask for the schema to be created for it: `create schema qrbot authorization <role>;`. |
| ❌ `the schema 'qrbot' exists but belongs to '…'` | The first migration ran with another role. Connect with that role - or hand the schema over (below). |
| `check` reports ❌ *exposed over HTTP* | Read the sentence: it names the schema, role or table concerned and the fix. |
| `/api/health` answers 503 | The API cannot reach the database: `docker compose logs migrate api` shows the driver's error - usually one of the rows above. |

**Handing the schema to the dedicated role** (when the first migration ran as `postgres`). In the SQL editor, as `postgres`:

```sql
grant qrbot to postgres;                  -- once: lets postgres hand objects over to qrbot
alter schema qrbot owner to qrbot;
do $$
declare t text;
begin
  for t in select tablename from pg_tables where schemaname = 'qrbot' loop
    execute format('alter table qrbot.%I owner to qrbot', t);
  end loop;
end $$;
```

Then point `DATABASE_URL` at `qrbot.<project-ref>` and `docker compose up -d`. (Sequences owned by the tables follow their owner.)

## 10. Without Docker

Put the same `DATABASE_URL`, `DB_SCHEMA` and pool settings into `/etc/qrbot/qrbot.env` ([DEPLOYMENT.md](DEPLOYMENT.md#9-without-docker-systemd)) and skip installing PostgreSQL;
`python -m app.cli migrate` creates everything. The `After=postgresql.service` line in the systemd units is harmless without a local PostgreSQL. For backups run `pg_dump` yourself:

```bash
pg_dump "$(python -m app.cli dump-url)" --schema=qrbot --no-owner --no-privileges | gzip -9 > db-$(date -u +%Y%m%d-%H%M%S).sql.gz
```

(`dump-url` prints the connection string including the password - do not paste its output anywhere.)

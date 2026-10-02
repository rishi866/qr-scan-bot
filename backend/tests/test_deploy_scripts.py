"""deploy/backup.sh and deploy/restore.sh, run for real against the test server through a stand-in ``docker`` command.

The stand-in executes locally what the scripts would run inside containers (the application's CLI, ``pg_dump``, ``psql``, ``tar``),
so the shell logic - which mode is chosen, quoting, pipelines, the one-transaction restore, traps, leftover files - is exercised.
Docker itself is not. Skipped when bash / psql / pg_dump are missing or ``pg_dump`` is older than the test server.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from app.dbtools import libpq_url

BACKEND = Path(__file__).resolve().parents[1]
DEPLOY = BACKEND.parent / "deploy"
DATABASE_URL = os.environ["DATABASE_URL"]
SCHEMA = "qrbot_x"
ADMIN = "shim_admin"

# Only what backup.sh / restore.sh use. `docker compose config --services` says whether a db container is part of the stack.
SHIM = r"""#!/usr/bin/env bash
echo "$*" >> "$FAKE_DOCKER_LOG"
if [[ $1 == compose ]]; then
  shift
  case "$1" in
    config) if [[ ${FAKE_DB_SERVICE:-0} == 1 ]]; then printf 'api\nbot\ndb\nmigrate\nweb\n'; else printf 'api\nbot\nmigrate\nweb\n'; fi ;;
    stop|up) exit 0 ;;
    exec)  # exec -T db sh -c '<command>': what the local PostgreSQL container would run
      shift; [[ $1 == -T ]] && shift; shift
      exec env POSTGRES_USER="$FAKE_PG_USER" POSTGRES_DB="$FAKE_PG_DB" PGPASSWORD="$FAKE_PG_PASSWORD" PGHOST="$FAKE_PG_HOST" PGPORT="$FAKE_PG_PORT" "$@" ;;
    run)
      shift; entrypoint=""
      while [[ $1 == -* ]]; do case "$1" in --entrypoint) entrypoint=$2; shift 2 ;; *) shift ;; esac; done
      service=$1; shift
      cd "$FAKE_BACKEND" || exit 1
      if [[ -n $entrypoint ]]; then exec "$entrypoint" czf - -C "$FAKE_DATA_DIR" uploads; fi
      if [[ $service == migrate ]]; then exec "$FAKE_PY" -m app.cli migrate; fi
      if [[ $1 == python ]]; then shift; exec "$FAKE_PY" "$@"; fi
      echo "stand-in docker: unsupported run: $service $*" >&2; exit 1 ;;
    *) echo "stand-in docker: unsupported: compose $*" >&2; exit 1 ;;
  esac
  exit $?
elif [[ $1 == run ]]; then   # docker run [--rm] [-i] [-e NAME[=value]] [-v a:b] IMAGE cmd...
  shift
  while [[ $1 == -* ]]; do
    case "$1" in
      -e) if [[ $2 == *=* ]]; then export "$2"; fi; shift 2 ;;   # `-e NAME` takes the value from the caller's environment
      -v) shift 2 ;;
      *) shift ;;
    esac
  done
  shift   # the image
  exec "$@"
fi
echo "stand-in docker: unsupported: $*" >&2; exit 1
"""


def _tools_problem() -> str | None:
    for tool in ("bash", "psql", "pg_dump", "gzip", "tar"):
        if shutil.which(tool) is None:
            return f"{tool} is not installed"
    return None


def psql(url: str, sql: str) -> str:
    done = subprocess.run(["psql", url, "-qAt", "-v", "ON_ERROR_STOP=1", "-c", sql], capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def cli(env: dict[str, str], *args: str, extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "app.cli", *args], cwd=BACKEND, env={**env, **(extra or {})}, capture_output=True, text=True, timeout=120, check=False)


class Rig:
    """A scratch database with the app's schema migrated into it, and a copy of the deploy scripts that talk to it."""

    def __init__(self, tmp: Path, db_name: str) -> None:
        parsed = make_url(DATABASE_URL)
        self.url = parsed.set(database=db_name).render_as_string(hide_password=False)  # SQLAlchemy form, as in .env
        self.libpq = libpq_url(self.url)
        self.work = tmp / "deploy"
        self.work.mkdir()
        for script in ("backup.sh", "restore.sh"):
            shutil.copy(DEPLOY / script, self.work / script)
        (self.work / "certs").mkdir()
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "docker").write_text(SHIM)
        (bin_dir / "docker").chmod(0o755)
        data = tmp / "data"
        (data / "uploads").mkdir(parents=True)
        (data / "uploads" / "proof1.png").write_text("screenshot-bytes")
        self.log = tmp / "docker.log"
        self.log.write_text("")
        self.env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "DATABASE_URL": self.url,
            "DB_SCHEMA": SCHEMA,
            "FAKE_DOCKER_LOG": str(self.log),
            "FAKE_BACKEND": str(BACKEND),
            "FAKE_PY": sys.executable,
            "FAKE_DATA_DIR": str(data),
            "FAKE_PG_USER": parsed.username or "",
            "FAKE_PG_PASSWORD": parsed.password or "",
            "FAKE_PG_HOST": parsed.host or "127.0.0.1",
            "FAKE_PG_PORT": str(parsed.port or 5432),
            "FAKE_PG_DB": db_name,
        }

    def run(self, script: str, *args: str, stdin: str = "", extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", script, *args], cwd=self.work, env={**self.env, **(extra or {})}, input=stdin, capture_output=True, text=True, timeout=180, check=False)

    def sql(self, statement: str) -> str:
        return psql(self.libpq, statement)

    def docker_calls(self) -> str:
        return self.log.read_text()

    @property
    def backups(self) -> Path:
        return self.work / "backups"

    def reset_data(self) -> None:
        """Back to the baseline: one admin, no probe row."""
        self.sql(f"delete from {SCHEMA}.settings where key = 'probe'; delete from {SCHEMA}.admins; insert into {SCHEMA}.admins(username, password_hash) values ('{ADMIN}', 'x')")


@pytest.fixture(scope="module")
def rig(tmp_path_factory):
    problem = _tools_problem()
    if problem:
        pytest.skip(problem)
    admin_url = libpq_url(make_url(DATABASE_URL).set(database="postgres").render_as_string(hide_password=False))
    name = f"qrbot_deploy_{uuid.uuid4().hex[:8]}"
    server = int(psql(admin_url, "show server_version_num")) // 10000
    client = re.search(r"(\d+)", subprocess.run(["pg_dump", "--version"], capture_output=True, text=True, check=False).stdout)
    if client is None or int(client.group(1)) < server:
        pytest.skip(f"pg_dump ({client.group(1) if client else '?'}) is older than the test server ({server})")
    psql(admin_url, f'CREATE DATABASE "{name}"')
    try:
        rig = Rig(tmp_path_factory.mktemp("deploy_scripts"), name)
        migrated = cli(rig.env, "migrate")
        assert migrated.returncode == 0, migrated.stdout + migrated.stderr
        rig.sql(f"insert into {SCHEMA}.admins(username, password_hash) values ('{ADMIN}', 'x')")
        yield rig
    finally:
        psql(admin_url, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def fresh(rig):
    rig.reset_data()
    shutil.rmtree(rig.backups, ignore_errors=True)
    rig.log.write_text("")
    return rig


# ── backup.sh ───────────────────────────────────────────────────────────────


def test_hosted_backup_writes_a_complete_dump_and_the_screenshots(fresh):
    done = fresh.run("backup.sh")
    assert done.returncode == 0, done.stdout + done.stderr
    dumps, archives = list(fresh.backups.glob("db-*.sql.gz")), list(fresh.backups.glob("uploads-*.tar.gz"))
    assert len(dumps) == 1 and len(archives) == 1
    assert not list(fresh.backups.glob("*.part"))
    sql = subprocess.run(["gzip", "-dc", str(dumps[0])], capture_output=True, text=True, check=True).stdout
    assert f"CREATE TABLE {SCHEMA}.admins" in sql and ADMIN in sql  # this app's schema only, with its data
    assert "CREATE TABLE public." not in sql
    listing = subprocess.run(["tar", "tzf", str(archives[0])], capture_output=True, text=True, check=True).stdout
    assert "uploads/proof1.png" in listing
    assert "backup written" in done.stdout
    password = make_url(fresh.url).password or ""
    assert f":{password}@" not in fresh.docker_calls()  # the connection string travels through the environment, not through argv


def test_a_backup_that_cannot_connect_fails_and_leaves_nothing_behind(fresh):
    wrong = make_url(fresh.url).set(password="definitely-wrong").render_as_string(hide_password=False)
    done = fresh.run("backup.sh", extra={"DATABASE_URL": wrong})
    assert done.returncode != 0
    assert not list(fresh.backups.glob("*"))  # neither a finished file nor a .part one


def test_a_shared_database_is_never_dumped_without_a_schema(fresh):
    done = fresh.run("backup.sh", extra={"DB_SCHEMA": ""})
    assert done.returncode != 0 and "DB_SCHEMA is not set" in done.stdout
    assert not list(fresh.backups.glob("db-*"))


def test_with_a_db_service_in_the_stack_the_whole_database_is_dumped(fresh):
    done = fresh.run("backup.sh", extra={"FAKE_DB_SERVICE": "1"})
    assert done.returncode == 0, done.stdout + done.stderr
    sql = subprocess.run(["gzip", "-dc", str(next(fresh.backups.glob("db-*.sql.gz")))], capture_output=True, text=True, check=True).stdout
    assert "DROP TABLE IF EXISTS" in sql  # --clean --if-exists, as before


# ── restore.sh ──────────────────────────────────────────────────────────────


def test_restore_returns_the_schema_to_the_state_of_the_backup(fresh):
    assert fresh.run("backup.sh").returncode == 0
    dump = next(fresh.backups.glob("db-*.sql.gz"))
    fresh.sql(f"delete from {SCHEMA}.admins; insert into {SCHEMA}.settings(key, value) values ('probe', '\"after-the-backup\"')")

    done = fresh.run("restore.sh", str(dump), stdin="RESTORE\n")
    assert done.returncode == 0, done.stdout + done.stderr
    assert fresh.sql(f"select username from {SCHEMA}.admins") == ADMIN
    assert fresh.sql(f"select count(*) from {SCHEMA}.settings where key = 'probe'") == "0"
    calls = fresh.docker_calls()
    assert "compose run --rm migrate" in calls and "compose up -d" in calls  # migrates and restarts afterwards
    assert "ledger reconciliation" in done.stdout


def test_a_failed_restore_stops_and_changes_nothing(fresh):
    assert fresh.run("backup.sh").returncode == 0
    dump = next(fresh.backups.glob("db-*.sql.gz"))
    broken = fresh.work / "broken.sql.gz"
    sql = subprocess.run(["gzip", "-dc", str(dump)], capture_output=True, text=True, check=True).stdout
    subprocess.run(["gzip", "-9", "-c"], input=sql + "\nSELECT * FROM this_table_does_not_exist;\n", stdout=broken.open("wb"), text=True, check=True)
    fresh.sql(f"insert into {SCHEMA}.settings(key, value) values ('probe', '\"current-data\"')")
    fresh.log.write_text("")

    done = fresh.run("restore.sh", str(broken), stdin="RESTORE\n")
    assert done.returncode != 0
    assert "restore FAILED" in done.stdout and "untouched" in done.stdout
    assert fresh.sql(f"select count(*) from {SCHEMA}.settings where key = 'probe'") == "1"  # the current data survived: it is one transaction
    assert fresh.sql(f"select username from {SCHEMA}.admins") == ADMIN
    assert "compose run --rm migrate" not in fresh.docker_calls()  # and nothing was restarted on top of a failure


def test_restore_does_nothing_unless_restore_is_typed(fresh):
    assert fresh.run("backup.sh").returncode == 0
    dump = next(fresh.backups.glob("db-*.sql.gz"))
    fresh.sql(f"insert into {SCHEMA}.settings(key, value) values ('probe', '\"keep\"')")
    done = fresh.run("restore.sh", str(dump), stdin="nope\n")
    assert done.returncode != 0 and "aborted" in done.stdout
    assert fresh.sql(f"select count(*) from {SCHEMA}.settings where key = 'probe'") == "1"


def test_restore_refuses_a_shared_database_without_a_schema(fresh):
    assert fresh.run("backup.sh").returncode == 0
    dump = next(fresh.backups.glob("db-*.sql.gz"))
    done = fresh.run("restore.sh", str(dump), stdin="RESTORE\n", extra={"DB_SCHEMA": ""})
    assert done.returncode != 0 and "DB_SCHEMA is not set" in done.stdout


# ── install_vps.sh: the part that writes deploy/.env (extracted verbatim; the rest needs root, apt and Docker) ──

TOKEN = "123456789:AAbbccddeeffgghhiijjkkllmmnnooppqqrr"


def installer_env_block() -> str:
    source = (DEPLOY / "install_vps.sh").read_text()
    start = source.index("get() {")
    end = source.index("DOMAIN=$(get DOMAIN)") + len("DOMAIN=$(get DOMAIN)")
    return 'set -euo pipefail\nsay() { printf "\\n==> %s\\n" "$*"; }\n' + source[start:end] + "\n"


def run_installer_block(tmp: Path, answers: list[str], existing_env: str | None = None) -> tuple[subprocess.CompletedProcess[str], Path]:
    work = tmp / "installer"
    work.mkdir()
    shutil.copy(DEPLOY / ".env.example", work / ".env.example")
    if existing_env is not None:
        (work / ".env").write_text(existing_env)
    script = work / "block.sh"
    script.write_text(installer_env_block())
    done = subprocess.run(["bash", str(script)], cwd=work, input="".join(a + "\n" for a in answers), capture_output=True, text=True, timeout=60, check=False)
    return done, work / ".env"


def env_value(env_file: Path, key: str) -> str | None:
    for line in env_file.read_text().splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1]
    return None


def test_installer_stores_a_supabase_setup_without_a_database_container(tmp_path):
    good = "postgresql://postgres.abcd:p%40ss@aws-0-ap-south-1.pooler.supabase.com:6543/postgres"
    done, env = run_installer_block(
        tmp_path,
        ["admin.example.com", "ops@example.com", TOKEN, "111,222", "2", "postgresql://u:pa$$word@h.supabase.com:5432/postgres", good, ""],
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "that does not look right" in done.stdout  # a URL containing $ is refused (Compose would interpolate it) and asked for again
    assert "WARNING: port 6543" in done.stdout  # the transaction pooler breaks prepared statements
    assert env_value(env, "DATABASE_URL") == good  # stored exactly as typed, percent-encoding intact
    assert env_value(env, "DB_SCHEMA") == "qrbot" and env_value(env, "DB_POOL_SIZE") == "3" and env_value(env, "DB_MAX_OVERFLOW") == "2"
    assert env_value(env, "COMPOSE_PROFILES") is None and env_value(env, "POSTGRES_PASSWORD") is None  # no db container
    assert re.fullmatch(r"[0-9a-f]{64}", env_value(env, "SECRET_KEY") or "")
    assert env_value(env, "ADMIN_TELEGRAM_IDS") == "111,222"
    assert (env.stat().st_mode & 0o777) == 0o600


@pytest.mark.parametrize("choice", ["1", ""])  # an empty answer takes the default
def test_installer_defaults_to_the_database_container_with_a_generated_password(tmp_path, choice):
    done, env = run_installer_block(tmp_path, ["admin.example.com", "ops@example.com", TOKEN, "111", choice])
    assert done.returncode == 0, done.stdout + done.stderr
    assert env_value(env, "COMPOSE_PROFILES") == "local-db"
    assert re.fullmatch(r"[0-9a-f]{48}", env_value(env, "POSTGRES_PASSWORD") or "")
    assert env_value(env, "DATABASE_URL") is None and env_value(env, "DB_SCHEMA") is None


def test_installer_keeps_an_existing_env_and_refuses_one_without_any_database_setting(tmp_path):
    base = f"DOMAIN=a.example.com\nACME_EMAIL=a@b.co\nTELEGRAM_BOT_TOKEN={TOKEN}\nADMIN_TELEGRAM_IDS=1\nSECRET_KEY=abc\n"
    (tmp_path / "kept").mkdir()
    done, env = run_installer_block(tmp_path / "kept", [], existing_env=base + "POSTGRES_PASSWORD=x\n")
    assert done.returncode == 0 and "already exists" in done.stdout and env_value(env, "DOMAIN") == "a.example.com"
    (tmp_path / "bare").mkdir()
    done, _ = run_installer_block(tmp_path / "bare", [], existing_env=base)
    assert done.returncode != 0 and "set POSTGRES_PASSWORD" in done.stdout


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker is not installed")
@pytest.mark.parametrize("choice", ["1", "2"])
def test_what_the_installer_writes_is_accepted_by_docker_compose(tmp_path, choice):
    answers = ["admin.example.com", "ops@example.com", TOKEN, "111", choice]
    if choice == "2":
        answers += ["postgresql://postgres.abcd:pw@aws-0-eu-west-1.pooler.supabase.com:5432/postgres", ""]
    done, env = run_installer_block(tmp_path, answers)
    assert done.returncode == 0, done.stdout + done.stderr
    for name in ("docker-compose.yml",):
        shutil.copy(DEPLOY / name, env.parent / name)
    (env.parent / "certs").mkdir()
    services = subprocess.run(["docker", "compose", "config", "--services"], cwd=env.parent, capture_output=True, text=True, timeout=60, check=False)
    if services.returncode != 0 and "docker compose" in services.stderr.lower() and "not a docker command" in services.stderr.lower():
        pytest.skip("the docker compose plugin is not installed")
    assert services.returncode == 0, services.stderr
    assert ("db" in services.stdout.split()) == (choice == "1")  # the container only in the container setup

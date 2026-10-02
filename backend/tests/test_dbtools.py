"""Hosted-database support (Supabase): a dedicated schema, migrations into it, Row Level Security, the self-check."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from app import dbtools
from app.config import Settings, normalize_database_url
from app.db import pin_schema, session_scope
from app.models import Base

BACKEND = Path(__file__).resolve().parents[1]
DATABASE_URL = os.environ["DATABASE_URL"]


def admin_url() -> str:
    """The same server, but the maintenance database (to create / drop scratch databases)."""
    return make_url(DATABASE_URL).set(database="postgres").render_as_string(hide_password=False)


def suffix() -> str:
    return uuid.uuid4().hex[:8]


# ── settings ────────────────────────────────────────────────────────────────


def test_db_schema_setting_accepts_plain_identifiers_only():
    assert Settings(environment="test", db_schema="").db_schema == ""
    assert Settings(environment="test", db_schema="  QrBot_2 ").db_schema == "qrbot_2"
    for bad in ("public", "auth", "information_schema", "pg_temp", "a-b", "1abc", 'x"; drop table users; --', "a b", "x" * 64):
        with pytest.raises(ValueError):
            Settings(environment="test", db_schema=bad)


def test_production_refuses_the_db_container_without_a_password():
    strong = "0123456789abcdef" * 4
    # what docker-compose composes when neither POSTGRES_PASSWORD nor DATABASE_URL was set in deploy/.env
    with pytest.raises(ValueError, match="POSTGRES_PASSWORD"):
        Settings(environment="production", secret_key=strong, database_url="postgresql+asyncpg://qrbot:@db:5432/qrbot")
    assert Settings(environment="production", secret_key=strong, database_url="postgresql+asyncpg://qrbot:pw@db:5432/qrbot")
    assert Settings(environment="production", secret_key=strong, database_url="postgresql+asyncpg://u:pw@aws-0-x.pooler.supabase.com:5432/postgres")
    assert Settings(environment="development", database_url="postgresql+asyncpg://qrbot:@db:5432/qrbot")  # only production insists


def test_pool_settings_have_sane_bounds():
    assert Settings(environment="test").db_pool_size == 10
    with pytest.raises(ValueError):
        Settings(environment="test", db_pool_size=0)
    with pytest.raises(ValueError):
        Settings(environment="test", db_pool_recycle_seconds=5)


# ── advice for hosted databases (pure) ──────────────────────────────────────

POOLER = "postgresql+asyncpg://postgres.abcd:pw@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"


def advice(url: str, schema: str = "qrbot") -> dict[str, tuple[bool | None, str]]:
    return {label: (status, detail) for status, label, detail in dbtools.hosted_db_advice(url, schema, 20)}


def test_advice_is_silent_for_an_ordinary_database():
    assert dbtools.hosted_db_advice("postgresql+asyncpg://qrbot:pw@db:5432/qrbot", "", 20) == []


def test_database_url_is_normalised_for_asyncpg():
    plain = "postgresql+asyncpg://qrbot:pw@db:5432/qrbot"
    assert normalize_database_url(plain) == plain  # an ordinary URL is left alone
    assert normalize_database_url("postgres://u:p%40ss@h:5432/d") == "postgresql+asyncpg://u:p%40ss@h:5432/d"
    assert normalize_database_url("postgresql://u:p@h/d?sslmode=verify-full") == "postgresql+asyncpg://u:p@h/d?ssl=verify-full"
    assert normalize_database_url("postgres://u:p@h/d?pgbouncer=true&sslmode=require") == "postgresql+asyncpg://u:p@h/d?ssl=require"
    # Supabase accepts unencrypted connections, so SSL is switched on unless a mode was chosen
    assert normalize_database_url("postgres://postgres.x:p@aws-0-ap-south-1.pooler.supabase.com:5432/postgres").endswith("?ssl=require")
    assert normalize_database_url("postgres://postgres:p@db.x.supabase.co:5432/postgres?ssl=verify-full").endswith("?ssl=verify-full")
    assert Settings(environment="test", database_url="postgres://u:p@h/d").database_url == "postgresql+asyncpg://u:p@h/d"


def test_advice_for_a_correct_supabase_setup_only_mentions_the_pool():
    out = advice(POOLER + "?ssl=require")
    assert set(out) == {"connection pool"} and out["connection pool"][0] is None
    assert "2 x 20" in out["connection pool"][1]


def test_advice_flags_the_transaction_pooler_as_a_problem():
    status, detail = advice(POOLER.replace(":5432", ":6543") + "?ssl=require")["database port"]
    assert status is False and "prepared statements" in detail


def test_advice_asks_for_a_schema_explains_the_direct_host_and_objects_to_weakened_ssl():
    out = advice("postgresql+asyncpg://postgres:pw@db.abcd.supabase.co:5432/postgres?ssl=require", schema="")
    assert out["DB_SCHEMA"][0] is None and "public" in out["DB_SCHEMA"][1]
    assert "IPv6" in out["database host"][1]
    assert "database SSL" not in out
    weak = advice(POOLER + "?ssl=prefer")["database SSL"]
    assert weak[0] is None and "unencrypted" in weak[1]


def test_libpq_url_translates_the_sqlalchemy_url_for_pg_dump():
    assert dbtools.libpq_url(POOLER + "?ssl=require") == "postgresql://postgres.abcd:pw@aws-0-ap-south-1.pooler.supabase.com:5432/postgres?sslmode=require"
    assert dbtools.libpq_url("postgresql+asyncpg://qrbot:s%40crt@db:5432/qrbot") == "postgresql://qrbot:s%40crt@db:5432/qrbot"  # encoded password survives
    assert "sslmode=verify-full" in dbtools.libpq_url(POOLER + "?ssl=verify-full")
    assert dbtools.libpq_url(POOLER + "?sslmode=require&ssl=disable").count("sslmode") == 1  # an explicit libpq setting wins
    # a CA file for verify-full travels along, so pg_dump / psql in their own container can verify the server too
    verified = dbtools.libpq_url(POOLER + "?ssl=verify-full", "/certs/ca.crt")
    assert dict(make_url(verified).query) == {"sslmode": "verify-full", "sslrootcert": "/certs/ca.crt"}  # (percent-encoded in the string; libpq decodes it)
    assert "sslrootcert" not in dbtools.libpq_url(POOLER + "?ssl=require")


# ── schema pinning ──────────────────────────────────────────────────────────


async def test_pinned_connections_only_see_their_schema_and_keep_it_after_rollbacks():
    schema = f"pin_{suffix()}"
    admin = create_async_engine(DATABASE_URL)
    pinned = create_async_engine(DATABASE_URL)
    pin_schema(pinned, schema)
    try:
        async with admin.begin() as conn:
            await conn.execute(text(f"CREATE SCHEMA {schema}"))
        async with pinned.connect() as conn:
            assert (await conn.execute(text("SHOW search_path"))).scalar() == schema
            await conn.rollback()  # a plain SET inside a transaction would be undone here
            assert (await conn.execute(text("SHOW search_path"))).scalar() == schema
            await conn.execute(text("CREATE TABLE probe (id int)"))
            await conn.commit()
        async with pinned.connect() as conn:  # a recycled / new pooled connection
            assert (await conn.execute(text("SHOW search_path"))).scalar() == schema
            assert (await conn.execute(text("SELECT to_regclass('probe')"))).scalar() == "probe"
        async with admin.connect() as conn:
            assert (await conn.execute(text(f"SELECT to_regclass('{schema}.probe')"))).scalar() is not None
            assert (await conn.execute(text("SELECT to_regclass('public.probe')"))).scalar() is None
    finally:
        async with admin.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        await pinned.dispose()
        await admin.dispose()


def test_pin_schema_refuses_anything_but_an_identifier():
    engine = create_async_engine(DATABASE_URL)
    with pytest.raises(ValueError):
        pin_schema(engine, "x; DROP TABLE users")


@pytest_asyncio.fixture
async def supabase_roles():
    created: list[str] = []
    engine = create_async_engine(DATABASE_URL)
    try:
        async with engine.begin() as conn:
            for role in ("anon", "authenticated", "service_role", "authenticator"):
                if (await conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role})).first() is None:
                    await conn.execute(text(f"CREATE ROLE {role} NOLOGIN"))
                    created.append(role)
    except DBAPIError:
        await engine.dispose()
        pytest.skip("the database user cannot create roles (needs CREATEROLE) - Supabase-style roles are simulated with it")
    yield created
    async with engine.begin() as conn:
        for role in created:
            await conn.execute(text(f"DROP ROLE IF EXISTS {role}"))
    await engine.dispose()


# ── migrating into a dedicated schema ───────────────────────────────────────


def run_cli(url: str, schema: str, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    full_env = {**os.environ, "DATABASE_URL": url, "DB_SCHEMA": schema, **(env or {})}
    return subprocess.run([sys.executable, "-m", "app.cli", *args], cwd=BACKEND, env=full_env, capture_output=True, text=True, timeout=120, check=False)


@asynccontextmanager
async def scratch_database():
    """A throw-away database on the test server; yields its URL."""
    name = f"qrbot_it_{suffix()}"
    admin = create_async_engine(admin_url(), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
        yield make_url(DATABASE_URL).set(database=name).render_as_string(hide_password=False)
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


async def test_migrate_creates_everything_in_the_dedicated_schema_and_nothing_in_public():
    schema = "qrbot_app"
    async with scratch_database() as url:
        first = run_cli(url, schema, "migrate")
        assert first.returncode == 0, first.stdout + first.stderr
        assert "database is up to date" in first.stdout
        second = run_cli(url, schema, "migrate")  # idempotent
        assert second.returncode == 0, second.stdout + second.stderr

        scratch = create_async_engine(url)
        async with scratch.connect() as conn:
            rows = (await conn.execute(text("SELECT table_schema, count(*) FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog', 'information_schema') GROUP BY 1"))).all()
            assert dict(rows) == {schema: len(Base.metadata.tables) + 1}  # + alembic_version
            assert (await conn.execute(text(f"SELECT version_num FROM {schema}.alembic_version"))).scalar() == "0001"
        await scratch.dispose()

        checked = run_cli(url, schema, "check")
        assert f"schema '{schema}'" in checked.stdout  # the self-check looks at the right place


async def test_migrate_switches_row_level_security_on_when_supabase_roles_exist(supabase_roles):
    schema = "qrbot_app"
    async with scratch_database() as url:
        migrated = run_cli(url, schema, "migrate")
        assert migrated.returncode == 0, migrated.stdout + migrated.stderr
        assert f"Row Level Security switched on for {len(Base.metadata.tables) + 1} table(s) in schema '{schema}'" in migrated.stdout

        scratch = create_async_engine(url)
        async with scratch.connect() as conn:
            off = (await conn.execute(text("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = :s AND c.relkind = 'r' AND NOT c.relrowsecurity"), {"s": schema})).all()
        await scratch.dispose()
        assert off == []

        again = run_cli(url, schema, "migrate")  # nothing left to do
        assert again.returncode == 0 and "switched on" not in again.stdout
        checked = run_cli(url, schema, "check")
        assert "exposed over HTTP - no" in checked.stdout


# ── helpers the deployment scripts call ─────────────────────────────────────


async def test_admin_exists_reports_through_its_exit_status():
    async with scratch_database() as url:
        assert run_cli(url, "qrbot_app", "migrate").returncode == 0
        assert run_cli(url, "qrbot_app", "admin-exists").returncode == 1  # nobody yet: the installer then asks for a password
        created = run_cli(url, "qrbot_app", "create-admin", "--username", "boss", env={"ADMIN_PASSWORD": "Correct-Horse-Battery-77"})
        assert created.returncode == 0, created.stdout + created.stderr
        assert run_cli(url, "qrbot_app", "admin-exists").returncode == 0


def test_dump_url_prints_the_libpq_form_and_nothing_else():
    out = run_cli("postgres://u:p%40ss@aws-0-x.pooler.supabase.com:5432/postgres", "qrbot", "dump-url")
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "postgresql://u:p%40ss@aws-0-x.pooler.supabase.com:5432/postgres?sslmode=require"


# ── a dedicated database role (needs CREATEROLE to set up; skipped otherwise) ──


@pytest_asyncio.fixture
async def plain_role():
    """A login role without any privileges beyond connecting - what a freshly created dedicated role is. Yields its name."""
    name = f"qrbot_role_{suffix()}"
    engine = create_async_engine(DATABASE_URL, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f"CREATE ROLE {name} LOGIN PASSWORD 'pw'"))
    except DBAPIError:
        await engine.dispose()
        pytest.skip("the database user cannot create roles (needs CREATEROLE)")
    yield name
    async with engine.connect() as conn:  # the scratch database, and with it everything the role owned, is gone by now
        await conn.execute(text(f"DROP ROLE IF EXISTS {name}"))
    await engine.dispose()


def as_role(url: str, role: str) -> str:
    return make_url(url).set(username=role, password="pw").render_as_string(hide_password=False)


async def ensure_schema_as(url: str, role: str, schema: str) -> None:
    engine = create_async_engine(as_role(url, role))
    try:
        async with engine.connect() as conn:
            await conn.run_sync(dbtools.ensure_schema, schema)
    finally:
        await engine.dispose()


async def test_ensure_schema_creates_a_missing_schema_and_leaves_an_existing_one_alone():
    schema = f"ens_{suffix()}"
    engine = create_async_engine(DATABASE_URL)
    try:
        for _ in range(2):  # the second run finds it and does nothing
            async with engine.connect() as conn:
                await conn.run_sync(dbtools.ensure_schema, schema)
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT count(*) FROM pg_namespace WHERE nspname = :s"), {"s": schema})).scalar() == 1
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        await engine.dispose()


async def test_a_role_that_may_not_create_schemas_is_told_what_to_grant(plain_role):
    async with scratch_database() as url:
        with pytest.raises(dbtools.SchemaAccessError) as err:
            await ensure_schema_as(url, plain_role, "qrbot")
        message = str(err.value)
        assert f"GRANT CREATE ON DATABASE {make_url(url).database} TO {plain_role}" in message
        assert "AUTHORIZATION" in message and "docs/SUPABASE.md" in message

        admin = create_async_engine(url, isolation_level="AUTOCOMMIT")
        async with admin.connect() as conn:  # the fix the message names
            await conn.execute(text(f'GRANT CREATE ON DATABASE "{make_url(url).database}" TO {plain_role}'))
        await ensure_schema_as(url, plain_role, "qrbot")
        async with admin.connect() as conn:
            owner = (await conn.execute(text("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'qrbot'"))).scalar()
        await admin.dispose()
        assert owner == plain_role  # the app's own role owns its schema - so Row Level Security never applies to it


async def test_a_schema_that_belongs_to_another_role_is_explained_not_reported_as_existing(plain_role):
    async with scratch_database() as url:
        admin = create_async_engine(url, isolation_level="AUTOCOMMIT")
        async with admin.connect() as conn:
            await conn.execute(text("CREATE SCHEMA qrbot"))  # created by the test user, as when the first migration ran with the master role
        await admin.dispose()
        with pytest.raises(dbtools.SchemaAccessError) as err:
            await ensure_schema_as(url, plain_role, "qrbot")
        message = str(err.value)
        assert "belongs to" in message and f"ALTER SCHEMA qrbot OWNER TO {plain_role}" in message


async def test_migrate_prints_the_fix_instead_of_a_traceback_when_the_role_lacks_privileges(plain_role):
    async with scratch_database() as url:
        out = run_cli(as_role(url, plain_role), "qrbot", "migrate")
        assert out.returncode == 1
        assert "❌" in out.stdout and "GRANT CREATE ON DATABASE" in out.stdout
        assert "Traceback" not in out.stderr


# ── Row Level Security (needs Supabase's roles; skipped when the test user may not create roles) ──


async def test_row_level_security_is_enabled_for_every_table_of_the_schema_and_only_there(supabase_roles):
    schema = f"rls_{suffix()}"
    async with session_scope() as db:
        await db.execute(text(f"CREATE SCHEMA {schema}"))
        await db.execute(text(f"CREATE TABLE {schema}.a (id int)"))
        await db.execute(text(f"CREATE TABLE {schema}.b (id int)"))
        await db.execute(text(f"CREATE TABLE {schema}.c (id int)"))
        await db.execute(text(f"ALTER TABLE {schema}.c ENABLE ROW LEVEL SECURITY"))
    try:
        async with session_scope() as db:
            assert await dbtools.tables_without_rls(db, schema) == ["a", "b"]
            assert await dbtools.enable_row_level_security(db, schema) == ["a", "b"]
        async with session_scope() as db:
            assert await dbtools.tables_without_rls(db, schema) == []
            assert await dbtools.enable_row_level_security(db, schema) == []  # idempotent
            assert await dbtools.exposure_problems(db, schema) == []
    finally:
        async with session_scope() as db:
            await db.execute(text(f"DROP SCHEMA {schema} CASCADE"))


def test_exposed_schemas_are_read_from_the_authenticator_settings():
    assert dbtools.exposed_schemas_from(["search_path=x", "pgrst.db_schemas=public, graphql_public ,mine"]) == ["public", "graphql_public", "mine"]
    assert dbtools.exposed_schemas_from(["search_path=x"]) == []
    assert dbtools.exposed_schemas_from([]) == []


async def test_exposure_check_names_the_ways_data_could_leak(supabase_roles, monkeypatch):
    schema = f"leak_{suffix()}"
    async with session_scope() as db:
        await db.execute(text(f"CREATE SCHEMA {schema}"))
        await db.execute(text(f"CREATE TABLE {schema}.t (id int)"))
        await db.execute(text(f"GRANT USAGE ON SCHEMA {schema} TO anon"))

    async def exposed(_db):
        return ["public", schema]

    monkeypatch.setattr(dbtools, "_data_api_schemas", exposed)  # setting the real parameter needs more privileges than a test user has
    try:
        async with session_scope() as db:
            problems = " | ".join(await dbtools.exposure_problems(db, schema))
        assert "Exposed schemas" in problems
        assert "role 'anon' can use schema" in problems
        assert "Row Level Security is off for 1 table" in problems
        async with session_scope() as db:
            assert "'public' schema" in " ".join(await dbtools.exposure_problems(db, "public"))
    finally:
        async with session_scope() as db:
            await db.execute(text(f"DROP SCHEMA {schema} CASCADE"))


async def test_nothing_happens_on_an_ordinary_postgres_without_supabase_roles(db):
    if await dbtools.api_roles_present(db):
        pytest.skip("this cluster already has Supabase-style roles")
    schema = await dbtools.effective_schema(db)
    assert await dbtools.enable_row_level_security(db, schema) == []
    assert await dbtools.exposure_problems(db, schema) == []

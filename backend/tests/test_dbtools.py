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
from app.config import Settings
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


def test_advice_for_a_correct_supabase_setup_only_mentions_the_pool():
    out = advice(POOLER + "?ssl=require")
    assert set(out) == {"connection pool"} and out["connection pool"][0] is None
    assert "2 x 20" in out["connection pool"][1]


def test_advice_flags_the_transaction_pooler_as_a_problem():
    status, detail = advice(POOLER.replace(":5432", ":6543") + "?ssl=require")["database port"]
    assert status is False and "prepared statements" in detail


def test_advice_asks_for_ssl_a_schema_and_explains_the_direct_host():
    out = advice("postgresql+asyncpg://postgres:pw@db.abcd.supabase.co:5432/postgres", schema="")
    assert out["database SSL"][0] is None and "ssl=require" in out["database SSL"][1]
    assert out["DB_SCHEMA"][0] is None and "public" in out["DB_SCHEMA"][1]
    assert "IPv6" in out["database host"][1]
    assert "database SSL" not in advice(POOLER + "?sslmode=require")


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


def run_cli(url: str, schema: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DATABASE_URL": url, "DB_SCHEMA": schema}
    return subprocess.run([sys.executable, "-m", "app.cli", *args], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=120, check=False)


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

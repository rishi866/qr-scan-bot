"""Alembic environment (async engine, URL from ``DATABASE_URL``).

With ``DB_SCHEMA`` set (hosted / shared database, e.g. Supabase) every table - and Alembic's own ``alembic_version`` -
lives in that schema: the connection is pinned to it, and the schema is created first if it does not exist.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.db import pin_schema
from app.dbtools import ensure_schema
from app.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata
SCHEMA = get_settings().db_schema or None  # validated: a plain lower-case identifier


def _url() -> str:
    return os.environ.get("ALEMBIC_DATABASE_URL") or get_settings().database_url


def run_migrations_offline() -> None:
    """``alembic upgrade head --sql``: prints the SQL (used to apply the schema by hand, e.g. in Supabase's SQL editor)."""
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True, compare_type=True, version_table_schema=SCHEMA)
    with context.begin_transaction():
        if SCHEMA:
            context.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
            context.execute(f"SET search_path TO {SCHEMA}")
        context.run_migrations()


def _do_run(connection: Connection) -> None:
    if SCHEMA:
        ensure_schema(connection, SCHEMA)  # not "CREATE SCHEMA IF NOT EXISTS": that needs the CREATE privilege even when the schema is there
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True, version_table_schema=SCHEMA)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(_url(), poolclass=pool.NullPool)
    if SCHEMA:
        pin_schema(engine, SCHEMA)
    async with engine.connect() as connection:
        await connection.run_sync(_do_run)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

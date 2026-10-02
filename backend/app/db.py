"""Async SQLAlchemy engine / session helpers."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None

_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}")


def pin_schema(engine: AsyncEngine, schema: str) -> None:
    """Make every new connection of ``engine`` see *only* ``schema`` (``SET search_path``).

    Used with a hosted, shared database (Supabase): our tables live in their own schema, so they cannot collide with
    anything else in the database and are not part of the API-exposed ``public`` schema. ``public`` is deliberately not
    on the path - if our schema were missing, a query fails instead of silently touching someone else's table.
    It is a plain ``SET`` after connecting (not a startup parameter), which also works through session-mode poolers.
    """
    if not _IDENTIFIER.fullmatch(schema):
        raise ValueError(f"not a valid schema name: {schema!r}")

    @event.listens_for(engine.sync_engine, "connect", insert=True)
    def _set_search_path(dbapi_connection: Any, _record: Any) -> None:
        previous = dbapi_connection.autocommit
        dbapi_connection.autocommit = True  # inside a transaction the SET would be undone by the pool's rollback
        try:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute(f"SET search_path TO {schema}")
            finally:
                cursor.close()
        finally:
            dbapi_connection.autocommit = previous


def init_engine(url: str | None = None, **kwargs) -> AsyncEngine:
    """(Re)create the global engine. Safe to call again (tests point it at a scratch database)."""
    global _engine, _sessionmaker
    settings = get_settings()
    params = {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_pre_ping": True,
        "pool_recycle": settings.db_pool_recycle_seconds,
    }
    params.update(kwargs)
    _engine = create_async_engine(url or settings.database_url, **params)
    if settings.db_schema:
        pin_schema(_engine, settings.db_schema)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        init_engine()
    assert _engine is not None
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        init_engine()
    assert _sessionmaker is not None
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """One transaction: commit on success, roll back on any exception."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None

"""Test fixtures: a real PostgreSQL database (``qrbot_test``), truncated before every test.

Start Postgres and create the role/database once (see docs/SETUP.md), or point
``TEST_DATABASE_URL`` at any empty scratch database.
"""

from __future__ import annotations

import os
import tempfile

os.environ["ENVIRONMENT"] = "test"
os.environ["SECRET_KEY"] = "test-secret-key-test-secret-key-test-secret-key"
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://qrbot:qrbot@127.0.0.1:5432/qrbot_test"
)
os.environ["ADMIN_TELEGRAM_IDS"] = "9001,9002"
os.environ["UPLOAD_DIR"] = tempfile.mkdtemp(prefix="qrbot-uploads-")
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:TEST-TOKEN"
os.environ["PUBLIC_BASE_URL"] = "https://admin.example.test"
os.environ["COOKIE_SECURE"] = "false"

import pytest_asyncio  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app import db as app_db  # noqa: E402
from app.models import Base  # noqa: E402

_TABLES = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)


@pytest_asyncio.fixture(scope="session", loop_scope="session", autouse=True)
async def _engine():
    engine = app_db.init_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await app_db.dispose_engine()


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _clean_database(_engine):
    async with app_db.get_engine().begin() as conn:
        await conn.execute(text(f"TRUNCATE TABLE {_TABLES} RESTART IDENTITY CASCADE"))
    yield


@pytest_asyncio.fixture(loop_scope="session")
async def db():
    """A plain session; tests commit explicitly when they need other sessions to see data."""
    async with app_db.get_sessionmaker()() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture(loop_scope="session")
async def api():
    """An ``httpx`` client already logged in as an admin (cookie + CSRF header)."""
    import httpx

    from app.api import security
    from app.api.main import app
    from tests.api_helpers import CSRF, login, make_admin

    security.login_limiter.reset()
    await make_admin()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=CSRF) as client:
        response = await login(client)
        assert response.status_code == 200, response.text
        yield client

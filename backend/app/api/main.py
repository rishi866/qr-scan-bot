"""FastAPI application: ``uvicorn app.api.main:app``."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text
from starlette.middleware.base import BaseHTTPMiddleware

from app.api import security
from app.api.routers import auth
from app.config import get_settings
from app.db import dispose_engine, session_scope
from app.logging_conf import setup_logging
from app.models import Admin
from app.timeutil import utcnow

log = logging.getLogger(__name__)


async def ensure_bootstrap_admin() -> bool:
    """Create the first admin from ``ADMIN_BOOTSTRAP_*`` when the table is empty."""
    settings = get_settings()
    password = settings.admin_bootstrap_password.get_secret_value()
    async with session_scope() as db:
        count = (await db.execute(select(func.count()).select_from(Admin))).scalar_one()
        if count:
            return False
        if not password:
            log.warning("No admin exists yet. Set ADMIN_BOOTSTRAP_PASSWORD or run: python -m app.cli create-admin")
            return False
        username = settings.admin_bootstrap_username.strip().lower() or "admin"
        try:
            security.validate_password_strength(password, username)
        except ValueError as exc:
            log.error("ADMIN_BOOTSTRAP_PASSWORD rejected: %s", exc)
            return False
        db.add(Admin(username=username, password_hash=security.hash_password(password)))
        log.info("created the first admin account %r", username)
        return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    await ensure_bootstrap_admin()
    yield
    await dispose_engine()


# The admin panel may not be framed or load anything from other origins. (Next.js' static export needs
# inline scripts / styles for hydration, hence 'unsafe-inline'; `data:` images are the 2FA QR code.)
PANEL_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; object-src 'none'; "
    "base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)
# The time-zone Mini App is opened inside Telegram (an iframe on Telegram Web), so it may only be framed by Telegram.
MINI_APP_PATHS = ("/tz.html", "/tz/")
MINI_APP_CSP = "frame-ancestors https://web.telegram.org https://webk.telegram.org https://webz.telegram.org https://*.telegram.org"


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if path in MINI_APP_PATHS:
            response.headers.setdefault("Content-Security-Policy", MINI_APP_CSP)
        else:
            response.headers.setdefault("X-Frame-Options", "DENY")
            if not path.startswith("/api/"):
                response.headers.setdefault("Content-Security-Policy", PANEL_CSP)
        if path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response


def create_app() -> FastAPI:
    settings = get_settings()
    production = settings.environment == "production"
    app = FastAPI(
        title="QR Exchange - Admin API",
        version="1.0.0",
        docs_url=None if production else "/api/docs",
        redoc_url=None,
        openapi_url=None if production else "/api/openapi.json",
        lifespan=lifespan,
    )
    app.add_middleware(SecurityHeaders)
    if settings.cors_origins:  # only for `next dev` on another port; production is same-origin behind Caddy
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Content-Type", "X-Requested-With"],
        )

    @app.get("/api/health", tags=["system"])
    async def health():
        try:
            async with session_scope() as db:
                await db.execute(text("SELECT 1"))
            database = True
        except Exception:
            log.exception("health check: database unreachable")
            database = False
        return JSONResponse({"status": "ok" if database else "degraded", "database": database, "time": utcnow().isoformat()}, status_code=200 if database else 503)

    app.include_router(auth.router)
    _include_optional_routers(app)

    static_dir = settings.admin_static_dir
    if static_dir and Path(static_dir).is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="admin-panel")
    return app


def _include_optional_routers(app: FastAPI) -> None:
    from app.api.routers import ROUTERS

    for router in ROUTERS:
        app.include_router(router)


app = create_app()

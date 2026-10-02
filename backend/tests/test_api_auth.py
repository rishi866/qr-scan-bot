"""Admin API: authentication, sessions, CSRF protection, lockout, password change and 2FA."""

from __future__ import annotations

import datetime as dt

import httpx
import pyotp
import pytest
from sqlalchemy import select

from app.api import security
from app.api.main import app, create_app, ensure_bootstrap_admin
from app.config import get_settings
from app.db import session_scope
from app.models import Admin, AuditLog
from app.timeutil import utcnow
from tests.api_helpers import CSRF, PASSWORD, login, make_admin


@pytest.fixture(autouse=True)
def _reset_limiter():
    security.login_limiter.reset()


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=CSRF) as c:
        yield c


async def test_health_is_public_and_reports_the_database(client):
    r = await client.get("/api/health")
    assert r.status_code == 200 and r.json()["database"] is True


async def test_login_sets_httponly_strict_cookie_and_me_works(client):
    await make_admin()
    r = await login(client)
    assert r.status_code == 200 and r.json()["status"] == "ok" and r.json()["admin"]["username"] == "boss"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and security.SESSION_COOKIE in cookie
    me = await client.get("/api/auth/me")
    assert me.status_code == 200 and me.json()["username"] == "boss"
    assert "password" not in me.text.lower()
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"


async def test_username_is_case_insensitive_and_wrong_credentials_are_generic(client):
    await make_admin()
    assert (await login(client, "BOSS")).status_code == 200
    client.cookies.clear()
    bad_pw = await login(client, "boss", "nope")
    bad_user = await login(client, "ghost", "nope")
    assert bad_pw.status_code == bad_user.status_code == 401
    assert bad_pw.json() == bad_user.json()  # no hint whether the user exists


async def test_unauthenticated_requests_are_refused_everywhere(client):
    spec = app.openapi()["paths"]
    public = {("post", "/api/auth/login"), ("post", "/api/auth/2fa"), ("post", "/api/auth/logout"), ("get", "/api/health")}
    checked = 0
    for path, ops in spec.items():
        for method in ops:
            if (method, path) in public:
                continue
            url = path.replace("{user_id}", "1").replace("{dispute_id}", "1").replace("{deposit_id}", "1").replace("{withdrawal_id}", "1").replace("{slot_id}", "1").replace("{admin_id}", "1")
            r = await client.request(method.upper(), url, json={} if method != "get" else None)
            assert r.status_code == 401, f"{method.upper()} {path} answered {r.status_code} without a session"
            checked += 1
    assert checked > 40


async def test_state_changing_calls_need_the_csrf_header(client):
    await make_admin()
    await login(client)
    no_header = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", cookies=client.cookies)
    async with no_header:
        r = await no_header.post("/api/users/1/approve")
        assert r.status_code == 403 and "CSRF" in r.json()["detail"]
        assert (await no_header.get("/api/auth/me")).status_code == 200  # reads are fine


async def test_forged_or_expired_tokens_are_rejected(client):
    admin_id = await make_admin()
    client.cookies.set(security.SESSION_COOKIE, "garbage")
    assert (await client.get("/api/auth/me")).status_code == 401
    expired = security.create_token(admin_id, "boss", 0, security.TOKEN_ACCESS, -10)
    client.cookies.set(security.SESSION_COOKIE, expired)
    assert (await client.get("/api/auth/me")).status_code == 401
    # a 2FA challenge token is not a session
    challenge = security.create_token(admin_id, "boss", 0, security.TOKEN_CHALLENGE, 300)
    client.cookies.set(security.SESSION_COOKIE, challenge)
    assert (await client.get("/api/auth/me")).status_code == 401
    # signed with another key
    import jwt

    forged = jwt.encode({"sub": "boss", "aid": admin_id, "tv": 0, "typ": "access", "iat": 1, "exp": 9999999999}, "other-key-other-key-other-key-1234", algorithm="HS256")
    client.cookies.set(security.SESSION_COOKIE, forged)
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_logout_revokes_the_session_everywhere(client):
    await make_admin()
    await login(client)
    stolen = client.cookies.get(security.SESSION_COOKIE)
    assert (await client.post("/api/auth/logout")).status_code == 200
    assert (await client.get("/api/auth/me")).status_code == 401
    client.cookies.set(security.SESSION_COOKIE, stolen)  # the old cookie no longer works either
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_account_locks_after_repeated_failures(client):
    await make_admin()
    for _ in range(security_max := 5):
        assert (await login(client, "boss", "wrong")).status_code == 401
    assert security_max == 5
    locked = await login(client)  # even the right password is refused while locked
    assert locked.status_code == 429
    async with session_scope() as db:
        admin = (await db.execute(select(Admin))).scalar_one()
        admin.locked_until = utcnow() - dt.timedelta(seconds=1)  # lock expires
    security.login_limiter.reset()
    assert (await login(client)).status_code == 200
    async with session_scope() as db:
        actions = [a.action for a in (await db.execute(select(AuditLog))).scalars()]
    assert actions.count("login_failed") == 5 and "login" in actions


async def test_ip_rate_limit_applies_before_any_password_check(client):
    await make_admin()
    statuses = [(await login(client, "ghost", "x")).status_code for _ in range(18)]
    assert statuses[:15] == [401] * 15 and set(statuses[15:]) == {429}


async def test_change_password_signs_out_other_sessions(client):
    await make_admin()
    await login(client)
    other = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=CSRF)
    async with other:
        await login(other)
        weak = await client.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": "short"})
        assert weak.status_code == 422
        digits = await client.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": "1234567890123"})
        assert digits.status_code == 422
        wrong = await client.post("/api/auth/password", json={"current_password": "nope", "new_password": "Another-Strong-77"})
        assert wrong.status_code == 403
        ok = await client.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": "Another-Strong-77"})
        assert ok.status_code == 200
        assert (await client.get("/api/auth/me")).status_code == 200  # the current session got a fresh cookie
        assert (await other.get("/api/auth/me")).status_code == 401  # the other one is gone
    client.cookies.clear()
    assert (await login(client)).status_code == 401
    assert (await login(client, "boss", "Another-Strong-77")).status_code == 200


class Clock:
    """A controllable wall clock for the TOTP verifier (codes are only valid near 'now' and only once)."""

    def __init__(self, monkeypatch):
        import time as real
        from types import SimpleNamespace

        self.offset = 0.0
        self._real = real
        monkeypatch.setattr(security, "time", SimpleNamespace(time=lambda: real.time() + self.offset, monotonic=real.monotonic))

    def now(self) -> float:
        return self._real.time() + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += seconds


async def test_two_factor_enrolment_login_replay_and_disable(client, monkeypatch):
    clock = Clock(monkeypatch)
    await make_admin()
    await login(client)
    assert (await client.post("/api/auth/2fa/setup", json={"password": "wrong"})).status_code == 403
    setup = (await client.post("/api/auth/2fa/setup", json={"password": PASSWORD})).json()
    assert setup["otpauth_uri"].startswith("otpauth://totp/") and setup["qr_png_base64"]
    secret = setup["secret"]
    totp = pyotp.TOTP(secret)
    assert (await client.post("/api/auth/2fa/enable", json={"code": "000000"})).status_code == 422
    code1 = totp.at(clock.now())
    assert (await client.post("/api/auth/2fa/enable", json={"code": code1})).json()["totp_enabled"] is True
    async with session_scope() as db:
        stored = (await db.execute(select(Admin))).scalar_one()
        assert secret not in (stored.totp_secret_enc or "")  # encrypted at rest

    # a new login now needs the second factor
    client.cookies.clear()
    first = await login(client)
    assert first.json()["status"] == "2fa_required" and security.SESSION_COOKIE not in client.cookies
    challenge = first.json()["challenge"]
    assert (await client.post("/api/auth/2fa", json={"challenge": challenge, "code": "123456"})).status_code == 401
    assert (await client.post("/api/auth/2fa", json={"challenge": "x" * 30, "code": code1})).status_code == 401
    # the code used for enrolment cannot be replayed to log in
    assert (await client.post("/api/auth/2fa", json={"challenge": challenge, "code": code1})).status_code == 401
    clock.advance(30)
    code2 = totp.at(clock.now())
    ok = await client.post("/api/auth/2fa", json={"challenge": challenge, "code": code2})
    assert ok.status_code == 200 and (await client.get("/api/auth/me")).status_code == 200

    # a code works only once
    client.cookies.clear()
    again = (await login(client)).json()["challenge"]
    assert (await client.post("/api/auth/2fa", json={"challenge": again, "code": code2})).status_code == 401
    clock.advance(30)
    assert (await client.post("/api/auth/2fa", json={"challenge": again, "code": totp.at(clock.now())})).status_code == 200

    # disabling needs the password and a fresh valid code
    assert (await client.post("/api/auth/2fa/disable", json={"password": PASSWORD, "code": "111111"})).status_code == 403
    clock.advance(30)
    off = await client.post("/api/auth/2fa/disable", json={"password": PASSWORD, "code": totp.at(clock.now())})
    assert off.status_code == 200 and off.json()["totp_enabled"] is False
    client.cookies.clear()
    assert (await login(client)).json()["status"] == "ok"  # password alone suffices again


async def test_totp_verifier_edge_cases():
    secret = pyotp.random_base32()
    totp = pyotp.TOTP(secret)
    t = 1_900_000_000.0
    step = int(t // 30)
    assert security.verify_totp(secret, totp.at(t), now=t) == step
    assert security.verify_totp(secret, totp.at(t - 30), now=t) == step - 1  # one step of clock drift
    assert security.verify_totp(secret, totp.at(t - 90), now=t) is None
    assert security.verify_totp(secret, totp.at(t), last_step=step, now=t) is None  # replay
    assert security.verify_totp(secret, totp.at(t), last_step=step - 1, now=t) == step
    assert security.verify_totp(secret, "12 34 56", now=t) is None
    assert security.verify_totp(secret, "", now=t) is None and security.verify_totp(secret, "abcdef", now=t) is None


async def test_admin_accounts_management(client):
    first = await make_admin("boss")
    await login(client)
    assert (await client.post("/api/admins", json={"username": "x", "password": PASSWORD})).status_code == 422
    assert (await client.post("/api/admins", json={"username": "second", "password": "weak"})).status_code == 422
    created = await client.post("/api/admins", json={"username": "Second.Admin", "password": "Second-Strong-55"})
    assert created.status_code == 201 and created.json()["username"] == "second.admin"
    assert (await client.post("/api/admins", json={"username": "second.admin", "password": "Second-Strong-55"})).status_code == 409
    listed = (await client.get("/api/admins")).json()["items"]
    assert [a["username"] for a in listed] == ["boss", "second.admin"] and "password_hash" not in str(listed)
    assert (await client.delete(f"/api/admins/{first}")).status_code == 409  # not yourself
    assert (await client.delete(f"/api/admins/{listed[1]['id']}")).status_code == 200
    assert (await client.delete("/api/admins/999")).status_code == 404


async def test_bootstrap_admin_is_created_once_from_the_environment(monkeypatch):
    from pydantic import SecretStr

    settings = get_settings()
    monkeypatch.setattr(settings, "admin_bootstrap_password", SecretStr("Bootstrap-Pass-99"))
    monkeypatch.setattr(settings, "admin_bootstrap_username", "Owner")
    assert await ensure_bootstrap_admin() is True
    assert await ensure_bootstrap_admin() is False  # only when no admin exists
    async with session_scope() as db:
        admin = (await db.execute(select(Admin))).scalar_one()
        assert admin.username == "owner" and security.verify_password(admin.password_hash, "Bootstrap-Pass-99")


async def test_weak_bootstrap_password_is_refused(monkeypatch):
    from pydantic import SecretStr

    monkeypatch.setattr(get_settings(), "admin_bootstrap_password", SecretStr("123"))
    assert await ensure_bootstrap_admin() is False
    async with session_scope() as db:
        assert (await db.execute(select(Admin))).first() is None


async def test_swagger_docs_are_off_in_production(monkeypatch):
    monkeypatch.setattr(get_settings(), "environment", "production")
    prod = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=prod), base_url="http://test") as c:
        assert (await c.get("/api/docs")).status_code == 404
        assert (await c.get("/api/openapi.json")).status_code == 404


async def test_admin_panel_static_files_are_served_when_configured(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<html><body>panel</body></html>")
    (tmp_path / "users").mkdir()
    (tmp_path / "users" / "index.html").write_text("<html><body>users page</body></html>")
    monkeypatch.setattr(get_settings(), "admin_static_dir", str(tmp_path))
    served = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=served), base_url="http://test") as c:
        assert "panel" in (await c.get("/")).text
        assert "users page" in (await c.get("/users/")).text
        assert (await c.get("/api/health")).status_code == 200  # the API still wins over the static mount
        assert (await c.get("/api/auth/me")).status_code == 401


async def test_security_headers_panel_is_unframeable_but_the_mini_app_may_be_framed_by_telegram(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<html><body>panel</body></html>")
    (tmp_path / "tz.html").write_text("<html><body>mini app</body></html>")
    monkeypatch.setattr(get_settings(), "admin_static_dir", str(tmp_path))
    served = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=served), base_url="http://test") as c:
        panel = await c.get("/")
        assert panel.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in panel.headers["content-security-policy"]
        assert "script-src 'self'" in panel.headers["content-security-policy"]

        mini = await c.get("/tz.html")
        assert mini.status_code == 200 and "mini app" in mini.text
        assert "x-frame-options" not in mini.headers  # DENY would blank the Mini App on Telegram Web
        csp = mini.headers["content-security-policy"]
        assert "frame-ancestors https://web.telegram.org" in csp
        # the page shares the panel's origin: it may load telegram.org's script but not talk to our API
        assert "connect-src 'none'" in csp and "default-src 'none'" in csp and "script-src 'unsafe-inline' https://telegram.org" in csp

        api = await c.get("/api/health")
        assert api.headers["x-frame-options"] == "DENY" and api.headers["cache-control"] == "no-store"
        assert "content-security-policy" not in api.headers

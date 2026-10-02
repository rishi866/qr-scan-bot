"""Admin-panel authentication primitives: Argon2 passwords, signed session tokens, TOTP 2FA."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import ipaddress
import threading
import time
from collections import defaultdict, deque

import jwt
import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Request

from app.config import get_settings
from app.timeutil import utcnow

_hasher = PasswordHasher()
MIN_PASSWORD_LENGTH = 12
SESSION_COOKIE = "qr_admin_session"
TOKEN_ACCESS = "access"  # noqa: S105 - token *type* label, not a secret
TOKEN_CHALLENGE = "2fa"  # noqa: S105
CHALLENGE_TTL_SECONDS = 300
TOTP_INTERVAL = 30


# ── passwords ───────────────────────────────────────────────────────────────


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def validate_password_strength(password: str, username: str = "") -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"The password must be at least {MIN_PASSWORD_LENGTH} characters long.")
    if password.isdigit() or password.isalpha():
        raise ValueError("Use a mix of letters and digits (or symbols).")
    if username and username.lower() in password.lower():
        raise ValueError("The password must not contain the username.")


# ── tokens ──────────────────────────────────────────────────────────────────


def _secret() -> str:
    return get_settings().secret_key.get_secret_value()


def create_token(admin_id: int, username: str, token_version: int, typ: str, ttl_seconds: int) -> str:
    now = utcnow()
    payload = {
        "sub": username,
        "aid": admin_id,
        "tv": token_version,
        "typ": typ,
        "iat": int(now.timestamp()),
        "exp": int((now + dt.timedelta(seconds=ttl_seconds)).timestamp()),
    }
    return jwt.encode(payload, _secret(), algorithm="HS256")


def decode_token(token: str, typ: str) -> dict | None:
    try:
        payload = jwt.decode(token, _secret(), algorithms=["HS256"], options={"require": ["exp", "iat", "sub", "aid", "typ"]})
    except jwt.PyJWTError:
        return None
    return payload if payload.get("typ") == typ else None


def session_ttl_seconds() -> int:
    return get_settings().session_ttl_hours * 3600


# ── TOTP (2FA) ──────────────────────────────────────────────────────────────


def _fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(b"totp-secret-key:" + _secret().encode()).digest())
    return Fernet(key)


def encrypt_secret(secret: str) -> str:
    return _fernet().encrypt(secret.encode()).decode()


def decrypt_secret(token: str) -> str | None:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return None


def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, username: str) -> str:
    return pyotp.TOTP(secret, interval=TOTP_INTERVAL).provisioning_uri(name=username, issuer_name=get_settings().totp_issuer)


def verify_totp(secret: str, code: str, last_step: int | None = None, now: float | None = None) -> int | None:
    """Return the matched time step (to store as ``last_totp_step``) or ``None``.

    Accepts the current code +/- one step for clock drift, and never the same step twice.
    """
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != 6:
        return None
    totp = pyotp.TOTP(secret, interval=TOTP_INTERVAL)
    current_step = int((now if now is not None else time.time()) // TOTP_INTERVAL)
    for step in (current_step, current_step - 1, current_step + 1):
        if hmac.compare_digest(totp.at(step * TOTP_INTERVAL), code):
            if last_step is not None and step <= last_step:
                return None  # replay of an already used code
            return step
    return None


# ── request helpers ─────────────────────────────────────────────────────────


def client_ip(request: Request) -> str:
    """The caller's IP. ``X-Forwarded-For`` is honoured only when the direct peer is a local proxy."""
    peer = request.client.host if request.client else ""
    try:
        trusted = ipaddress.ip_address(peer).is_private or ipaddress.ip_address(peer).is_loopback
    except ValueError:
        trusted = False
    forwarded = request.headers.get("x-forwarded-for", "")
    if trusted and forwarded:
        return forwarded.split(",")[0].strip()[:45]
    return peer[:45] or "unknown"


class SlidingWindowLimiter:
    """Tiny in-process limiter (the admin panel is a single-process app)."""

    def __init__(self, max_events: int, window_seconds: float):
        self.max_events = max_events
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str, now: float | None = None) -> bool:
        """Record an attempt; ``False`` means the limit is exceeded."""
        now = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= self.max_events:
                return False
            hits.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


login_limiter = SlidingWindowLimiter(max_events=15, window_seconds=60)

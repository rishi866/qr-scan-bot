"""The operations CLI: first admin creation and the lost-authenticator recovery path."""

from __future__ import annotations

import argparse

from sqlalchemy import select

from app import cli
from app import db as app_db
from app.api import security
from app.db import session_scope
from app.models import Admin

PASSWORD = "Correct-Horse-Battery-77"


async def _keep_engine(monkeypatch):
    """The CLI disposes the global engine when it is done; the test session shares it."""

    async def noop() -> None:
        return None

    monkeypatch.setattr(app_db, "dispose_engine", noop)


async def _admin(username: str) -> Admin | None:
    async with session_scope() as db:
        return (await db.execute(select(Admin).where(Admin.username == username))).scalar_one_or_none()


async def test_create_admin_hashes_the_password_and_refuses_duplicates(monkeypatch, capsys):
    await _keep_engine(monkeypatch)
    assert await cli._create_admin("Owner", PASSWORD, reset=False) == 0
    admin = await _admin("owner")  # usernames are normalised to lower case
    assert admin is not None and admin.password_hash != PASSWORD
    assert security.verify_password(admin.password_hash, PASSWORD)

    assert await cli._create_admin("owner", PASSWORD, reset=False) == 1
    assert "already exists" in capsys.readouterr().out


async def test_create_admin_enforces_the_password_rules(monkeypatch, capsys):
    await _keep_engine(monkeypatch)
    assert await cli._create_admin("owner", "short", reset=False) == 1
    assert await cli._create_admin("owner", "owner-owner-owner-1A", reset=False) == 1  # contains the username
    assert await _admin("owner") is None
    assert "❌" in capsys.readouterr().out


async def test_reset_signs_everything_out_and_can_switch_two_factor_off(monkeypatch, capsys):
    await _keep_engine(monkeypatch)
    await cli._create_admin("owner", PASSWORD, reset=False)
    async with session_scope() as db:  # pretend the admin enrolled an authenticator and got locked out
        admin = (await db.execute(select(Admin).where(Admin.username == "owner"))).scalar_one()
        admin.totp_enabled = True
        admin.totp_secret_enc = security.encrypt_secret(security.new_totp_secret())
        admin.last_totp_step = 123
        admin.failed_attempts = 5
        before = admin.token_version

    # a plain reset keeps 2FA (the password alone must not bypass it)
    assert await cli._create_admin("owner", "Another-Strong-Pass-88", reset=True) == 0
    admin = await _admin("owner")
    assert admin.totp_enabled and admin.totp_secret_enc and admin.failed_attempts == 0
    assert admin.token_version == before + 1
    assert security.verify_password(admin.password_hash, "Another-Strong-Pass-88")

    # the explicit recovery flag turns it off
    assert await cli._create_admin("owner", "Third-Strong-Pass-99", reset=True, disable_2fa=True) == 0
    admin = await _admin("owner")
    assert not admin.totp_enabled and admin.totp_secret_enc is None and admin.last_totp_step is None
    assert "two-factor authentication was switched off" in capsys.readouterr().out


def test_disable_2fa_requires_reset(capsys):
    args = argparse.Namespace(password=PASSWORD, username="owner", reset=False, disable_2fa=True)
    assert cli.cmd_create_admin(args) == 1
    assert "--disable-2fa only works together with --reset" in capsys.readouterr().out

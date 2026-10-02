"""Blockchain side, exercised against a real in-memory EVM with a real ERC-20 contract."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
from decimal import Decimal
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.chain import binance as binance_mod
from app.chain import payout as payout_mod
from app.chain.bsc import ChainError, Receipt, TransferLog, is_valid_address
from app.chain.hd import HDError, HDWallet, generate_mnemonic
from app.chain.payout import run_payouts
from app.chain.sweeper import sweep
from app.chain.watcher import get_cursor, scan_deposits, set_cursor
from app.config import get_settings
from app.db import session_scope
from app.enums import DepositStatus, WithdrawalStatus
from app.models import Deposit, Outbox, Withdrawal
from app.services import payments, settings_service, wallet
from app.services import users as users_mod
from tests.chain_env import Env, make_env
from tests.factories import fund, make_scanner, make_user

# the well-known Hardhat/Anvil test mnemonic: its first addresses are published, so this proves
# our derivation is compatible with MetaMask / Trust Wallet (m/44'/60'/0'/0/i)
MNEMONIC = "test test test test test test test test test test test junk"
HARDHAT_0 = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
HARDHAT_1 = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"


@pytest.fixture
def env(monkeypatch) -> Env:
    payout_mod._retry_after.clear()  # module-level cooldown must not leak between tests
    settings = get_settings()
    monkeypatch.setattr(settings, "deposit_confirmations", 2)
    monkeypatch.setattr(settings, "bsc_scan_chunk", 50)
    monkeypatch.setattr(settings, "bsc_scan_start_block", None)
    return make_env()


# ── HD wallet ───────────────────────────────────────────────────────────────


def test_hd_matches_published_wallet_vectors():
    hd = HDWallet(mnemonic=MNEMONIC)
    assert hd.address(0) == HARDHAT_0
    assert hd.address(1) == HARDHAT_1
    assert hd.can_sign


def test_watch_only_xpub_derives_the_same_addresses_but_cannot_sign():
    full = HDWallet(mnemonic=MNEMONIC)
    watch = HDWallet(xpub=full.xpub)
    assert [watch.address(i) for i in range(5)] == [full.address(i) for i in range(5)]
    assert not watch.can_sign
    with pytest.raises(HDError):
        watch.private_key(0)


def test_hd_rejects_bad_input():
    with pytest.raises(HDError):
        HDWallet()
    with pytest.raises(HDError):
        HDWallet(mnemonic="not a valid mnemonic at all")
    with pytest.raises(HDError):
        HDWallet(xpub="xpub-garbage")
    other = HDWallet(mnemonic=generate_mnemonic())
    with pytest.raises(HDError):
        HDWallet(mnemonic=MNEMONIC, xpub=other.xpub)
    with pytest.raises(HDError):
        HDWallet(mnemonic=MNEMONIC).address(-1)
    assert len(generate_mnemonic(24).split()) == 24


def test_address_validation():
    assert is_valid_address(HARDHAT_0)
    assert is_valid_address(HARDHAT_0.lower())
    assert not is_valid_address(HARDHAT_0[:-1] + "0")  # broken EIP-55 checksum
    assert not is_valid_address("0x123")
    assert not is_valid_address("")
    assert not is_valid_address("f39Fd6e51aad88F6F4ce6aB8827279cffFb92266")  # no 0x


# ── watcher ─────────────────────────────────────────────────────────────────


async def _seller_with_address(db, hd: HDWallet):
    seller = await make_user(db, role="seller")
    address = (await payments.get_or_create_deposit_address(db, seller, hd)).address
    await db.commit()
    return seller, address


async def test_deposit_address_is_unique_stable_and_checked(db):
    hd = HDWallet(mnemonic=MNEMONIC)
    a = await make_user(db, role="seller")
    b = await make_user(db, role="seller")
    addr_a = (await payments.get_or_create_deposit_address(db, a, hd)).address
    addr_b = (await payments.get_or_create_deposit_address(db, b, hd)).address
    await db.commit()
    assert addr_a != addr_b
    assert (await payments.get_or_create_deposit_address(db, a, hd)).address == addr_a  # stable
    assert addr_a == hd.address(a.user_id)  # index == user id -> recoverable from the seed alone
    # a different seed must be refused instead of silently handing out unrelated addresses
    with pytest.raises(payments.PaymentError):
        await payments.get_or_create_deposit_address(db, a, HDWallet(mnemonic=generate_mnemonic()))
    assert await payments.verify_hd_consistency(db, hd) == []
    assert len(await payments.verify_hd_consistency(db, HDWallet(mnemonic=generate_mnemonic()))) == 2


async def test_watcher_credits_after_confirmations_exactly_once(db, env):
    hd = HDWallet(mnemonic=MNEMONIC)
    seller, address = await _seller_with_address(db, hd)
    seller_id, seller_tg = seller.user_id, seller.telegram_id

    first = await scan_deposits(env.chain)  # first run only sets the cursor
    assert first.credited == 0

    tx = env.send_token(address, 25)
    early = await scan_deposits(env.chain)
    assert early.credited == 0  # not enough confirmations yet
    env.mine(3)
    done = await scan_deposits(env.chain)
    assert done.credited == 1

    w = await wallet.get_wallet(db, seller_id)
    assert w.balance == Decimal("25") and w.total_deposited == Decimal("25")
    dep = (await db.execute(select(Deposit))).scalar_one()
    assert dep.status == DepositStatus.CREDITED.value and dep.tx_hash == tx and dep.address == address
    notes = (await db.execute(select(Outbox).where(Outbox.chat_id == seller_tg))).scalars().all()
    assert any("Deposit received" in n.body and "25.00 USDT" in n.body for n in notes)

    # nothing new -> nothing happens; and a forced re-scan of the same blocks can never double credit
    assert (await scan_deposits(env.chain)).credited == 0
    await db.rollback()
    async with session_scope() as other:
        await set_cursor(other, 0)
    again = await scan_deposits(env.chain)
    assert again.credited == 0 and again.duplicates == 1
    w = await wallet.get_wallet(db, seller_id)
    assert w.balance == Decimal("25")
    assert (await wallet.reconcile(db)).ok


async def test_watcher_handles_multiple_users_amounts_and_noise(db, env):
    hd = HDWallet(mnemonic=MNEMONIC)
    s1, a1 = await _seller_with_address(db, hd)
    s2, a2 = await _seller_with_address(db, hd)
    await scan_deposits(env.chain)

    env.send_token(a1, 10)
    env.send_token(a2, 0.5)  # below the 1 USDT minimum
    env.send_token(a1, 0, raw=7)  # dust (7e-18 token) -> ignored
    env.send_token("0x" + "ab" * 20, 99)  # an address we do not own -> ignored
    env.mine(3)
    result = await scan_deposits(env.chain)
    assert result.credited == 2 and result.dust == 1

    assert (await wallet.get_wallet(db, s1.user_id)).balance == Decimal("10")
    assert (await wallet.get_wallet(db, s2.user_id)).balance == Decimal("0")  # held for review
    low = (await db.execute(select(Deposit).where(Deposit.user_id == s2.user_id))).scalar_one()
    assert low.status == DepositStatus.BELOW_MIN.value
    admin_msgs = (await db.execute(select(Outbox).where(Outbox.chat_id.in_([9001, 9002])))).scalars().all()
    assert any("Below-minimum" in m.body for m in admin_msgs)

    # admin credits the small deposit manually
    await payments.confirm_deposit(db, low.id, "admin")
    await db.commit()
    assert (await wallet.get_wallet(db, s2.user_id)).balance == Decimal("0.5")
    assert (await wallet.reconcile(db)).ok


async def test_watcher_scans_in_chunks_and_resumes_from_cursor(db, env):
    hd = HDWallet(mnemonic=MNEMONIC)
    seller, address = await _seller_with_address(db, hd)
    await scan_deposits(env.chain)
    start_cursor = None
    async with session_scope() as other:
        start_cursor = await get_cursor(other)
    env.send_token(address, 5)
    env.mine(120)  # more blocks than one chunk (50)
    result = await scan_deposits(env.chain)
    assert result.credited == 1
    async with session_scope() as other:
        assert await get_cursor(other) == env.client.latest_block() - get_settings().deposit_confirmations
    assert start_cursor is not None
    assert (await wallet.get_wallet(db, seller.user_id)).balance == Decimal("5")


# ── payouts ─────────────────────────────────────────────────────────────────


async def _approved_withdrawal(db, env, *, amount="4", funds="10", dest=None):
    scanner = await make_scanner(db, alias="user1")
    await fund(db, scanner, funds)
    await db.commit()
    dest = dest or HARDHAT_1
    wd = await payments.request_withdrawal(db, scanner, "bep20", dest, Decimal(amount))
    await db.commit()
    await payments.approve_withdrawal(db, wd.id, "admin")
    await db.commit()
    return scanner, wd.id, dest


def _enable_auto_payout(monkeypatch, env: Env, *, max_amount="50", tokens=1000.0):
    hot = env.new_funded_account(tokens=tokens)
    settings = get_settings()
    monkeypatch.setattr(settings, "auto_payout_enabled", True)
    monkeypatch.setattr(settings, "auto_payout_max_amount", Decimal(max_amount))
    monkeypatch.setattr(settings, "payout_private_key", SecretStr(hot.key.hex()))
    return hot


async def test_auto_payout_end_to_end_with_fee(db, env, monkeypatch):
    _enable_auto_payout(monkeypatch, env)
    await settings_service.save(db, {"withdrawal_fee": "0.1"}, "t")
    await db.commit()
    scanner, wid, dest = await _approved_withdrawal(db, env, amount="4")

    sent = await run_payouts(env.chain)
    assert sent.sent == [wid]
    wd = (await db.execute(select(Withdrawal).execution_options(populate_existing=True))).scalar_one()
    assert wd.status == WithdrawalStatus.PROCESSING.value and wd.tx_hash
    assert env.token_balance(dest) == pytest.approx(3.9)  # amount - fee

    env.mine(3)
    done = await run_payouts(env.chain)
    assert done.completed == [wid]
    wd = (await db.execute(select(Withdrawal).execution_options(populate_existing=True))).scalar_one()
    assert wd.status == WithdrawalStatus.COMPLETED.value and wd.processed_by == "admin"
    w = await wallet.get_wallet(db, scanner.user_id)
    assert (w.balance, w.pending, w.total_withdrawn) == (Decimal("6"), Decimal("0"), Decimal("4"))
    msgs = (await db.execute(select(Outbox).where(Outbox.chat_id == scanner.telegram_id))).scalars().all()
    assert any("Withdrawal #" in m.body and "paid" in m.body for m in msgs)
    assert (await wallet.reconcile(db)).ok

    # running again must never pay twice
    assert (await run_payouts(env.chain)).sent == []
    assert env.token_balance(dest) == pytest.approx(3.9)


async def test_payout_over_the_limit_is_left_for_manual_payment(db, env, monkeypatch):
    _enable_auto_payout(monkeypatch, env, max_amount="2")
    _, wid, dest = await _approved_withdrawal(db, env, amount="4")
    result = await run_payouts(env.chain)
    assert result.skipped == [wid] and result.sent == []
    assert env.token_balance(dest) == 0
    wd = (await db.execute(select(Withdrawal).execution_options(populate_existing=True))).scalar_one()
    assert wd.status == WithdrawalStatus.APPROVED.value


async def test_payout_disabled_by_default(db, env):
    _, wid, dest = await _approved_withdrawal(db, env)
    assert (await run_payouts(env.chain)).sent == []
    assert env.token_balance(dest) == 0


async def test_empty_hot_wallet_postpones_and_alerts_once(db, env, monkeypatch):
    _enable_auto_payout(monkeypatch, env, tokens=0)  # hot wallet has gas but no tokens
    _, wid, dest = await _approved_withdrawal(db, env)
    first = await run_payouts(env.chain)
    assert first.sent == [] and first.failed == []
    wd = (await db.execute(select(Withdrawal).execution_options(populate_existing=True))).scalar_one()
    assert wd.status == WithdrawalStatus.APPROVED.value and "postponed" in (wd.admin_note or "")
    alerts = (await db.execute(select(Outbox).where(Outbox.chat_id.in_([9001, 9002]), Outbox.body.like("%needs attention%")))).scalars().all()
    assert len(alerts) == 2
    await run_payouts(env.chain)  # cooldown: no retry storm, no duplicate alert
    alerts = (await db.execute(select(Outbox).where(Outbox.chat_id.in_([9001, 9002]), Outbox.body.like("%needs attention%")))).scalars().all()
    assert len(alerts) == 2


async def test_ambiguous_send_failure_is_never_retried_automatically(db, env, monkeypatch):
    _enable_auto_payout(monkeypatch, env)
    _, wid, dest = await _approved_withdrawal(db, env)

    def boom(_signed):
        raise TimeoutError("rpc timed out after sending")

    monkeypatch.setattr(env.client, "_broadcast", boom)
    result = await run_payouts(env.chain)
    assert result.failed == [wid]
    wd = (await db.execute(select(Withdrawal).execution_options(populate_existing=True))).scalar_one()
    assert wd.status == WithdrawalStatus.FAILED.value and "unclear" in wd.admin_note
    assert (await run_payouts(env.chain)).sent == []  # stays failed until an admin decides
    # admin verified nothing was sent and retries
    await payments.retry_withdrawal(db, wid, "admin")
    await db.commit()
    wd = (await db.execute(select(Withdrawal).execution_options(populate_existing=True))).scalar_one()
    assert wd.status == WithdrawalStatus.APPROVED.value


async def test_reverted_payout_transaction_is_flagged_failed(db, env, monkeypatch):
    _enable_auto_payout(monkeypatch, env)
    _, wid, dest = await _approved_withdrawal(db, env)
    await run_payouts(env.chain)
    monkeypatch.setattr(env.client, "receipt", lambda h: Receipt(status=0, block_number=1))
    result = await run_payouts(env.chain)
    assert result.failed == [wid]
    w = await wallet.get_wallet(db, (await db.execute(select(Withdrawal.user_id))).scalar_one())
    assert w.pending == Decimal("4")  # still locked, nothing paid out in the books


async def test_crash_between_claim_and_hash_is_escalated(db, env, monkeypatch):
    _enable_auto_payout(monkeypatch, env)
    _, wid, dest = await _approved_withdrawal(db, env)
    async with session_scope() as other:
        wd = await payments.claim_for_payout(other, wid)
        wd.processed_at = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)
    result = await run_payouts(env.chain)
    assert result.failed == [wid] and result.sent == []
    assert env.token_balance(dest) == 0


async def test_manual_withdrawal_flow_and_rejection_returns_funds(db):
    scanner = await make_scanner(db, alias="user1")
    await fund(db, scanner, "10")
    await db.commit()
    sid = scanner.user_id
    wd1 = await payments.request_withdrawal(db, scanner, "bep20", HARDHAT_1, Decimal("3"))
    await db.commit()
    wd1_id = wd1.id
    assert (await wallet.get_wallet(db, sid)).pending == Decimal("3")
    with pytest.raises(payments.PaymentError):  # one open withdrawal at a time
        await payments.request_withdrawal(db, scanner, "bep20", HARDHAT_1, Decimal("1"))
    await db.rollback()
    scanner = await users_mod.require_user(db, sid)
    await payments.reject_withdrawal(db, wd1_id, "admin", "wrong address")
    await db.commit()
    w = await wallet.get_wallet(db, sid)
    assert (w.balance, w.pending) == (Decimal("10"), Decimal("0"))
    with pytest.raises(payments.PaymentError):
        await payments.complete_withdrawal(db, wd1_id, "admin", "0xabc")  # already rejected
    await db.rollback()
    scanner = await users_mod.require_user(db, sid)

    wd2 = await payments.request_withdrawal(db, scanner, "binance", "12345678", Decimal("5"))
    await db.commit()
    wd2_id = wd2.id
    await payments.complete_withdrawal(db, wd2_id, "admin", "BinancePay-ref-1")
    await db.commit()
    w = await wallet.get_wallet(db, sid)
    assert (w.balance, w.pending, w.total_withdrawn) == (Decimal("5"), Decimal("0"), Decimal("5"))
    assert (await wallet.reconcile(db)).ok


@pytest.mark.parametrize(
    ("method", "address", "amount"),
    [
        ("bep20", "0x123", "5"),
        ("bep20", HARDHAT_1[:-1] + "0", "5"),  # bad checksum
        ("binance", "abc", "5"),
        ("binance", "12345678", "0.5"),  # below minimum 1
        ("binance", "12345678", "500"),  # more than balance
        ("paypal", "x", "5"),
    ],
)
async def test_withdrawal_validation(db, method, address, amount):
    scanner = await make_scanner(db, alias="user1")
    await fund(db, scanner, "10")
    await db.commit()
    with pytest.raises(payments.PaymentError):
        await payments.request_withdrawal(db, scanner, method, address, Decimal(amount))
    await db.rollback()


async def test_only_scanners_can_withdraw(db):
    seller = await make_user(db, role="seller")
    await fund(db, seller, "10")
    await db.commit()
    with pytest.raises(payments.PaymentError):
        await payments.request_withdrawal(db, seller, "bep20", HARDHAT_1, Decimal("5"))
    await db.rollback()


async def test_admin_adjustment_needs_reason_and_cannot_overdraw(db):
    user = await make_user(db, role="seller")
    await db.commit()
    uid = user.user_id
    with pytest.raises(payments.PaymentError):
        await payments.adjust_wallet(db, uid, Decimal("5"), "  ", "admin")
    await db.rollback()
    await payments.adjust_wallet(db, uid, Decimal("5"), "goodwill credit", "admin")
    await db.commit()
    with pytest.raises(payments.PaymentError):
        await payments.adjust_wallet(db, uid, Decimal("-6"), "oops", "admin")
    await db.rollback()
    await payments.adjust_wallet(db, uid, Decimal("-2"), "correction", "admin")
    await db.commit()
    assert (await wallet.get_wallet(db, uid)).balance == Decimal("3")
    assert (await wallet.reconcile(db)).ok  # adjustments are legitimate and tracked separately


# ── sweeper ─────────────────────────────────────────────────────────────────


async def test_sweeper_moves_deposits_to_treasury_with_gas_topup(db, env):
    hd = HDWallet(mnemonic=MNEMONIC)
    _, a1 = await _seller_with_address(db, hd)
    _, a2 = await _seller_with_address(db, hd)
    env.send_token(a1, 30)
    env.send_token(a2, 0.5)  # below the sweep minimum
    gas = env.new_funded_account(bnb_wei=10**18)
    treasury = HARDHAT_0

    plan = await sweep(env.chain, hd, treasury, gas.key.hex(), min_amount=Decimal(1), dry_run=True)
    assert [(a.address, a.status) for a in plan] == [(a1, "planned")]
    assert env.token_balance(treasury) == 0  # dry run moves nothing

    done = await sweep(env.chain, hd, treasury, gas.key.hex(), min_amount=Decimal(1), dry_run=False)
    assert [a.status for a in done] == ["swept"] and done[0].gas_tx_hash and done[0].tx_hash
    assert env.token_balance(treasury) == 30 and env.token_balance(a1) == 0 and env.token_balance(a2) == 0.5

    with pytest.raises(ChainError):  # watch-only wallets cannot sweep
        await sweep(env.chain, HDWallet(xpub=hd.xpub), treasury, gas.key.hex(), dry_run=False)
    with pytest.raises(ChainError):
        await sweep(env.chain, hd, "0x" + "0" * 40, gas.key.hex(), dry_run=False)


# ── transfer-log parsing ────────────────────────────────────────────────────


async def test_get_transfers_ignores_nft_style_events_and_other_recipients(env):
    a = env.new_funded_account()
    b = env.new_funded_account()
    env.send_token(a.address, 3)
    env.send_token(b.address, 4)
    logs = env.client.get_transfers(0, env.client.latest_block(), [a.address])
    assert [isinstance(x, TransferLog) for x in logs] == [True]
    assert logs[0].to_address == a.address and logs[0].value_raw == 3 * 10**18
    assert env.client.get_transfers(0, env.client.latest_block(), []) == []


# ── Binance ─────────────────────────────────────────────────────────────────

API_SECRET = "secret-key"


def _binance_transport(pay_items=None, deposit_items=None, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        parts = urlsplit(str(request.url))
        query = parts.query
        params = dict(parse_qsl(query))
        signature = params.pop("signature")
        unsigned = query[: query.rindex("&signature=")]
        expected = hmac.new(API_SECRET.encode(), unsigned.encode(), hashlib.sha256).hexdigest()
        assert signature == expected, "request signature is wrong"
        assert request.headers["X-MBX-APIKEY"] == "api-key"
        assert "timestamp" in params
        if seen is not None:
            seen.append(parts.path)
        if parts.path == "/sapi/v1/pay/transactions":
            return httpx.Response(200, json={"code": "000000", "data": pay_items or [], "success": True})
        if parts.path == "/sapi/v1/capital/deposit/hisrec":
            return httpx.Response(200, json=deposit_items or [])
        return httpx.Response(404, text="nope")

    return httpx.MockTransport(handler)


def _client(pay_items=None, deposit_items=None, seen=None) -> binance_mod.BinanceClient:
    return binance_mod.BinanceClient(
        "api-key", API_SECRET, "https://binance.test", httpx.AsyncClient(transport=_binance_transport(pay_items, deposit_items, seen))
    )


async def test_binance_signed_requests_and_lookup():
    seen: list[str] = []
    pay = [{"transactionId": "M_P_111", "amount": "10.00000000", "currency": "USDT", "payerInfo": {"binanceId": "555"}}]
    client = _client(pay_items=pay, seen=seen)
    found = await client.find_payment("m_p_111")  # case-insensitive
    assert found and found.amount == Decimal("10") and found.payer_id == "555" and found.source == "pay"
    assert await client.find_payment("M_P_999") is None
    assert "/sapi/v1/pay/transactions" in seen

    dep = [{"txId": "Off-chain transfer 987654", "amount": "7.5", "coin": "USDT", "status": 1}]
    found = await _client(deposit_items=dep).find_payment("987654")
    assert found and found.amount == Decimal("7.5") and found.source == "deposit"
    # outgoing / malformed rows are never treated as a deposit
    assert await _client(pay_items=[{"transactionId": "X1", "amount": "-5", "currency": "USDT"}]).find_payment("X1") is None
    assert await _client(pay_items=[{"transactionId": "X2", "amount": "abc", "currency": "USDT"}]).find_payment("X2") is None


async def test_binance_http_error_is_reported():
    client = binance_mod.BinanceClient("api-key", API_SECRET, "https://binance.test", httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401, text="bad key"))))
    with pytest.raises(binance_mod.BinanceError):
        await client.find_payment("abc123")


async def test_binance_claims_autocredit_only_on_exact_match(db):
    user = await make_user(db, role="seller")
    other = await make_user(db, role="seller")
    await db.commit()
    user_id, other_id = user.user_id, other.user_id
    good = await payments.create_binance_claim(db, user, Decimal("10"), "M_P_111")
    wrong_amount = await payments.create_binance_claim(db, other, Decimal("20"), "M_P_222")
    missing = await payments.create_binance_claim(db, other, Decimal("5"), "M_P_333")
    await db.commit()
    good_id, wrong_id, missing_id = good.id, wrong_amount.id, missing.id
    with pytest.raises(payments.PaymentError):  # same reference cannot be claimed twice
        await payments.create_binance_claim(db, await users_mod.require_user(db, other_id), Decimal("10"), "M_P_111")
    await db.rollback()
    with pytest.raises(payments.PaymentError):
        await payments.create_binance_claim(db, await users_mod.require_user(db, other_id), Decimal("10"), "bad ref!")
    await db.rollback()

    pay = [
        {"transactionId": "M_P_111", "amount": "10", "currency": "USDT"},
        {"transactionId": "M_P_222", "amount": "19", "currency": "USDT"},
    ]
    stats = await binance_mod.poll_binance_claims(_client(pay_items=pay))
    assert stats == {"checked": 3, "credited": 1, "flagged": 1}

    rows = {d.id: d for d in (await db.execute(select(Deposit).execution_options(populate_existing=True))).scalars()}
    assert rows[good_id].status == DepositStatus.CREDITED.value
    assert rows[wrong_id].status == DepositStatus.PENDING.value and "amount" in rows[wrong_id].note
    assert rows[missing_id].status == DepositStatus.PENDING.value
    assert (await wallet.get_wallet(db, user_id)).balance == Decimal("10")
    assert (await wallet.get_wallet(db, other_id)).balance == Decimal("0")

    # polling again must not credit twice
    stats = await binance_mod.poll_binance_claims(_client(pay_items=pay))
    assert stats["credited"] == 0
    assert (await wallet.get_wallet(db, user_id)).balance == Decimal("10")
    # the admin can still confirm the remaining claim by hand (with a corrected amount)
    await payments.confirm_deposit(db, wrong_id, "admin", amount=Decimal("19"))
    await db.commit()
    assert (await wallet.get_wallet(db, other_id)).balance == Decimal("19")
    assert (await wallet.reconcile(db)).ok

from decimal import Decimal

import pytest

from app import money
from app.services import settings_service
from app.services.settings_service import DEFAULT_URL_DOMAINS, SettingsError
from app.services.urls import InvalidUrl, validate_chatgpt_url

GOOD = [
    "https://chatgpt.com/checkout/openai_llc/cs_live_abc123",
    "https://chatgpt.com/checkout/openai_llc/cs_live_abc#fidkdWxOYHwnPyd1blpxYHZxWjA0",
    "https://pay.openai.com/c/pay/cs_live_a1B2#fid123",
    "https://chat.openai.com/share/abc",
    "  https://chatgpt.com/  ",
    "https://CHATGPT.COM/x",
    "https://chatgpt.com:443/x",
]

BAD = [
    "",
    "   ",
    "chatgpt.com/checkout",  # no scheme
    "http://chatgpt.com/x",  # not https
    "https://chatgpt.com.evil.com/x",  # look-alike suffix
    "https://evilchatgpt.com/x",
    "https://evil.com/?u=https://chatgpt.com/x",
    "https://chatgpt.com@evil.com/x",  # userinfo trick
    "https://user:pass@chatgpt.com/x",
    "https://chatgpt.com:8443/x",
    "https://chatgpt.com/x https://evil.com",  # two links
    "https://chatgpt.com/x\nhttps://evil.com",
    "https://chatgpt.com/x​",  # zero width char
    "https://chаtgpt.com/x",  # cyrillic 'а'
    "javascript:alert(1)",
    "https://localhost/x",
    "https://" + "a" * 3000 + ".chatgpt.com",
    "https://chatgpt.com:99999999/x",
    "ftp://chatgpt.com/x",
    "https://.chatgpt.com/",
]


@pytest.mark.parametrize("url", GOOD)
def test_valid_urls(url):
    assert validate_chatgpt_url(url, DEFAULT_URL_DOMAINS) == url.strip()


@pytest.mark.parametrize("url", BAD)
def test_invalid_urls(url):
    with pytest.raises(InvalidUrl):
        validate_chatgpt_url(url, DEFAULT_URL_DOMAINS)


def test_custom_domain_list():
    assert validate_chatgpt_url("https://checkout.stripe.com/c/pay/x", ["stripe.com"])
    with pytest.raises(InvalidUrl):
        validate_chatgpt_url("https://chatgpt.com/x", ["stripe.com"])


def test_money_helpers():
    assert money.fmt_money("0.5005") == "0.5005"
    assert money.fmt_money(10) == "10.00"
    assert money.fmt_money(Decimal("1234.50000000")) == "1234.50"
    assert money.fmt_money("0.00000001") == "0.00000001"
    assert money.percent_of(Decimal("0.5"), Decimal("0.1")) == Decimal("0.00050000")
    assert money.to_decimal("1,5") == Decimal("1.5")
    for bad in ("abc", "NaN", "inf", True, ""):
        with pytest.raises(ValueError):
            money.to_decimal(bad)
    assert money.to_raw(Decimal("1.5"), 18) == 1_500_000_000_000_000_000
    assert money.from_raw(1_500_000_000_000_000_000, 18) == Decimal("1.5")
    with pytest.raises(ValueError):
        money.to_raw(Decimal("0.0000000000000000001"), 18)


async def test_settings_defaults_and_save(db):
    cfg = await settings_service.load(db)
    assert cfg.task_amount == Decimal("0.5")
    assert cfg.commission_percent == Decimal("0.1")
    assert cfg.commission_per_task == Decimal("0.0005")
    assert cfg.seller_cost == Decimal("0.5005")
    assert cfg.slot_duration_hours == 2
    assert cfg.allowed_url_domains == DEFAULT_URL_DOMAINS

    cfg = await settings_service.save(
        db, {"task_amount": "1", "commission_percent": "2.5", "allowed_url_domains": "ChatGPT.com, openai.com\nfoo.org"}, "tester"
    )
    assert cfg.seller_cost == Decimal("1.025")
    assert cfg.allowed_url_domains == ["chatgpt.com", "openai.com", "foo.org"]
    await db.commit()
    assert (await settings_service.load(db)).task_amount == Decimal("1")


@pytest.mark.parametrize(
    "updates",
    [
        {"nope": 1},
        {"task_amount": "-1"},
        {"task_amount": "abc"},
        {"commission_percent": "99"},
        {"slot_duration_hours": 5},  # does not divide 24
        {"url_response_timeout_seconds": 1},
        {"url_response_timeout_seconds": "12.5"},
        {"allowed_url_domains": "not a domain"},
        {"allowed_url_domains": ""},
        {"auto_reputation": "maybe"},
    ],
)
async def test_settings_validation_rejects_bad_input(db, updates):
    with pytest.raises(SettingsError):
        await settings_service.save(db, updates, "tester")


async def test_settings_save_is_all_or_nothing(db):
    with pytest.raises(SettingsError):
        await settings_service.save(db, {"task_amount": "2", "commission_percent": "-5"}, "tester")
    await db.rollback()
    assert (await settings_service.load(db)).task_amount == Decimal("0.5")


async def test_corrupt_stored_value_falls_back_to_default(db):
    from app.models import Setting

    db.add(Setting(key="task_amount", value="garbage"))
    await db.commit()
    assert (await settings_service.load(db)).task_amount == Decimal("0.5")

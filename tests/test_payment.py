import pytest

from app.payment import (
    PAYMENT_EIP712_NAME,
    PAYMENT_EIP712_VERSION,
    build_payment_option,
    load_payment_rail,
    paywall_required,
)

VALID_ENV = {"PAY_TO_ADDRESS": "0xf4c9fa07f3bb852547fdc4df7c1d9fd9991cfa51"}


def test_load_payment_rail_accepts_valid_env():
    rail = load_payment_rail(VALID_ENV)
    assert rail.pay_to == VALID_ENV["PAY_TO_ADDRESS"]
    assert rail.network == "eip155:196"
    assert rail.asset == "0x779ded0c9e1022225f8e0630b35a9b54be713736"
    assert rail.facilitator_url == "https://web3.okx.com"


def test_load_payment_rail_rejects_bad_address():
    with pytest.raises(ValueError):
        load_payment_rail({"PAY_TO_ADDRESS": "0x1234"})


def test_load_payment_rail_rejects_zero_address():
    with pytest.raises(ValueError):
        load_payment_rail({"PAY_TO_ADDRESS": "0x" + "0" * 40})


def test_load_payment_rail_rejects_unknown_override():
    env = dict(VALID_ENV, TILLA_PAYMENT_BOGUS="x")
    with pytest.raises(ValueError):
        load_payment_rail(env)


def test_load_payment_rail_rejects_wrong_base_url():
    env = dict(VALID_ENV, OKX_BASE_URL="https://evil.example")
    with pytest.raises(ValueError):
        load_payment_rail(env)


def test_paywall_required_true():
    assert paywall_required({"TILLA_REQUIRE_PAYWALL": "true"}) is True


def test_paywall_required_false():
    assert paywall_required({"TILLA_REQUIRE_PAYWALL": "off"}) is False


def test_paywall_required_invalid():
    with pytest.raises(ValueError):
        paywall_required({"TILLA_REQUIRE_PAYWALL": "maybe"})


def test_build_payment_option_field_mapping():
    rail = load_payment_rail(VALID_ENV)
    opt = build_payment_option(rail)
    assert opt.scheme == "exact"
    assert opt.network == "eip155:196"
    assert opt.pay_to == VALID_ENV["PAY_TO_ADDRESS"]
    assert opt.max_timeout_seconds == 300
    assert opt.price.amount == "50000"
    assert opt.price.asset == "0x779ded0c9e1022225f8e0630b35a9b54be713736"
    assert opt.price.extra == {
        "name": PAYMENT_EIP712_NAME,
        "version": PAYMENT_EIP712_VERSION,
    }

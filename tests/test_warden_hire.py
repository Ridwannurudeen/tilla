"""M10 Warden PAID-hire client tests (no network, no funds, no on-chain).

Everything is respx-mocked. Asserts the funds-safety invariants: the client pins
scheme/asset/network/payTo/amount and REFUSES to sign a mismatched challenge; it
signs at most once and never retries a signed authorization; any failure degrades to
the free demo scan. The default (flag off) path never even constructs the signing
code and only ever calls the demo endpoint.
"""

import httpx
import pytest
import respx
from x402.http.constants import PAYMENT_RESPONSE_HEADER, PAYMENT_SIGNATURE_HEADER
from x402.http.utils import (
    PAYMENT_REQUIRED_HEADER,
    decode_payment_signature_header,
    encode_payment_required_header,
    encode_payment_response_header,
)
from x402.schemas import PaymentRequired, PaymentRequirements, SettleResponse

from app import config, screening, warden_hire
from app.config import WARDEN_SCREEN_URL
from app.payment import PAYMENT_ASSET, PAYMENT_NETWORK

PAID_URL = config.WARDEN_PAID_SCAN_URL
PAYTO = "0xf4c9fa07f3bb852547fdc4df7c1d9fd9991cfa51"
TEST_KEY = "0x" + "1" * 64
TX = "0x" + "a" * 64


def _enable(monkeypatch, payto=PAYTO):
    monkeypatch.setattr(config, "WARDEN_PAID_ENABLED", True)
    monkeypatch.setattr(config, "TILLA_WARDEN_PAYER_KEY", TEST_KEY)
    monkeypatch.setattr(config, "WARDEN_PAID_PAYTO", payto)


def _challenge(
    amount="10000", pay_to=PAYTO, asset=PAYMENT_ASSET, network=PAYMENT_NETWORK
):
    reqs = PaymentRequirements(
        scheme="exact",
        network=network,
        asset=asset,
        amount=amount,
        pay_to=pay_to,
        max_timeout_seconds=300,
        extra={"name": "USD₮0", "version": "1"},
    )
    return encode_payment_required_header(
        PaymentRequired(x402_version=2, error="pay", accepts=[reqs])
    )


class _Warden:
    """A respx side_effect that 402s the unpaid probe and 200s the signed replay,
    counting signed attempts and capturing the sent signature."""

    def __init__(
        self, challenge_header, verdict=None, replay_status=200, sign_raises=False
    ):
        self.challenge_header = challenge_header
        self.verdict = verdict or {"verdict": "ALLOW", "risk_level": "none"}
        self.replay_status = replay_status
        self.sign_raises = sign_raises
        self.signed = 0
        self.challenges = 0
        self.sent = []

    def __call__(self, request):
        if PAYMENT_SIGNATURE_HEADER in request.headers:
            self.signed += 1
            self.sent.append(request.headers[PAYMENT_SIGNATURE_HEADER])
            if self.sign_raises:
                raise httpx.ConnectError("boom")
            settle = encode_payment_response_header(
                SettleResponse(success=True, transaction=TX, network="eip155:196")
            )
            return httpx.Response(
                self.replay_status,
                json=self.verdict,
                headers={PAYMENT_RESPONSE_HEADER: settle},
            )
        self.challenges += 1
        return httpx.Response(
            402, json={}, headers={PAYMENT_REQUIRED_HEADER: self.challenge_header}
        )


# ------------------------------------------------------- paid_scan happy path
@respx.mock
def test_paid_scan_signs_once_and_returns_receipt(monkeypatch):
    _enable(monkeypatch)
    warden = _Warden(_challenge())
    respx.post(PAID_URL).mock(side_effect=warden)

    result = warden_hire.paid_scan("hello")
    assert result is not None
    assert result.verdict == {"verdict": "ALLOW", "risk_level": "none"}
    assert result.amount_micro == 10000
    assert result.tx_hash == TX
    assert warden.challenges == 1
    assert warden.signed == 1  # signed exactly once
    # the signed authorization targets the pinned payTo
    pp = decode_payment_signature_header(warden.sent[0])
    assert pp.payload["authorization"]["to"] == PAYTO


# ------------------------------------------------------- funds-safety guards
@respx.mock
def test_paid_scan_refuses_over_cap_amount(monkeypatch):
    _enable(monkeypatch)
    warden = _Warden(_challenge(amount="20000"))  # over the 10000 cap
    respx.post(PAID_URL).mock(side_effect=warden)
    assert warden_hire.paid_scan("x") is None
    assert warden.signed == 0  # never signed


@respx.mock
def test_paid_scan_refuses_wrong_payto(monkeypatch):
    _enable(monkeypatch)
    warden = _Warden(_challenge(pay_to="0x" + "b" * 40))
    respx.post(PAID_URL).mock(side_effect=warden)
    assert warden_hire.paid_scan("x") is None
    assert warden.signed == 0


@respx.mock
def test_paid_scan_refuses_wrong_asset(monkeypatch):
    _enable(monkeypatch)
    warden = _Warden(_challenge(asset="0x" + "c" * 40))
    respx.post(PAID_URL).mock(side_effect=warden)
    assert warden_hire.paid_scan("x") is None
    assert warden.signed == 0


@respx.mock
def test_paid_scan_refuses_when_payto_pin_empty(monkeypatch):
    _enable(monkeypatch, payto="")  # no pin configured -> never sign
    warden = _Warden(_challenge())
    respx.post(PAID_URL).mock(side_effect=warden)
    assert warden_hire.paid_scan("x") is None
    assert warden.signed == 0


# ------------------------------------------------------- one-clean-tx (no retry)
@respx.mock
def test_paid_scan_no_retry_after_signed_transport_failure(monkeypatch):
    _enable(monkeypatch)
    warden = _Warden(_challenge(), sign_raises=True)
    respx.post(PAID_URL).mock(side_effect=warden)
    assert warden_hire.paid_scan("x") is None
    assert warden.signed == 1  # exactly one signed attempt, never re-fired


@respx.mock
def test_paid_scan_replay_non_200_is_no_settle(monkeypatch):
    _enable(monkeypatch)
    warden = _Warden(_challenge(), replay_status=402)  # signature rejected -> no settle
    respx.post(PAID_URL).mock(side_effect=warden)
    assert warden_hire.paid_scan("x") is None


def test_paid_scan_no_key_makes_no_request(monkeypatch):
    monkeypatch.setattr(config, "WARDEN_PAID_ENABLED", True)
    monkeypatch.setattr(config, "TILLA_WARDEN_PAYER_KEY", "")
    with respx.mock:
        route = respx.post(PAID_URL).mock(return_value=httpx.Response(402))
        assert warden_hire.paid_scan("x") is None
        assert route.call_count == 0  # signing path never constructed


@respx.mock
def test_paid_scan_first_response_not_402_is_none(monkeypatch):
    _enable(monkeypatch)
    respx.post(PAID_URL).mock(
        return_value=httpx.Response(200, json={"verdict": "ALLOW"})
    )
    assert warden_hire.paid_scan("x") is None


# ------------------------------------------------------- screen() dispatch
@respx.mock
def test_screen_dormant_by_default_uses_demo_only():
    # flag off (default): ONLY the demo URL is ever called; the paid path is never
    # constructed.
    paid = respx.post(PAID_URL).mock(return_value=httpx.Response(402))
    demo = respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(200, json={"verdict": "ALLOW"})
    )
    outcome = screening.screen("safe")
    assert outcome.status == "allow"
    assert outcome.receipt.mode == "demo"
    assert outcome.receipt.tx_hash is None
    assert demo.called
    assert paid.call_count == 0


def test_paid_enabled_requires_flag_and_key(monkeypatch):
    monkeypatch.setattr(config, "WARDEN_PAID_ENABLED", True)
    monkeypatch.setattr(config, "TILLA_WARDEN_PAYER_KEY", "")
    assert screening._paid_enabled() is False
    monkeypatch.setattr(config, "TILLA_WARDEN_PAYER_KEY", TEST_KEY)
    assert screening._paid_enabled() is True
    monkeypatch.setattr(config, "WARDEN_PAID_ENABLED", False)
    assert screening._paid_enabled() is False


@respx.mock
def test_screen_paid_allow_returns_paid_receipt(monkeypatch):
    _enable(monkeypatch)
    warden = _Warden(_challenge())
    respx.post(PAID_URL).mock(side_effect=warden)
    demo = respx.post(WARDEN_SCREEN_URL).mock(return_value=httpx.Response(200))
    outcome = screening.screen("safe")
    assert outcome.status == "allow"
    assert outcome.receipt.mode == "paid"
    assert outcome.receipt.tx_hash == TX
    assert outcome.receipt.amount_micro == 10000
    assert demo.call_count == 0  # the paid hire served it; demo never touched


@respx.mock
def test_screen_paid_block_raises(monkeypatch):
    _enable(monkeypatch)
    warden = _Warden(_challenge(), verdict={"verdict": "BLOCK", "risk_level": "high"})
    respx.post(PAID_URL).mock(side_effect=warden)
    with pytest.raises(screening.ScreeningBlocked):
        screening.screen("bad")


@respx.mock
def test_screen_paid_failure_falls_back_to_demo(monkeypatch):
    _enable(monkeypatch)
    # paid path 402s again on replay (sig rejected) -> paid_scan None -> demo used
    warden = _Warden(_challenge(), replay_status=402)
    respx.post(PAID_URL).mock(side_effect=warden)
    demo = respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(200, json={"verdict": "ALLOW"})
    )
    outcome = screening.screen("safe")
    assert outcome.status == "allow"
    assert outcome.receipt.mode == "demo"  # degraded to the free scan
    assert demo.called

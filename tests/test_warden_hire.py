"""M10 Warden PAID-hire client tests (no network, no funds, no on-chain).

Everything is respx-mocked. Asserts the funds-safety invariants: the client pins
scheme/asset/network/payTo/amount and REFUSES to sign a mismatched challenge; it
signs at most once and never retries a signed authorization; any failure degrades to
the free demo scan. The default (flag off) path never even constructs the signing
code and only ever calls the demo endpoint. The rating write-back is asserted to be
local-only (an event_log row + a dry-run log line) and strictly fail-open.
"""

import logging

import httpx
import pytest
import respx
from sqlalchemy import select
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
from app.db import SessionLocal
from app.models import EventLog
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


# ------------------------------------------------------- rating write-back
def _ratings() -> list[dict]:
    """Every hire.rating payload recorded, oldest first."""
    with SessionLocal() as s:
        return [
            e.data
            for e in s.scalars(
                select(EventLog)
                .where(EventLog.event == "hire.rating")
                .order_by(EventLog.id)
            )
        ]


@respx.mock
def test_settled_hire_records_local_rating_event(monkeypatch):
    _enable(monkeypatch)
    respx.post(PAID_URL).mock(side_effect=_Warden(_challenge()))
    screening.screen("safe")

    (rating,) = _ratings()
    assert rating["agent_id"] == 3808  # Warden, the agent Tilla paid
    assert rating["service"] == "paid_scan"
    assert rating["rating"] == 5  # actionable verdict, served well inside the budget
    assert rating["max_rating"] == 5
    assert rating["actionable_verdict"] is True
    assert rating["tx_hash"] == TX  # the settle tx this rating is evidence for
    assert rating["amount_micro"] == 10000
    assert rating["network"] == PAYMENT_NETWORK
    assert isinstance(rating["latency_ms"], int)


@respx.mock
def test_block_verdict_is_still_a_rated_hire(monkeypatch):
    """A BLOCK is a hire that WORKED — Tilla paid, got an actionable answer, and
    honored it. The rating is written before ScreeningBlocked propagates."""
    _enable(monkeypatch)
    warden = _Warden(_challenge(), verdict={"verdict": "BLOCK", "risk_level": "high"})
    respx.post(PAID_URL).mock(side_effect=warden)
    with pytest.raises(screening.ScreeningBlocked):
        screening.screen("bad")

    (rating,) = _ratings()
    assert rating["actionable_verdict"] is True
    assert rating["rating"] == 5


@respx.mock
def test_unactionable_settled_hire_rates_lowest(monkeypatch):
    """Money spent, nothing Tilla can act on -> the floor of the scale."""
    _enable(monkeypatch)
    respx.post(PAID_URL).mock(
        side_effect=_Warden(_challenge(), verdict={"verdict": "REVIEW"})
    )
    demo = respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(200, json={"verdict": "ALLOW"})
    )
    outcome = screening.screen("safe")
    assert outcome.receipt.mode == "demo"  # unchanged fallback semantics
    assert demo.called

    (rating,) = _ratings()
    assert rating["actionable_verdict"] is False
    assert rating["rating"] == 1


@respx.mock
def test_demo_screen_records_no_rating():
    """Nothing was hired, so nothing is rated (the dormant default path)."""
    respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(200, json={"verdict": "ALLOW"})
    )
    screening.screen("safe")
    assert _ratings() == []


def test_rating_penalizes_a_slow_hire(monkeypatch):
    """Half the screen budget is the line between a fast and a slow hire."""
    monkeypatch.setattr(config, "WARDEN_SCREEN_TIMEOUT", 10.0)
    assert warden_hire._rating(True, 4_999) == 5
    assert warden_hire._rating(True, 5_001) == 3
    assert warden_hire._rating(False, 1) == 1  # unactionable outranks any speed


@respx.mock
def test_rating_submission_emits_the_command_and_sends_nothing(monkeypatch, caplog):
    """With an INDEPENDENT counterparty and TILLA_RATE_HIRES=1, the seam logs the
    exact verified `onchainos agent feedback-submit` line and still sends nothing —
    --task-id is the operator's to supply, and respx fails this test on any stray
    request. (This test previously asserted a "no verified transport" dry run; the
    CLI contract has since been read off the installed binary.)"""
    _enable(monkeypatch)
    _owners(monkeypatch, "0xf4c9", "0xsomeoneelse")
    monkeypatch.setenv("TILLA_RATE_HIRES", "1")
    paid = respx.post(PAID_URL).mock(side_effect=_Warden(_challenge()))
    with caplog.at_level(logging.INFO, logger="tilla"):
        screening.screen("safe")
    assert "onchainos agent feedback-submit" in caplog.text
    assert "--task-id" in caplog.text  # never auto-filled
    assert paid.call_count == 2  # the 402 probe + the one signed replay, nothing more
    assert len(_ratings()) == 1


@respx.mock
def test_rating_flag_off_still_records_locally_and_logs_nothing(monkeypatch, caplog):
    _enable(monkeypatch)
    monkeypatch.delenv("TILLA_RATE_HIRES", raising=False)
    respx.post(PAID_URL).mock(side_effect=_Warden(_challenge()))
    with caplog.at_level(logging.INFO, logger="tilla"):
        screening.screen("safe")
    assert "DRY RUN" not in caplog.text
    assert len(_ratings()) == 1  # the local half is not flag-gated


@respx.mock
def test_rating_failure_never_moves_the_screening_decision(monkeypatch):
    """Fail-open, and ONLY here: a broken rating write leaves the paid ALLOW intact."""
    _enable(monkeypatch)

    def _boom(*args, **kwargs):
        raise RuntimeError("event log down")

    monkeypatch.setattr(warden_hire, "log_event", _boom)
    respx.post(PAID_URL).mock(side_effect=_Warden(_challenge()))
    demo = respx.post(WARDEN_SCREEN_URL).mock(return_value=httpx.Response(200))

    outcome = screening.screen("safe")
    assert outcome.status == "allow"
    assert outcome.receipt.mode == "paid"
    assert outcome.receipt.tx_hash == TX
    assert demo.call_count == 0  # never degraded by the rating problem
    assert _ratings() == []


def _owners(monkeypatch, mine: str | None, theirs: str | None):
    """Stub the on-chain ownerOf read b2b performs, by agent id."""
    from app import b2b

    monkeypatch.setattr(
        b2b,
        "verify_agent_owner",
        lambda agent_id, *, fresh=False: mine if int(agent_id) == 6961 else theirs,
    )


def test_self_review_is_refused_when_owners_match(monkeypatch):
    """The real production condition: Warden #3808 and Tilla #6961 both resolve to
    0xf4c9…fa51, so a rating of Warden is a review of the reviewer's own owner."""
    _owners(monkeypatch, "0xf4c9", "0xf4c9")
    with pytest.raises(warden_hire.SelfReviewRefused) as exc:
        warden_hire.assert_not_self_review(3808)
    assert "shares Tilla's owner" in str(exc.value)


def test_unreadable_ownership_is_refused_not_assumed_independent(monkeypatch):
    # Fail CLOSED: an unverifiable counterparty is not evidence of independence.
    _owners(monkeypatch, "0xf4c9", None)
    with pytest.raises(warden_hire.SelfReviewRefused):
        warden_hire.assert_not_self_review(3808)
    _owners(monkeypatch, None, "0xother")
    with pytest.raises(warden_hire.SelfReviewRefused):
        warden_hire.assert_not_self_review(3808)


def test_third_party_owner_passes_the_guard(monkeypatch):
    _owners(monkeypatch, "0xf4c9", "0xsomeoneelse")
    warden_hire.assert_not_self_review(3808)  # no raise


@respx.mock
def test_settled_hire_withholds_the_rating_when_not_independent(monkeypatch):
    """The hire is still recorded locally — honest bookkeeping — but marked
    independent=False with the reason, and nothing is prepared for submission."""
    _enable(monkeypatch)
    _owners(monkeypatch, "0xf4c9", "0xf4c9")
    respx.post(PAID_URL).mock(side_effect=_Warden(_challenge()))
    screening.screen("safe")

    (rating,) = _ratings()
    assert rating["independent"] is False
    assert "shares Tilla's owner" in rating["withheld_reason"]
    assert rating["tx_hash"] == TX  # the paid hire itself is still evidenced


@respx.mock
def test_settled_hire_marks_an_independent_rating(monkeypatch):
    _enable(monkeypatch)
    _owners(monkeypatch, "0xf4c9", "0xsomeoneelse")
    respx.post(PAID_URL).mock(side_effect=_Warden(_challenge()))
    screening.screen("safe")

    (rating,) = _ratings()
    assert rating["independent"] is True
    assert "withheld_reason" not in rating


def test_submit_command_matches_the_verified_cli_contract():
    """Pins the flags of `onchainos agent feedback-submit` as read from the
    installed CLI on 2026-07-25: --agent-id, --creator-id, --score (0.00-5.00),
    --task-id (REQUIRED), --description."""
    cmd = warden_hire._submit_command(
        {
            "agent_id": 3808,
            "rating": 5,
            "actionable_verdict": True,
            "latency_ms": 120,
        }
    )
    assert cmd.startswith("onchainos agent feedback-submit")
    for flag in ("--agent-id 3808", "--creator-id 6961", "--score 5.00", "--task-id"):
        assert flag in cmd
    assert "--description" in cmd

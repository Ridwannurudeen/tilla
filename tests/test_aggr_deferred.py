"""M8 aggr_deferred tests (no network, no funds): the accepts-builder gating, the
pure PAYMENT-REQUIRED challenge filter, the agent-guard's per-store header rewrite,
and the pay-time handler scheme gate that 409s a non-batch aggr payment BEFORE any
settle. A real settle needs an OKX TEE agentic-wallet buyer and is USER-gated, so
nothing here asserts a settlement — only that the OPTION is offered honestly.
"""

import asyncio

import pytest
from starlette.requests import Request
from starlette.responses import Response
from x402.http.utils import (
    decode_payment_required_header,
    encode_payment_required_header,
)
from x402.schemas import PaymentPayload, PaymentRequired, PaymentRequirements

import app.main as main  # noqa: F401 — ensures app + limiter wired for direct calls
from app import agentic, config
from app.db import SessionLocal
from app.models import Product, Store
from app.payment import (
    build_store_payment_option,
    build_store_payment_options,
    load_payment_rail,
)
from sqlalchemy import select

RAIL = load_payment_rail({"PAY_TO_ADDRESS": "0x" + "f" * 40})
ASSET = "0x" + "a" * 40
PAYER = "0x" + "3" * 40
NONCE = "0x" + "7" * 64


def _reqs(scheme: str) -> PaymentRequirements:
    return PaymentRequirements(
        scheme=scheme,
        network="eip155:196",
        asset=ASSET,
        amount="9000000",
        pay_to="0x" + "b" * 40,
        max_timeout_seconds=300,
        extra={},
    )


def _challenge_header(*schemes: str) -> str:
    pr = PaymentRequired(
        x402_version=2, error="pay", accepts=[_reqs(s) for s in schemes]
    )
    return encode_payment_required_header(pr)


def _set_pricing(slug: str, model: str) -> None:
    with SessionLocal() as s:
        store = s.scalar(select(Store).where(Store.slug == slug))
        p = agentic._active_product(s, store.id)
        p.pricing_model = model
        s.commit()


# ------------------------------------------------------- accepts-builder
def test_accepts_builder_exact_only_when_flag_off(monkeypatch):
    monkeypatch.setattr(config, "AGGR_DEFERRED_ENABLED", False)
    opts = build_store_payment_options(RAIL)
    assert [o.scheme for o in opts] == ["exact"]


def test_accepts_builder_exact_field_identical_to_singular(monkeypatch):
    """Live-safety: with the flag off the built option is field-identical to the
    pre-M8 single option (same scheme/network/timeout, same dynamic resolvers)."""
    monkeypatch.setattr(config, "AGGR_DEFERRED_ENABLED", False)
    only = build_store_payment_options(RAIL)[0]
    ref = build_store_payment_option(RAIL)
    assert only.scheme == ref.scheme == "exact"
    assert only.network == ref.network
    assert only.max_timeout_seconds == ref.max_timeout_seconds
    # both pay_to/price are async resolver callables (dynamic accepts), not literals
    assert callable(only.pay_to) and callable(only.price)


def test_accepts_builder_adds_aggr_when_flag_on(monkeypatch):
    monkeypatch.setattr(config, "AGGR_DEFERRED_ENABLED", True)
    opts = build_store_payment_options(RAIL)
    assert [o.scheme for o in opts] == ["exact", "aggr_deferred"]
    aggr = opts[1]
    assert aggr.network == "eip155:196"
    assert callable(aggr.pay_to) and callable(aggr.price)


# ------------------------------------------------------- pure filter
def test_filter_removes_aggr_from_two_scheme_challenge():
    out = agentic._filter_aggr_from_challenge(
        _challenge_header("exact", "aggr_deferred")
    )
    assert out is not None
    assert [a.scheme for a in decode_payment_required_header(out).accepts] == ["exact"]


def test_filter_noop_when_no_aggr():
    assert agentic._filter_aggr_from_challenge(_challenge_header("exact")) is None


def test_filter_noop_on_garbage():
    assert agentic._filter_aggr_from_challenge("not-base64-!@#") is None


# ------------------------------------------------------- guard dispatch
def _buy_request(slug: str) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": f"/s/{slug}/buy",
        "raw_path": f"/s/{slug}/buy".encode(),
        "headers": [],
        "query_string": b"",
        "client": ("test", 1),
        "server": ("test", 80),
        "scheme": "http",
    }
    return Request(scope)


def _dispatch_402(slug: str, header: str) -> Response:
    async def _call_next(_req):
        return Response(status_code=402, headers={"PAYMENT-REQUIRED": header})

    return asyncio.run(agentic.agent_guard_dispatch(_buy_request(slug), _call_next))


def test_guard_strips_aggr_for_non_batch_store(make_store, monkeypatch):
    make_store(slug="gd1")  # default one_time
    monkeypatch.setattr(config, "AGGR_DEFERRED_ENABLED", True)
    resp = _dispatch_402("gd1", _challenge_header("exact", "aggr_deferred"))
    schemes = [
        a.scheme
        for a in decode_payment_required_header(
            resp.headers["PAYMENT-REQUIRED"]
        ).accepts
    ]
    assert schemes == ["exact"]  # aggr stripped for a non-batch store


def test_guard_keeps_aggr_for_batch_store(make_store, monkeypatch):
    make_store(slug="gd2")
    _set_pricing("gd2", "batch")
    monkeypatch.setattr(config, "AGGR_DEFERRED_ENABLED", True)
    resp = _dispatch_402("gd2", _challenge_header("exact", "aggr_deferred"))
    schemes = [
        a.scheme
        for a in decode_payment_required_header(
            resp.headers["PAYMENT-REQUIRED"]
        ).accepts
    ]
    assert schemes == ["exact", "aggr_deferred"]  # batch keeps the option


def test_guard_untouched_when_flag_off(make_store, monkeypatch):
    make_store(slug="gd3")
    monkeypatch.setattr(config, "AGGR_DEFERRED_ENABLED", False)
    # flag off -> the branch never runs; header passes through byte-identical
    header = _challenge_header("exact", "aggr_deferred")
    resp = _dispatch_402("gd3", header)
    assert resp.headers["PAYMENT-REQUIRED"] == header


# ------------------------------------------------------- handler scheme gate
def test_payment_scheme_reads_matched_requirements():
    req = _buy_request("x")
    req.state.payment_requirements = _reqs("aggr_deferred")
    assert agentic._payment_scheme(req) == "aggr_deferred"


def test_payment_scheme_falls_back_to_payload_accepted():
    req = _buy_request("x")
    req.state.payment_payload = PaymentPayload(
        x402_version=2, payload={}, accepted=_reqs("exact")
    )
    assert agentic._payment_scheme(req) == "exact"


def _paid_request(slug: str, scheme: str) -> Request:
    req = _buy_request(slug)
    req.state.payment_payload = PaymentPayload(
        x402_version=2,
        payload={"authorization": {"nonce": NONCE, "from": PAYER}},
        accepted=_reqs(scheme),
    )
    req.state.payment_requirements = _reqs(scheme)
    return req


def test_handler_409s_aggr_on_non_batch_before_settle(make_store):
    make_store(slug="hg1")  # one_time
    with SessionLocal() as s:
        with pytest.raises(Exception) as exc:
            agentic.agent_buy(_paid_request("hg1", "aggr_deferred"), "hg1", s)
    assert getattr(exc.value, "status_code", None) == 409
    # no order created -> the signed authorization is never executed
    with SessionLocal() as s:
        assert s.scalar(select(Product).join(Store)) is not None
        from app.models import Order

        assert s.scalar(select(Order)) is None


def test_handler_allows_aggr_on_batch_store(make_store):
    make_store(slug="hg2")
    _set_pricing("hg2", "batch")
    with SessionLocal() as s:
        resp = agentic.agent_buy(_paid_request("hg2", "aggr_deferred"), "hg2", s)
    assert resp.status_code == 200
    from app.models import Order

    with SessionLocal() as s:
        order = s.scalar(select(Order))
        assert order is not None
        assert order.status == "settling"  # delivered, awaiting settle confirm


def test_handler_allows_exact_on_non_batch(make_store):
    make_store(slug="hg3")  # one_time
    with SessionLocal() as s:
        resp = agentic.agent_buy(_paid_request("hg3", "exact"), "hg3", s)
    assert resp.status_code == 200

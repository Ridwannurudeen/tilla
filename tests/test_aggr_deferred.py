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
from app import agentic, checkout, config
from app.db import SessionLocal
from app.models import Order, Product, Store
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


# --------------------------------- aggr_deferred settle reconciliation (finding #3)
def _batch_settling_order(make_store, slug: str) -> str:
    make_store(slug=slug)
    _set_pricing(slug, "batch")
    with SessionLocal() as s:
        agentic.agent_buy(_paid_request(slug, "aggr_deferred"), slug, s)
    from app.models import Order

    with SessionLocal() as s:
        return s.scalar(select(Order)).id


def _events(order_id: str) -> list[str]:
    from app.models import EventLog

    with SessionLocal() as s:
        return [
            e.event
            for e in s.scalars(
                select(EventLog).where(EventLog.order_id == order_id)
            ).all()
        ]


def test_aggr_deferred_no_tx_stays_settling_and_is_not_marked_settled(make_store):
    # aggr_deferred can serve 200 with NO decodable tx at settle time; the order
    # must NOT go terminal 'delivered' or emit 'agent_order.settled' with no
    # evidence — it stays provisional 'settling' with a distinct pending marker.
    oid = _batch_settling_order(make_store, "ad1")
    agentic.record_settlement(oid, "not-a-decodable-header", "aggr_deferred")
    from app.models import Order

    with SessionLocal() as s:
        assert s.get(Order, oid).status == "settling"
        assert s.get(Order, oid).tx_hash is None
    events = _events(oid)
    assert "agent_order.settle_pending" in events
    assert "agent_order.settled" not in events


def test_aggr_deferred_with_real_tx_flips_delivered(make_store):
    # Once a real aggregated tx hash is present, the order flips to delivered.
    from x402.http.utils import encode_payment_response_header
    from x402.schemas import SettleResponse

    oid = _batch_settling_order(make_store, "ad2")
    header = encode_payment_response_header(
        SettleResponse(success=True, transaction="0x" + "d" * 64, network="eip155:196")
    )
    agentic.record_settlement(oid, header, "aggr_deferred")
    from app.models import Order

    with SessionLocal() as s:
        assert s.get(Order, oid).status == "delivered"
        assert s.get(Order, oid).tx_hash == "0x" + "d" * 64
    assert "agent_order.settled" in _events(oid)


# ------------------------ get_settle_status reconciliation poller (M8 blocker) ------
from app import reconcile  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_reconcile_state():
    """The per-pair scan cursors and the completed-scan stamp are module-level, and
    these tests share one (buyer, pay_to) pair — reset both around every test."""
    reconcile._reset_state()
    yield
    reconcile._reset_state()


class _FakeStatusClient:
    """Stands in for the OKX sync facilitator client — no network. Records the
    reference it was polled with and returns a canned SettleStatusResponse."""

    def __init__(self, resp):
        self._resp = resp
        self.queried: list[str] = []

    def get_settle_status(self, tx_hash: str):
        self.queried.append(tx_hash)
        return self._resp


def _status(**kw):
    from x402.schemas.responses import SettleStatusResponse

    return SettleStatusResponse(**kw)


def _pending_settling_order(make_store, slug: str, ref: str = "0x" + "e" * 64) -> str:
    """A batch order held 'settling' after a deferred settle returned success with a
    PENDING (unconfirmed) aggregated reference — the state the poller finalizes."""
    from x402.http.utils import encode_payment_response_header
    from x402.schemas import SettleResponse

    oid = _batch_settling_order(make_store, slug)
    header = encode_payment_response_header(
        SettleResponse(
            success=True, transaction=ref, status="pending", network="eip155:196"
        )
    )
    agentic.record_settlement(oid, header, "aggr_deferred")
    return oid


def test_pending_settle_captures_ref_and_stays_settling(make_store):
    # A deferred settle marked 'pending' (aggregated tx not yet confirmed) must NOT
    # flip to delivered — it stays settling, records the pollable ref, logs pending.
    oid = _pending_settling_order(make_store, "rp0")
    with SessionLocal() as s:
        o = s.get(Order, oid)
        assert o.status == "settling"
        assert o.settle_ref == "0x" + "e" * 64
        assert o.tx_hash is None
    events = _events(oid)
    assert "agent_order.settle_pending" in events
    assert "agent_order.settled" not in events


def test_reconcile_confirmed_tx_flips_delivered(make_store, monkeypatch):
    oid = _pending_settling_order(make_store, "rp1")
    client = _FakeStatusClient(
        _status(
            success=True,
            status="success",
            transaction="0x" + "c" * 64,
            network="eip155:196",
        )
    )
    monkeypatch.setattr(reconcile, "_client_factory", lambda: client)
    assert reconcile.reconcile_tick() == 1
    assert client.queried == ["0x" + "e" * 64]
    with SessionLocal() as s:
        o = s.get(Order, oid)
        assert o.status == "delivered"
        assert o.tx_hash == "0x" + "c" * 64
    assert "agent_order.settled" in _events(oid)


def test_reconcile_pending_leaves_settling(make_store, monkeypatch):
    oid = _pending_settling_order(make_store, "rp2")
    client = _FakeStatusClient(_status(success=True, status="pending"))
    monkeypatch.setattr(reconcile, "_client_factory", lambda: client)
    assert reconcile.reconcile_tick() == 0
    with SessionLocal() as s:
        o = s.get(Order, oid)
        assert o.status == "settling"
        assert o.tx_hash is None
    assert "agent_order.settled" not in _events(oid)


def test_reconcile_success_without_tx_never_delivers(make_store, monkeypatch):
    # Never deliver without a confirmed tx hash, even on a 'success' status.
    oid = _pending_settling_order(make_store, "rp3")
    client = _FakeStatusClient(
        _status(success=True, status="success", transaction=None)
    )
    monkeypatch.setattr(reconcile, "_client_factory", lambda: client)
    assert reconcile.reconcile_tick() == 0
    with SessionLocal() as s:
        assert s.get(Order, oid).status == "settling"


def test_reconcile_failed_voids(make_store, monkeypatch):
    oid = _pending_settling_order(make_store, "rp4")
    client = _FakeStatusClient(
        _status(success=False, status="failed", error_reason="reverted")
    )
    monkeypatch.setattr(reconcile, "_client_factory", lambda: client)
    assert reconcile.reconcile_tick() == 1
    with SessionLocal() as s:
        assert s.get(Order, oid).status == "canceled"
    events = _events(oid)
    assert "agent_order.settle_reconcile_failed" in events
    assert "agent_order.settled" not in events


def test_reconcile_missing_status_does_not_void(make_store, monkeypatch):
    # A facilitator response with success=False but NO explicit "failed" status
    # (e.g. status omitted on a transient error) must NOT void a paid order — only
    # an explicit "failed" is definitive. Stays settling, retried next tick.
    oid = _pending_settling_order(make_store, "rpns")
    client = _FakeStatusClient(_status(success=False, status=None))
    monkeypatch.setattr(reconcile, "_client_factory", lambda: client)
    assert reconcile.reconcile_tick() == 0
    with SessionLocal() as s:
        assert s.get(Order, oid).status == "settling"
    assert "agent_order.settle_reconcile_failed" not in _events(oid)


def test_reaper_exempts_aggr_deferred_orders(make_store, monkeypatch):
    # A poller-owned aggr order (settle_ref set) whose aggregated tx confirms slowly
    # must NOT be reaped by the 15-min reaper — that would void a genuinely-paid
    # order without refund. The reaper skips settle_ref-bearing orders.
    import datetime

    oid = _pending_settling_order(make_store, "rpreap")
    with SessionLocal() as s:
        o = s.get(Order, oid)
        # Age it well past the reap window.
        o.paid_at = checkout._now() - datetime.timedelta(hours=2)
        s.commit()
        reaped = agentic.reap_agent_orders(s)
        s.commit()
        assert reaped == 0
        assert s.get(Order, oid).status == "settling"  # left for the poller


def test_reconcile_idempotent_double_tick(make_store, monkeypatch):
    oid = _pending_settling_order(make_store, "rp5")
    client = _FakeStatusClient(
        _status(
            success=True,
            status="success",
            transaction="0x" + "a" * 64,
            network="eip155:196",
        )
    )
    monkeypatch.setattr(reconcile, "_client_factory", lambda: client)
    assert reconcile.reconcile_tick() == 1
    # A delivered order has left 'settling', so the second tick never re-selects it —
    # no re-delivery, no duplicate settled event.
    assert reconcile.reconcile_tick() == 0
    with SessionLocal() as s:
        assert s.get(Order, oid).status == "delivered"
    assert _events(oid).count("agent_order.settled") == 1


def test_reconcile_skips_orders_without_settle_ref(make_store, monkeypatch):
    # An order with no captured ref is never polled (the poller only touches rows
    # carrying settle_ref) — a no-tx settle stays for the reaper, not the poller.
    oid = _batch_settling_order(make_store, "rp6")
    agentic.record_settlement(oid, "not-a-decodable-header", "aggr_deferred")
    client = _FakeStatusClient(_status(success=True, status="success"))
    monkeypatch.setattr(reconcile, "_client_factory", lambda: client)
    assert reconcile.reconcile_tick() == 0
    assert client.queried == []
    with SessionLocal() as s:
        assert s.get(Order, oid).status == "settling"


def test_reconcile_dormant_without_client(make_store, monkeypatch):
    _pending_settling_order(make_store, "rp7")
    monkeypatch.setattr(reconcile, "_client_factory", lambda: None)
    # No client (no creds) => idle, zero network, nothing acted on.
    assert reconcile.reconcile_tick() == 0


# --------------- CHAIN settlement detection (facilitator gives no tx ref) ----------
# The live OKX case: /settle serves 200 with an EMPTY transaction, so settlement is
# only observable on-chain — a USDT0 Transfer from buyer -> merchant whose tx was
# submitted by the facilitator RELAYER, batched (N orders -> one summed transfer).
import json  # noqa: E402

import httpx  # noqa: E402
import respx  # noqa: E402

from app import chain  # noqa: E402

RELAYER = config.AGGR_FACILITATOR_RELAYER


class _ChainRpc:
    """respx-mocked X Layer JSON-RPC: a head, USDT0 Transfer logs, and receipts (with
    the tx `to` = submitter). Mirrors tests/test_checkout.Rpc."""

    def __init__(self, head, logs=None, receipts=None):
        self.head = head
        self.logs = logs or []
        self.receipts = receipts or {}
        self.scanned: list[tuple[int, int]] = []  # (fromBlock, toBlock) per eth_getLogs

    def handler(self, request):
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        if method == "eth_blockNumber":
            result = hex(self.head)
        elif method == "eth_getLogs":
            frm = int(params[0]["fromBlock"], 16)
            to = int(params[0]["toBlock"], 16)
            self.scanned.append((frm, to))
            tos = {a.lower() for a in params[0]["topics"][2]}
            result = [
                lg
                for lg in self.logs
                if frm <= int(lg["blockNumber"], 16) <= to
                and lg["topics"][2].lower() in tos
            ]
        elif method == "eth_getTransactionReceipt":
            result = self.receipts.get(params[0].lower())
        else:
            result = None
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )

    def install(self):
        respx.post(config.RPC_URL).mock(side_effect=self.handler)


def _transfer_log(frm, to, value, block, tx, log_index=0):
    return {
        "address": config.USDT0,
        "topics": [
            config.TRANSFER_TOPIC,
            chain.pad_address(frm),
            chain.pad_address(to),
        ],
        "data": hex(value),
        "transactionHash": tx,
        "logIndex": hex(log_index),
        "blockNumber": hex(block),
    }


def _receipt(to, status="0x1"):
    return {"status": status, "to": to}


def _chain_settling_order(make_store, slug, price=1_000_000, pay_to="0x" + "a" * 40):
    """A batch order held 'settling' after the live empty-tx settle (settle_ref NULL)."""
    make_store(slug=slug, price_micro=price, pay_to=pay_to)
    _set_pricing(slug, "batch")
    with SessionLocal() as s:
        agentic.agent_buy(_paid_request(slug, "aggr_deferred"), slug, s)
    # The live path: /settle 200 with no decodable tx leaves it settling, ref NULL.
    with SessionLocal() as s:
        return s.scalar(select(Order)).id


@respx.mock
def test_chain_reconcile_flips_delivered_on_facilitator_transfer(
    make_store, monkeypatch
):
    monkeypatch.setattr(config, "AGGR_DEFERRED_ENABLED", True)
    oid = _chain_settling_order(make_store, "ch1", price=1_000_000)
    tx = "0x" + "1" * 64
    rpc = _ChainRpc(
        head=1000,
        logs=[_transfer_log(PAYER, "0x" + "a" * 40, 1_000_000, 900, tx)],
        receipts={tx: _receipt(RELAYER)},
    )
    rpc.install()
    assert reconcile.reconcile_chain_tick() == 1
    with SessionLocal() as s:
        o = s.get(Order, oid)
        assert o.status == "delivered"
        assert o.tx_hash == tx
        assert o.settle_ref == tx
    assert "agent_order.settled" in _events(oid)


@respx.mock
def test_chain_reconcile_batches_one_transfer_to_two_orders(make_store, monkeypatch):
    monkeypatch.setattr(config, "AGGR_DEFERRED_ENABLED", True)
    make_store(slug="ch2", price_micro=1_000_000, pay_to="0x" + "a" * 40)
    _set_pricing("ch2", "batch")
    ids = []
    for i in range(2):
        nonce = "0x" + f"{i:064x}"
        req = _buy_request("ch2")
        req.state.payment_payload = PaymentPayload(
            x402_version=2,
            payload={"authorization": {"nonce": nonce, "from": PAYER}},
            accepted=_reqs("aggr_deferred"),
        )
        req.state.payment_requirements = _reqs("aggr_deferred")
        with SessionLocal() as s:
            agentic.agent_buy(req, "ch2", s)
    with SessionLocal() as s:
        ids = [o.id for o in s.scalars(select(Order).order_by(Order.created_at)).all()]
    assert len(ids) == 2
    # ONE transfer of the SUM (2.0) settles BOTH 1.0 orders.
    tx = "0x" + "2" * 64
    rpc = _ChainRpc(
        head=1000,
        logs=[_transfer_log(PAYER, "0x" + "a" * 40, 2_000_000, 900, tx)],
        receipts={tx: _receipt(RELAYER)},
    )
    rpc.install()
    assert reconcile.reconcile_chain_tick() == 2
    with SessionLocal() as s:
        for oid in ids:
            o = s.get(Order, oid)
            assert o.status == "delivered"
            assert o.settle_ref == tx


@respx.mock
def test_chain_reconcile_ignores_non_relayer_transfer(make_store, monkeypatch):
    # A direct buyer->merchant transfer NOT submitted by the facilitator relayer is
    # not a settlement — the order must stay settling (no false positive).
    monkeypatch.setattr(config, "AGGR_DEFERRED_ENABLED", True)
    oid = _chain_settling_order(make_store, "ch3", price=1_000_000)
    tx = "0x" + "3" * 64
    rpc = _ChainRpc(
        head=1000,
        logs=[_transfer_log(PAYER, "0x" + "a" * 40, 1_000_000, 900, tx)],
        receipts={tx: _receipt("0x" + "9" * 40)},  # tx.to != relayer
    )
    rpc.install()
    assert reconcile.reconcile_chain_tick() == 0
    with SessionLocal() as s:
        assert s.get(Order, oid).status == "settling"


@respx.mock
def test_chain_reconcile_ignores_wrong_from(make_store, monkeypatch):
    # A facilitator-relayed transfer to pay_to but from a DIFFERENT buyer does not
    # settle this order.
    monkeypatch.setattr(config, "AGGR_DEFERRED_ENABLED", True)
    oid = _chain_settling_order(make_store, "ch4", price=1_000_000)
    tx = "0x" + "4" * 64
    rpc = _ChainRpc(
        head=1000,
        logs=[_transfer_log("0x" + "5" * 40, "0x" + "a" * 40, 1_000_000, 900, tx)],
        receipts={tx: _receipt(RELAYER)},
    )
    rpc.install()
    assert reconcile.reconcile_chain_tick() == 0
    with SessionLocal() as s:
        assert s.get(Order, oid).status == "settling"


@respx.mock
def test_chain_reconcile_idempotent_double_tick(make_store, monkeypatch):
    monkeypatch.setattr(config, "AGGR_DEFERRED_ENABLED", True)
    oid = _chain_settling_order(make_store, "ch5", price=1_000_000)
    tx = "0x" + "6" * 64
    rpc = _ChainRpc(
        head=1000,
        logs=[_transfer_log(PAYER, "0x" + "a" * 40, 1_000_000, 900, tx)],
        receipts={tx: _receipt(RELAYER)},
    )
    rpc.install()
    assert reconcile.reconcile_chain_tick() == 1
    # Delivered order has left 'settling'; a re-scan of the same tx never re-settles.
    assert reconcile.reconcile_chain_tick() == 0
    assert _events(oid).count("agent_order.settled") == 1


def test_chain_reconcile_dormant_when_flag_off(make_store, monkeypatch):
    monkeypatch.setattr(config, "AGGR_DEFERRED_ENABLED", False)
    _chain_settling_order(make_store, "ch6", price=1_000_000)
    # Flag off => no candidate query, zero RPC (no respx mock installed, would error).
    assert reconcile.reconcile_chain_tick() == 0


def test_reap_tick_skips_when_chain_unreachable(make_store, monkeypatch):
    # A live aggr_deferred order sits settling with settle_ref NULL until the chain
    # reconciler finds the facilitator transfer. If the chain is unreachable the reaper
    # must NOT void it (fail-closed) — else an RPC outage spanning the 15-min window
    # would claw back a genuinely-paid order.
    import datetime

    monkeypatch.setattr(config, "AGGR_DEFERRED_ENABLED", True)
    oid = _chain_settling_order(make_store, "reapguard1", price=1_000_000)
    with SessionLocal() as s:
        s.get(Order, oid).paid_at = checkout._now() - datetime.timedelta(hours=2)
        s.commit()
    monkeypatch.setattr(reconcile, "chain_reachable", lambda: False)
    agentic._reap_tick()
    with SessionLocal() as s:
        assert s.get(Order, oid).status == "settling"


@respx.mock
def test_reap_tick_voids_stuck_order_when_chain_reachable(make_store, monkeypatch):
    # Chain reachable + a COMPLETED scan that finds no facilitator transfer => a
    # genuinely stuck order (never settled on-chain) is still voided by the reaper,
    # once it is past the deferred deadline.
    import datetime

    monkeypatch.setattr(config, "AGGR_DEFERRED_ENABLED", True)
    oid = _chain_settling_order(make_store, "reapguard2", price=1_000_000)
    with SessionLocal() as s:
        s.get(Order, oid).paid_at = (
            checkout._now()
            - agentic.DEFERRED_REAP_AFTER
            - datetime.timedelta(minutes=1)
        )
        s.commit()
    _ChainRpc(head=1000).install()  # head answers, no transfer in the window
    agentic._reap_tick()
    with SessionLocal() as s:
        assert s.get(Order, oid).status == "canceled"


@respx.mock
def test_reaper_holds_deferred_order_whose_transfer_lands_late(make_store, monkeypatch):
    # THE PROD DEFECT (six orders voided 2026-07-23): the live OKX /settle returns no tx
    # ref, so EVERY deferred order sits 'settling' with settle_ref NULL until the
    # facilitator relayer's transfer appears on chain. The 15-min reaper voided them
    # mid-flight. An order past the OLD deadline must SURVIVE and then be finalized by
    # the reconcile path when the transfer finally lands — never voided.
    import datetime

    monkeypatch.setattr(config, "AGGR_DEFERRED_ENABLED", True)
    oid = _chain_settling_order(make_store, "reaplate", price=1_000_000)
    with SessionLocal() as s:
        s.get(Order, oid).paid_at = (
            checkout._now() - agentic.REAP_AFTER - datetime.timedelta(minutes=1)
        )
        s.commit()
    rpc = _ChainRpc(head=1000)
    rpc.install()
    # Tick 1: the relayer has not paid yet. Nothing on chain, order past the OLD 15-min
    # deadline — it must still be settling.
    agentic._reap_tick()
    with SessionLocal() as s:
        assert s.get(Order, oid).status == "settling"
    # Tick 2: the relayer's transfer lands — reconcile finalizes the very same order.
    tx = "0x" + "7" * 64
    rpc.logs = [_transfer_log(PAYER, "0x" + "a" * 40, 1_000_000, 900, tx)]
    rpc.receipts = {tx: _receipt(RELAYER)}
    agentic._reap_tick()
    with SessionLocal() as s:
        o = s.get(Order, oid)
        assert o.status == "delivered"
        assert o.tx_hash == tx
    assert "agent_order.reaped" not in _events(oid)


@respx.mock
def test_reap_tick_holds_deferred_order_when_the_log_scan_fails(
    make_store, monkeypatch
):
    # chain_reachable() only proves eth_blockNumber answers. When the eth_getLogs scan
    # itself fails, "no settlement transfer found" is not an answer — a paid-but-slow
    # order must be held, not voided.
    import datetime

    monkeypatch.setattr(config, "AGGR_DEFERRED_ENABLED", True)
    oid = _chain_settling_order(make_store, "reapblind", price=1_000_000)
    with SessionLocal() as s:
        s.get(Order, oid).paid_at = (
            checkout._now() - agentic.DEFERRED_REAP_AFTER - datetime.timedelta(hours=1)
        )
        s.commit()
    rpc = _ChainRpc(head=1000)

    def blind(request):
        body = json.loads(request.content)
        if body["method"] == "eth_getLogs":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "error": {"code": -32000, "message": "query returned no results"},
                },
            )
        return rpc.handler(request)

    respx.post(config.RPC_URL).mock(side_effect=blind)
    agentic._reap_tick()
    with SessionLocal() as s:
        assert s.get(Order, oid).status == "settling"


# ---------- backlog anchoring: settlements older than the trailing lookback window -----
# The scan used to be head-anchored (head - AGGR_SETTLE_LOOKBACK_BLOCKS, ~20 min at the
# 1s X Layer block time). Combined with the fail-closed reap that is a trap: an order
# whose settlement lands outside that window is (correctly) never voided, but was also
# never reachable — stuck 'settling' forever. The scan now anchors at the OLDEST settling
# order for the pair and walks FORWARD across ticks until it reaches head.
_WINDOW_SPAN = config.GETLOGS_MAX_SPAN + 1  # each slice covers [c, c+SPAN] inclusive
_TICK_SPAN = config.RECONCILE_MAX_WINDOWS * _WINDOW_SPAN


def _age_order(oid: str, delta) -> None:
    with SessionLocal() as s:
        o = s.get(Order, oid)
        o.created_at = checkout._now() - delta
        o.paid_at = o.created_at
        s.commit()


def _anchor_for(head: int, delta) -> int:
    """The block _scan_start lands on for an order aged ``delta`` (1 block/s x1.25 + 600).
    Real elapsed time only grows while the test runs, so the live anchor is this value or
    a few blocks EARLIER — the safe direction, and what the assertions allow for."""
    return head - (int(delta.total_seconds() * 1.25) + 600)


@respx.mock
def test_fresh_settle_is_caught_on_the_first_tick(make_store, monkeypatch):
    # (c) The fast path is unchanged: a fresh order's transfer lands inside the trailing
    # window, so ONE tick finalizes it and the walk never reaches back beyond the window.
    monkeypatch.setattr(config, "AGGR_DEFERRED_ENABLED", True)
    oid = _chain_settling_order(make_store, "anchorfresh", price=1_000_000)
    head = 100_000
    tx = "0x" + "a1" * 32
    rpc = _ChainRpc(
        head=head,
        logs=[_transfer_log(PAYER, "0x" + "a" * 40, 1_000_000, head - 30, tx)],
        receipts={tx: _receipt(RELAYER)},
    )
    rpc.install()
    assert reconcile.reconcile_chain_tick() == 1
    with SessionLocal() as s:
        assert s.get(Order, oid).status == "delivered"
    # Anchored on the trailing window, never deeper, and completed in one tick.
    assert rpc.scanned[0][0] == head - config.AGGR_SETTLE_LOOKBACK_BLOCKS
    assert len(rpc.scanned) <= config.RECONCILE_MAX_WINDOWS
    assert reconcile.LAST_CHAIN_SCAN_MONO > 0


@respx.mock
def test_old_order_anchors_back_and_walks_forward_to_its_transfer(
    make_store, monkeypatch
):
    # (a) A 2h-old settling order: the anchor reaches ~9600 blocks back (2h at 1 block/s
    # x1.25 + 600), far beyond the 1200-block trailing window, and successive ticks walk
    # forward until the transfer is covered.
    import datetime

    monkeypatch.setattr(config, "AGGR_DEFERRED_ENABLED", True)
    oid = _chain_settling_order(make_store, "anchorold", price=1_000_000)
    age = datetime.timedelta(hours=2)
    _age_order(oid, age)
    head = 100_000
    anchor = _anchor_for(head, age)
    tx = "0x" + "a2" * 32
    # Sits in the THIRD tick's slice: anchor + 2 * _TICK_SPAN .. + 3 * _TICK_SPAN.
    block = anchor + 2 * _TICK_SPAN + 500
    rpc = _ChainRpc(
        head=head,
        logs=[_transfer_log(PAYER, "0x" + "a" * 40, 1_000_000, block, tx)],
        receipts={tx: _receipt(RELAYER)},
    )
    rpc.install()
    assert (
        head - block > config.AGGR_SETTLE_LOOKBACK_BLOCKS
    )  # a head scan cannot see it

    assert reconcile.reconcile_chain_tick() == 0
    # Anchored at (or just before) the order, far deeper than the trailing window.
    assert rpc.scanned[0][0] <= anchor
    assert rpc.scanned[0][0] < head - config.AGGR_SETTLE_LOOKBACK_BLOCKS
    assert reconcile.LAST_CHAIN_SCAN_MONO == 0  # a partial walk is NOT a completed scan
    assert reconcile.reconcile_chain_tick() == 0
    with SessionLocal() as s:
        assert s.get(Order, oid).status == "settling"

    assert reconcile.reconcile_chain_tick() == 1  # third tick covers the transfer
    with SessionLocal() as s:
        o = s.get(Order, oid)
        assert o.status == "delivered"
        assert o.tx_hash == tx
        assert o.settle_ref == tx
    # Forward progress: later slices, never a re-walk of the first one.
    assert rpc.scanned[0][0] < rpc.scanned[-1][0]


@respx.mock
def test_restart_forgets_the_cursor_and_re_anchors(make_store, monkeypatch):
    # (b) The cursors are process-local on purpose. After a restart mid-backlog the maps
    # are empty, so the anchor is recomputed from the order's age and the walk restarts —
    # an arbitrarily old settlement is always eventually reachable.
    import datetime

    monkeypatch.setattr(config, "AGGR_DEFERRED_ENABLED", True)
    oid = _chain_settling_order(make_store, "anchorboot", price=1_000_000)
    age = datetime.timedelta(hours=2)
    _age_order(oid, age)
    head = 100_000
    anchor = _anchor_for(head, age)
    tx = "0x" + "a3" * 32
    block = anchor + 2 * _TICK_SPAN + 500
    rpc = _ChainRpc(
        head=head,
        logs=[_transfer_log(PAYER, "0x" + "a" * 40, 1_000_000, block, tx)],
        receipts={tx: _receipt(RELAYER)},
    )
    rpc.install()
    assert reconcile.reconcile_chain_tick() == 0  # one tick of progress, then...
    reconcile._reset_state()  # ...process restart: cursors gone
    rpc.scanned.clear()

    assert reconcile.reconcile_chain_tick() == 0
    # Re-anchored from scratch off the order's age, not resumed and not head-anchored.
    assert rpc.scanned[0][0] <= anchor
    assert rpc.scanned[0][0] < head - config.AGGR_SETTLE_LOOKBACK_BLOCKS
    assert reconcile.reconcile_chain_tick() == 0
    assert reconcile.reconcile_chain_tick() == 1
    with SessionLocal() as s:
        assert s.get(Order, oid).status == "delivered"


@respx.mock
def test_reap_tick_holds_order_while_the_backlog_walk_is_incomplete(
    make_store, monkeypatch
):
    # The reaper must not read a PARTIAL forward walk as "scanned, nothing found". An
    # order past the deferred deadline whose transfer sits in a not-yet-walked slice
    # survives every tick until the walk actually reaches it.
    import datetime

    monkeypatch.setattr(config, "AGGR_DEFERRED_ENABLED", True)
    oid = _chain_settling_order(make_store, "reapbacklog", price=1_000_000)
    age = agentic.DEFERRED_REAP_AFTER + datetime.timedelta(hours=1)
    _age_order(oid, age)
    head = 500_000
    anchor = _anchor_for(head, age)
    tx = "0x" + "a4" * 32
    block = anchor + 2 * _TICK_SPAN + 500
    rpc = _ChainRpc(
        head=head,
        logs=[_transfer_log(PAYER, "0x" + "a" * 40, 1_000_000, block, tx)],
        receipts={tx: _receipt(RELAYER)},
    )
    rpc.install()
    for _ in range(2):
        agentic._reap_tick()
        with SessionLocal() as s:
            assert s.get(Order, oid).status == "settling"  # mid-walk: never voided
    agentic._reap_tick()  # the walk reaches the transfer
    with SessionLocal() as s:
        o = s.get(Order, oid)
        assert o.status == "delivered"
        assert o.tx_hash == tx
    assert "agent_order.reaped" not in _events(oid)

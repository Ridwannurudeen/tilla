"""M15.3 — provider gate conformance harness.

A reusable suite EVERY registered PaymentRailProvider / DeliveryProvider must pass,
run parametrized over the LIVE registry so neither a future provider nor drift in a
built-in seam can ship without meeting the invariants:

  - delivery ``mint`` is PAYLOAD-ONLY and reproduces exactly what a REAL
    ``checkout.deliver()`` run writes to ``Delivery.payload`` — compared against the
    actual row, never a hardcoded constant, so drift in ``deliver`` itself trips the
    golden (the 15.1-review warning recorded in ``app/providers.py``).
  - delivery ``revoke`` is idempotent (first ``True``, then ``False``).
  - payment ``record_settlement`` is idempotent (same PAYMENT-RESPONSE twice settles
    the order exactly once).
  - a ``pre_settle_gate`` may only NARROW: a ``>=400`` gate forces the settle path to
    be skipped and NO settle HTTP call fires (the ``agent_buy`` dead-store invariant);
    a built-in gate never originates a settlement.
"""

import httpx
import pytest
import respx
from sqlalchemy import func, select
from x402.http.utils import encode_payment_response_header
from x402.schemas import SettleResponse

from app import agentic, checkout, providers
from app.db import SessionLocal
from app.models import Deliverable, Delivery, Entitlement, EventLog, Order, Store

PAYER = "0x" + "3" * 40
NONCE = "0x" + "1" * 64
SETTLE_HEADER = encode_payment_response_header(
    SettleResponse(success=True, transaction="0x" + "e" * 64, network="eip155:196")
)


def _is_license_key(value: str) -> bool:
    return value.startswith("TILLA-") and len(value.split("-")) == 5


def _confirmed_order(store_id: int, oid: str) -> None:
    with SessionLocal() as s:
        s.add(
            Order(
                id=oid,
                store_id=store_id,
                pay_to="0x" + "a" * 40,
                amount_micro=1_000_000,
                expected_micro=1_000_000,
                status="confirmed",
            )
        )
        s.commit()


def _setup_store_for_kind(make_store, kind: str) -> int:
    """A store whose real ``checkout.deliver()`` takes the ``kind`` branch: legacy
    text has no deliverable; file/license need an active deliverable of that kind."""
    sid = make_store(slug=f"conf-{kind}", delivery="conformance-secret-text")
    if kind in ("file", "license"):
        with SessionLocal() as s:
            s.add(Deliverable(store_id=sid, kind=kind, active=True, payload="p"))
            s.commit()
    return sid


# ---------------------------------------------------- every registry entry conforms
def test_every_registered_provider_conforms_to_protocol():
    assert providers.DELIVERY_PROVIDERS  # non-empty registry
    assert providers.PAYMENT_RAIL_PROVIDERS
    for p in providers.DELIVERY_PROVIDERS.values():
        assert isinstance(p, providers.DeliveryProvider)
    for p in providers.PAYMENT_RAIL_PROVIDERS.values():
        assert isinstance(p, providers.PaymentRailProvider)


# ----------------------------- delivery mint == a REAL deliver() Delivery.payload
@pytest.mark.parametrize("kind", sorted(providers.DELIVERY_PROVIDERS))
def test_delivery_mint_matches_real_deliver(make_store, kind):
    sid = _setup_store_for_kind(make_store, kind)
    provider = providers.DELIVERY_PROVIDERS[kind]

    # run the REAL deliver() on one order and capture the payload it persisted
    _confirmed_order(sid, oid_a := f"conf-{kind}-a")
    with SessionLocal() as s:
        checkout.deliver(s, s.get(Order, oid_a))
        s.commit()
        deliver_payload = s.scalar(
            select(Delivery.payload).where(Delivery.order_id == oid_a)
        )
        deliver_kind = s.scalar(select(Delivery.kind).where(Delivery.order_id == oid_a))
    assert deliver_kind == kind

    # the provider's payload-only mint must reproduce it
    _confirmed_order(sid, oid_b := f"conf-{kind}-b")
    with SessionLocal() as s:
        mint_out = provider.mint(s, s.get(Order, oid_b))
        # mint is side-effect-free: order B is untouched, no Delivery row written
        assert s.get(Order, oid_b).status == "confirmed"
        assert s.scalar(select(Delivery.id).where(Delivery.order_id == oid_b)) is None

    if kind == "license":
        # both are independently minted keys — same generator, so identical shape
        # (not value); this still trips if deliver's key format ever drifts.
        assert _is_license_key(mint_out) and _is_license_key(deliver_payload)
        assert mint_out != deliver_payload
    else:
        assert mint_out == deliver_payload


# ------------------------------------------------------ delivery revoke idempotent
@pytest.mark.parametrize("kind", sorted(providers.DELIVERY_PROVIDERS))
def test_delivery_revoke_idempotent(make_store, kind):
    sid = make_store(slug=f"conf-rev-{kind}")
    provider = providers.DELIVERY_PROVIDERS[kind]
    _confirmed_order(sid, oid := f"conf-rev-{kind}-o")
    with SessionLocal() as s:
        d = Deliverable(store_id=sid, kind="text", payload="x", active=True)
        s.add(d)
        s.flush()
        s.add(Entitlement(order_id=oid, deliverable_id=d.id))
        s.commit()
    with SessionLocal() as s:
        order = s.get(Order, oid)
        assert provider.revoke(s, order) is True  # first revoke lands
        assert provider.revoke(s, order) is False  # idempotent no-op
        s.commit()


# ------------------------------------------------ payment record_settlement idempotent
@pytest.mark.parametrize("scheme", sorted(providers.PAYMENT_RAIL_PROVIDERS))
def test_record_settlement_idempotent(make_store, scheme):
    provider = providers.PAYMENT_RAIL_PROVIDERS[scheme]
    sid = make_store(slug=f"conf-set-{scheme}", price_micro=1_000_000, delivery="X")
    with SessionLocal() as s:
        store = s.get(Store, sid)
        product = agentic._active_product(s, sid)
        order, _ = agentic.fulfill_agent_order(s, store, product, PAYER, NONCE)
        s.commit()
        oid = order.id
        assert s.get(Order, oid).status == "settling"

    provider.record_settlement(oid, SETTLE_HEADER, scheme)
    provider.record_settlement(oid, SETTLE_HEADER, scheme)  # replay

    with SessionLocal() as s:
        assert s.get(Order, oid).status == "delivered"
        # the settling->delivered flip (and its accrual/webhook side effects) fired once
        settled = s.scalar(
            select(func.count())
            .select_from(EventLog)
            .where(EventLog.event == "agent_order.settled", EventLog.order_id == oid)
        )
        assert settled == 1


def test_builtin_gate_never_originates():
    # A built-in payment rail may only NARROW (veto >=400) — never originate a
    # settlement — so every built-in gate returns None on a clean call.
    for provider in providers.PAYMENT_RAIL_PROVIDERS.values():
        assert provider.pre_settle_gate() is None


# ---------------------------------------------- gate veto skips settle (no HTTP call)
_FACILITATOR = "https://facilitator.conformance.test/settle"


class _VetoRail:
    """A conformance stub whose gate vetoes: its ``record_settlement`` must never be
    reached, and no settle HTTP call may fire."""

    scheme = "veto"

    def build_accepts(self, rail):
        return []

    def pre_settle_gate(self, *args, **kwargs):
        return 409

    def record_settlement(self, order_id, header, scheme=None):
        raise AssertionError("record_settlement must not run after a >=400 gate")


class _PassRail:
    """A stub whose gate clears: its settle DOES fire (one facilitator call), proving
    the harness assertion is not vacuously true."""

    scheme = "pass"

    def build_accepts(self, rail):
        return []

    def pre_settle_gate(self, *args, **kwargs):
        return None

    def record_settlement(self, order_id, header, scheme=None):
        httpx.post(_FACILITATOR, json={"order": order_id})


def _guarded_settle(provider, order_id: str, header: str) -> str:
    """The agent_buy dead-store invariant, isolated: a ``>=400`` gate returns early
    (dead store -> skip settle), so the settle path never runs; only a clear gate
    lets ``record_settlement`` (and any facilitator HTTP call) fire."""
    code = provider.pre_settle_gate()
    if code is not None and code >= 400:
        return "vetoed"
    provider.record_settlement(order_id, header)
    return "settled"


@respx.mock
def test_provider_gate_blocks_settle():
    route = respx.post(_FACILITATOR).mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    # a >=400 gate: settle skipped, ZERO facilitator calls
    assert _guarded_settle(_VetoRail(), "o-veto", SETTLE_HEADER) == "vetoed"
    assert route.call_count == 0
    # a clear gate: settle fires exactly once (guards against a vacuous pass)
    assert _guarded_settle(_PassRail(), "o-pass", SETTLE_HEADER) == "settled"
    assert route.call_count == 1

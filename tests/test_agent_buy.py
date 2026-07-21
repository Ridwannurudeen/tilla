"""M7 agent-buy tests: the x402 resolver hooks (sentinel-not-raise), the
deliver path + idempotency, the settle-failed void + replay recovery, the
reaper, settle bookkeeping, the route pattern, and non-interference with the
human unique-amount checkout. No network — the full x402 settle needs live
facilitator creds and is staged separately.
"""

import asyncio
import types
from datetime import timedelta

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from x402.http.utils import encode_payment_response_header
from x402.http.x402_http_server_base import x402HTTPServerBase
from x402.schemas import AssetAmount, SettleResponse

import app.main as main
from app import agentic, checkout, delivery
from app.db import SessionLocal
from app.models import Deliverable, Delivery, Entitlement, Order, Store
from app.payment import build_store_payment_option, load_payment_rail

client = TestClient(main.app)

SENTINEL = "0xf4c9fa07f3bb852547fdc4df7c1d9fd9991cfa51"
NONCE = "0x" + "1" * 64
NONCE2 = "0x" + "2" * 64
PAYER = "0x" + "3" * 40


def _store_product(sid):
    with SessionLocal() as s:
        store = s.get(Store, sid)
        product = agentic._active_product(s, sid)
        s.expunge_all()
        return store, product


def _add_deliverable(store_id, kind, **kw):
    with SessionLocal() as s:
        # replace any active deliverable, mirroring the deactivate-then-insert path
        d = Deliverable(store_id=store_id, kind=kind, active=True, **kw)
        s.add(d)
        s.commit()
        return d.id


# ------------------------------------------------------- resolver hooks
def test_resolve_pay_to_live_store_byte_identical(make_store):
    mixed = "0x" + "Ab" * 20  # mixed-case: must survive unchanged
    make_store(slug="rp1", pay_to=mixed)
    got = agentic.resolve_pay_to("/s/rp1/buy", SENTINEL)
    assert got == mixed  # no .lower(), no reformat


def test_resolve_price_live_store(make_store):
    make_store(slug="rp2", price_micro=1_234_567)
    price = agentic.resolve_price("/s/rp2/buy")
    assert isinstance(price, AssetAmount)
    assert price.amount == "1234567"
    assert price.asset == agentic.ASSET
    assert price.extra == {"name": "USD₮0", "version": "1"}


def test_resolve_unknown_slug_returns_sentinel():
    assert agentic.resolve_pay_to("/s/nope/buy", SENTINEL) == SENTINEL
    assert agentic.resolve_price("/s/nope/buy").amount == "1"


@pytest.mark.parametrize("status", ["pending_screening", "blocked"])
def test_resolve_non_live_returns_sentinel(make_store, status):
    make_store(slug="rp3", status=status)
    assert agentic.resolve_pay_to("/s/rp3/buy", SENTINEL) == SENTINEL
    assert agentic.resolve_price("/s/rp3/buy").amount == "1"


def test_resolve_malformed_path_returns_sentinel():
    assert agentic.resolve_pay_to("/not/a/buy", SENTINEL) == SENTINEL
    assert agentic.resolve_price("/api/checkout/x").amount == "1"


def test_resolvers_deterministic(make_store):
    make_store(slug="rp4", pay_to="0x" + "c" * 40, price_micro=7_000_000)
    assert agentic.resolve_pay_to("/s/rp4/buy", SENTINEL) == agentic.resolve_pay_to(
        "/s/rp4/buy", SENTINEL
    )
    assert (
        agentic.resolve_price("/s/rp4/buy").amount
        == agentic.resolve_price("/s/rp4/buy").amount
    )


# ------------------------------------------------------- route pattern
def test_buy_route_pattern_matches_only_single_segment():
    verb, path, rx = x402HTTPServerBase._parse_route_pattern("POST /s/:slug/buy")
    assert verb == "POST"
    assert rx.match("/s/foo-1/buy")
    assert not rx.match("/s/foo/bar/buy")  # ':slug' is [^/]+, no cross-slash
    assert not rx.match("/s/foo/")
    assert not rx.match("/api/checkout/x")


# ------------------------------------------------------- deliver + idempotency
def test_fulfill_creates_agent_order_and_delivers_text(make_store):
    sid = make_store(slug="b1", price_micro=1_000_000, delivery="SECRET-TEXT")
    store, product = _store_product(sid)
    with SessionLocal() as s:
        store = s.merge(store)
        product = s.merge(product)
        order, body = agentic.fulfill_agent_order(s, store, product, PAYER, NONCE)
        s.commit()
        assert order.channel == "agent"
        # goods minted in-band, but held non-terminal until the x402 settle confirms
        assert order.status == "settling"
        assert order.expected_micro == order.amount_micro == 1_000_000
        assert order.x402_nonce == NONCE
        assert order.from_addr == PAYER
        assert body["order_id"] == order.id
        assert body["kind"] == "text"
        assert body["delivery"] == "SECRET-TEXT"  # legacy store.delivery path
        assert body["tx"] == "pending"
        assert s.scalar(select(Delivery).where(Delivery.order_id == order.id))


def test_fulfill_file_deliverable_returns_download_url(make_store):
    sid = make_store(slug="b2", price_micro=1_000_000)
    _add_deliverable(sid, "file", file_sha256="a" * 64, file_name="x.pdf", file_size=10)
    store, product = _store_product(sid)
    with SessionLocal() as s:
        order, body = agentic.fulfill_agent_order(
            s, s.merge(store), s.merge(product), PAYER, NONCE
        )
        s.commit()
        assert body["kind"] == "file"
        assert "download_url" in body and "/api/download/" in body["download_url"]
        assert "license_key" not in body


def test_fulfill_license_deliverable_returns_license_key(make_store):
    sid = make_store(slug="b3", price_micro=1_000_000)
    _add_deliverable(sid, "license", max_activations=3)
    store, product = _store_product(sid)
    with SessionLocal() as s:
        order, body = agentic.fulfill_agent_order(
            s, s.merge(store), s.merge(product), PAYER, NONCE
        )
        s.commit()
        assert body["kind"] == "license"
        assert body["license_key"].startswith("TILLA-")


def test_fulfill_idempotent_same_nonce(make_store):
    sid = make_store(slug="b4", price_micro=1_000_000, delivery="ONCE")
    store, product = _store_product(sid)
    with SessionLocal() as s:
        o1, body1 = agentic.fulfill_agent_order(
            s, s.merge(store), s.merge(product), PAYER, NONCE
        )
        s.commit()
    with SessionLocal() as s:
        o2, body2 = agentic.fulfill_agent_order(
            s, s.merge(store), s.merge(product), PAYER, NONCE
        )
        s.commit()
    assert o1.id == o2.id
    assert body1 == body2
    with SessionLocal() as s:
        assert (
            s.scalar(select(func.count(Order.id)).where(Order.x402_nonce == NONCE)) == 1
        )
        assert s.scalar(select(func.count(Delivery.id))) == 1
        assert s.scalar(select(func.count(Entitlement.id))) == 0  # no deliverable


def test_fulfill_same_nonce_different_payer_409(make_store):
    # A different payer replaying a victim's (public) nonce on the same store must
    # not receive the victim's order — the payment middleware skips settle on 409.
    sid = make_store(slug="np1", price_micro=1_000_000, delivery="VICTIM")
    store, product = _store_product(sid)
    with SessionLocal() as s:
        agentic.fulfill_agent_order(s, s.merge(store), s.merge(product), PAYER, NONCE)
        s.commit()
    attacker = "0x" + "4" * 40
    with SessionLocal() as s:
        with pytest.raises(HTTPException) as e:
            agentic.fulfill_agent_order(
                s, s.merge(store), s.merge(product), attacker, NONCE
            )
        assert e.value.status_code == 409


def test_fulfill_same_nonce_different_store_409(make_store):
    # A nonce first used on store A, replayed against store B, must 409 (not settle
    # B's merchant while handing back A's deliverable).
    sid_a = make_store(slug="ns-a", price_micro=1_000_000, delivery="A")
    sid_b = make_store(slug="ns-b", price_micro=1_000_000, delivery="B")
    store_a, product_a = _store_product(sid_a)
    with SessionLocal() as s:
        agentic.fulfill_agent_order(
            s, s.merge(store_a), s.merge(product_a), PAYER, NONCE
        )
        s.commit()
    store_b, product_b = _store_product(sid_b)
    with SessionLocal() as s:
        with pytest.raises(HTTPException) as e:
            agentic.fulfill_agent_order(
                s, s.merge(store_b), s.merge(product_b), PAYER, NONCE
            )
        assert e.value.status_code == 409


def test_buy_endpoint_402_without_payment(make_store):
    make_store(slug="b5")
    r = client.post("/s/b5/buy")
    assert r.status_code == 402


def test_require_live_store_raises_404_and_409(make_store):
    make_store(slug="pend1", status="pending_screening")
    with SessionLocal() as s:
        with pytest.raises(HTTPException) as e:
            agentic._require_live_store(s, "does-not-exist")
        assert e.value.status_code == 404
        with pytest.raises(HTTPException) as e:
            agentic._require_live_store(s, "pend1")
        assert e.value.status_code == 409


# ------------------------------------------------------- settle-failed compensator
def test_settle_failed_voids_fresh_order_and_revokes(make_store):
    sid = make_store(slug="sf1", price_micro=1_000_000)
    _add_deliverable(sid, "file", file_sha256="a" * 64, file_name="x.pdf", file_size=10)
    store, product = _store_product(sid)
    with SessionLocal() as s:
        order, body = agentic.fulfill_agent_order(
            s, s.merge(store), s.merge(product), PAYER, NONCE
        )
        s.commit()
        oid = order.id
        token = body["download_url"].rsplit("/", 1)[-1]

    result = agentic.settle_failed_core(NONCE)
    assert result == {"error": "settlement_failed"}
    with SessionLocal() as s:
        o = s.get(Order, oid)
        assert o.status == "canceled"
        ent = s.scalar(select(Entitlement).where(Entitlement.order_id == oid))
        assert ent.revoked_at is not None
    # the minted download token now 403s (revoked entitlement)
    assert client.get(f"/api/download/{token}").status_code == 403


def test_settle_failed_recovered_body_has_no_secrets(make_store):
    # A replay after a lost response reaches settle_failed with the order already
    # settled (delivered). The recovered receipt must be NEUTRAL — never the
    # license key / download url / delivery payload (those stay behind the
    # sign-to-claim gate), since the EIP-3009 auth is rebuildable from public
    # settle calldata.
    sid = make_store(slug="sf2", price_micro=1_000_000, delivery="GOODS")
    _add_deliverable(sid, "license", max_activations=3)
    store, product = _store_product(sid)
    with SessionLocal() as s:
        order, _ = agentic.fulfill_agent_order(
            s, s.merge(store), s.merge(product), PAYER, NONCE
        )
        order.status = "delivered"  # an earlier settle landed (record_settlement)
        order.tx_hash = "0x" + "d" * 64
        s.commit()
        oid = order.id

    result = agentic.settle_failed_core(NONCE)
    assert result["recovered"] is True
    assert result["tx"] == "0x" + "d" * 64
    assert result["order_id"] == oid
    assert result["message"] == main.CLAIM_DELIVERY_MESSAGE
    assert "license_key" not in result
    assert "download_url" not in result
    assert "delivery" not in result
    with SessionLocal() as s:
        o = s.get(Order, oid)
        assert o.status == "delivered"  # NOT voided
        ent = s.scalar(select(Entitlement).where(Entitlement.order_id == oid))
        assert ent is None or ent.revoked_at is None


def test_settle_failed_unknown_nonce_is_inert():
    assert agentic.settle_failed_core("0x" + "9" * 64) == {"error": "settlement_failed"}


# ------------------------------------------------------- settle bookkeeping
def test_record_settlement_sets_tx_hash(make_store):
    sid = make_store(slug="rs1", price_micro=1_000_000, delivery="X")
    store, product = _store_product(sid)
    with SessionLocal() as s:
        order, _ = agentic.fulfill_agent_order(
            s, s.merge(store), s.merge(product), PAYER, NONCE
        )
        s.commit()
        oid = order.id
    header = encode_payment_response_header(
        SettleResponse(success=True, transaction="0x" + "e" * 64, network="eip155:196")
    )
    agentic.record_settlement(oid, header)
    with SessionLocal() as s:
        o = s.get(Order, oid)
        assert o.tx_hash == "0x" + "e" * 64
        assert o.status == "delivered"  # settling -> delivered on settle success


def test_record_settlement_delivers_even_when_header_unparsable(make_store):
    # A successful settle whose PAYMENT-RESPONSE header is undecodable/empty must
    # still flip settling -> delivered (a served 200+header IS the success signal),
    # so the reaper never voids a genuinely-paid order (finding #3).
    sid = make_store(slug="rs2", price_micro=1_000_000, delivery="X")
    store, product = _store_product(sid)
    with SessionLocal() as s:
        order, _ = agentic.fulfill_agent_order(
            s, s.merge(store), s.merge(product), PAYER, NONCE
        )
        s.commit()
        oid = order.id
    agentic.record_settlement(oid, "not-a-decodable-header")
    with SessionLocal() as s:
        o = s.get(Order, oid)
        assert o.status == "delivered"
        assert o.tx_hash is None
    # aged, but settled: the reaper leaves it alone
    with SessionLocal() as s:
        o = s.get(Order, oid)
        o.paid_at = checkout._now() - timedelta(minutes=30)
        s.commit()
    with SessionLocal() as s:
        assert agentic.reap_agent_orders(s) == 0
        assert s.get(Order, oid).status == "delivered"


def test_settling_order_not_claimable_via_library_until_settle(make_store):
    # Finding #1: an agent order with settle still pending is held 'settling', so
    # the out-of-band claim paths never surface the goods. /api/library (wallet
    # session for the payer) must return nothing until record_settlement lands.
    sid = make_store(slug="lib1", price_micro=1_000_000, delivery="TOP-SECRET")
    store, product = _store_product(sid)
    with SessionLocal() as s:
        order, _ = agentic.fulfill_agent_order(
            s, s.merge(store), s.merge(product), PAYER, NONCE
        )
        s.commit()
        oid = order.id
    with SessionLocal() as s:
        assert s.get(Order, oid).status == "settling"
    token = delivery.mint_session_token(PAYER)
    auth = {"Authorization": f"Bearer {token}"}
    pending = client.get("/api/library", headers=auth)
    assert pending.status_code == 200
    assert pending.json()["purchases"] == []  # settle pending -> nothing claimable
    # settle confirms: the order flips delivered and the deliverable appears
    header = encode_payment_response_header(
        SettleResponse(success=True, transaction="0x" + "e" * 64, network="eip155:196")
    )
    agentic.record_settlement(oid, header)
    settled = client.get("/api/library", headers=auth).json()["purchases"]
    assert len(settled) == 1
    assert settled[0]["order_id"] == oid
    assert settled[0]["delivery"] == "TOP-SECRET"


def test_guard_store_status(make_store):
    make_store(slug="g_live")
    make_store(slug="g_pend", status="pending_screening")
    assert agentic._guard_store_status("nope") == (404, "store not found")
    assert agentic._guard_store_status("g_pend")[0] == 409
    assert agentic._guard_store_status("g_live") is None


# ------------------------------------------------------- reaper
def test_reaper_voids_old_settling_agent_order(make_store):
    sid = make_store(slug="rp_old", price_micro=1_000_000)
    _add_deliverable(sid, "license", max_activations=3)
    store, product = _store_product(sid)
    with SessionLocal() as s:
        order, _ = agentic.fulfill_agent_order(
            s, s.merge(store), s.merge(product), PAYER, NONCE
        )
        order.paid_at = checkout._now() - timedelta(minutes=20)
        s.commit()
        old_id = order.id
    # a recent settling agent order stays put
    with SessionLocal() as s:
        store2 = s.get(Store, sid)
        product2 = agentic._active_product(s, sid)
        order2, _ = agentic.fulfill_agent_order(s, store2, product2, PAYER, NONCE2)
        s.commit()
        recent_id = order2.id

    with SessionLocal() as s:
        assert agentic.reap_agent_orders(s) == 1
        s.commit()
    with SessionLocal() as s:
        assert s.get(Order, old_id).status == "canceled"
        assert s.get(Order, recent_id).status == "settling"
        ent = s.scalar(select(Entitlement).where(Entitlement.order_id == old_id))
        assert ent.revoked_at is not None


def test_reaper_ignores_settled_and_human_orders(make_store):
    sid = make_store(slug="rp_mix", price_micro=1_000_000, delivery="X")
    store, product = _store_product(sid)
    # settled agent order (record_settlement flipped it delivered) — must not reap
    with SessionLocal() as s:
        order, _ = agentic.fulfill_agent_order(
            s, s.merge(store), s.merge(product), PAYER, NONCE
        )
        order.paid_at = checkout._now() - timedelta(minutes=30)
        order.status = "delivered"
        order.tx_hash = "0x" + "e" * 64
        s.commit()
        settled_id = order.id
    # a human (web) order, delivered + old, tx_hash None — must not be reaped
    with SessionLocal() as s:
        h = Order(
            id="humanorder00001",
            store_id=sid,
            pay_to="0x" + "a" * 40,
            amount_micro=1_000_000,
            expected_micro=1_002_000,
            status="delivered",
            channel="web",
            paid_at=checkout._now() - timedelta(minutes=60),
        )
        s.add(h)
        s.commit()
    with SessionLocal() as s:
        assert agentic.reap_agent_orders(s) == 0
    with SessionLocal() as s:
        assert s.get(Order, settled_id).status == "delivered"
        assert s.get(Order, "humanorder00001").status == "delivered"


# ------------------------------------------------------- non-interference (M3)
def test_agent_order_does_not_block_human_amount_allocation(make_store):
    sid = make_store(slug="ni1", price_micro=5_000_000, delivery="X")
    store, product = _store_product(sid)
    with SessionLocal() as s:
        store = s.merge(store)
        product = s.merge(product)
        agentic.fulfill_agent_order(s, store, product, PAYER, NONCE)  # exact price
        s.commit()
    # a human checkout on the same store still gets an offset amount (price+[1..])
    with SessionLocal() as s:
        store = s.get(Store, sid)
        product = agentic._active_product(s, sid)
        human = checkout.create_order(s, store, product)
        s.commit()
        assert human.status == "pending"
        assert human.expected_micro != 5_000_000
        assert 5_000_001 <= human.expected_micro <= 5_004_999


# ------------------------------------------------------- dynamic PaymentOption
def test_build_store_payment_option_resolves_live_and_sentinel(make_store):
    make_store(slug="dyn1", pay_to="0x" + "Ab" * 20, price_micro=2_500_000)
    rail = load_payment_rail({"PAY_TO_ADDRESS": SENTINEL})
    opt = build_store_payment_option(rail)
    live = types.SimpleNamespace(path="/s/dyn1/buy")
    ghost = types.SimpleNamespace(path="/s/ghost/buy")
    assert asyncio.run(opt.pay_to(live)) == "0x" + "Ab" * 20  # byte-identical
    assert asyncio.run(opt.price(live)).amount == "2500000"
    assert asyncio.run(opt.pay_to(ghost)) == SENTINEL  # dead store → sentinel
    assert asyncio.run(opt.price(ghost)).amount == "1"

"""M16 increment 16.1 — B2B wholesale tiers + verified quote.

Covers: the extended manage-key-gated ``POST /api/stores/{slug}/pricing`` tier
writes (+ validation rejects), the public rate-limited ``GET /s/{slug}/quote``
with read-only ERC-8004 owner verification (respx-mocked eth_call), the CORE
INVARIANT (INV-1) that a tier price applies ONLY when the payer wallet equals the
verified agent owner — otherwise base price, RPC outage included — and the rule
that tier tables never leak on feed/llms/MCP.
"""

import base64
import json
import time

import httpx
import pytest
import respx
from sqlalchemy import select
from x402.http.utils import encode_payment_response_header
from x402.schemas import SettleResponse

import app.main as main
from app import agentic, b2b, config, delivery
from app.db import SessionLocal
from app.models import EventLog, Order, Product, Store
from fastapi.testclient import TestClient

client = TestClient(main.app)

REGISTRY = "0x" + "1" * 40
OWNER = "0x" + "b" * 40
OTHER = "0x" + "c" * 40


@pytest.fixture(autouse=True)
def _reset_owner_cache():
    b2b._owner_cache.clear()
    yield
    b2b._owner_cache.clear()


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setattr(config, "ERC8004_REGISTRY", REGISTRY)


def _mock_owner(wallet: str):
    padded = "0x" + "0" * 24 + wallet.replace("0x", "")
    return respx.post(config.RPC_URL).mock(
        return_value=httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "result": padded}
        )
    )


def _mock_rpc_error():
    return respx.post(config.RPC_URL).mock(
        return_value=httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "m": "x"}}
        )
    )


def _give_key(slug: str) -> str:
    plain, key_hash = delivery.mint_manage_key()
    with SessionLocal() as s:
        store = s.scalar(select(Store).where(Store.slug == slug))
        store.manage_key_hash = key_hash
        s.commit()
    return plain


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _product(slug: str) -> Product:
    with SessionLocal() as s:
        store = s.scalar(select(Store).where(Store.slug == slug))
        p = agentic._active_product(s, store.id)
        s.expunge_all()
        return p


def _set_tiers(slug: str, tiers, model="one_time", params=None):
    key = _give_key(slug)
    body = {"pricing_model": model, "tiers": tiers}
    if params is not None:
        body["params"] = params
    return client.post(f"/api/stores/{slug}/pricing", json=body, headers=_auth(key))


TIER_6961 = {"buyer": "erc8004:6961", "price_micro": 5_000_000}
TIER_ANY = {"buyer": "any_agent", "price_micro": 7_000_000}


# --------------------------------------------------------------- tier writes
def test_pricing_accepts_tiers(make_store):
    make_store(slug="b1", price_micro=9_000_000)
    r = _set_tiers("b1", [TIER_6961, TIER_ANY])
    assert r.status_code == 200
    assert r.json()["tiers"] == [TIER_6961, TIER_ANY]
    assert _product("b1").pricing_params["tiers"] == [TIER_6961, TIER_ANY]


def test_pricing_rejects_tier_above_base_price(make_store):
    make_store(slug="babove", price_micro=9_000_000)
    r = _set_tiers("babove", [{"buyer": "any_agent", "price_micro": 9_000_001}])
    assert r.status_code == 422
    assert "exceed the base" in r.text


def test_pricing_canonicalizes_leading_zero_agent_id(make_store):
    make_store(slug="bzeros", price_micro=9_000_000)
    r = _set_tiers("bzeros", [{"buyer": "erc8004:0007", "price_micro": 5_000_000}])
    assert r.status_code == 200
    assert _product("bzeros").pricing_params["tiers"][0]["buyer"] == "erc8004:7"


def test_tiers_coexist_with_model_params(make_store):
    make_store(slug="b2")
    metered = {"unit": "token", "price_per_unit_micro": 1000, "min_deposit_micro": 1}
    r = _set_tiers("b2", [TIER_6961], model="metered", params=metered)
    assert r.status_code == 200
    stored = _product("b2").pricing_params
    assert stored["tiers"] == [TIER_6961]
    assert stored["unit"] == "token"


def test_pricing_clears_tiers_when_omitted(make_store):
    make_store(slug="b3")
    _set_tiers("b3", [TIER_6961])
    key = _give_key("b3")
    r = client.post(
        "/api/stores/b3/pricing",
        json={"pricing_model": "one_time"},
        headers=_auth(key),
    )
    assert r.status_code == 200
    assert _product("b3").pricing_params is None


def test_pricing_rejects_too_many_tiers(make_store):
    make_store(slug="b4")
    tiers = [{"buyer": f"erc8004:{i}", "price_micro": 5_000_000} for i in range(21)]
    assert _set_tiers("b4", tiers).status_code == 422


def test_pricing_rejects_duplicate_buyer(make_store):
    make_store(slug="b5")
    assert _set_tiers("b5", [TIER_6961, dict(TIER_6961)]).status_code == 422


def test_pricing_rejects_bad_buyer(make_store):
    make_store(slug="b6")
    bad = {"buyer": "0xdeadbeef", "price_micro": 5_000_000}
    assert _set_tiers("b6", [bad]).status_code == 422


def test_pricing_rejects_below_min_price(make_store):
    make_store(slug="b7")
    bad = {"buyer": "any_agent", "price_micro": 9_999}
    assert _set_tiers("b7", [bad]).status_code == 422


def test_pricing_rejects_float_price(make_store):
    make_store(slug="b8")
    bad = {"buyer": "any_agent", "price_micro": 5_000_000.5}
    assert _set_tiers("b8", [bad]).status_code == 422


def test_pricing_tiers_require_manage_key(make_store):
    make_store(slug="b9")
    r = client.post(
        "/api/stores/b9/pricing",
        json={"pricing_model": "one_time", "tiers": [TIER_ANY]},
    )
    assert r.status_code == 401


def test_tier_config_idor_blocked(make_store):
    """Another merchant's manage key cannot write tiers to this store."""
    make_store(slug="b10")
    make_store(slug="b10b", pay_to="0x" + "d" * 40)
    other_key = _give_key("b10b")
    r = client.post(
        "/api/stores/b10/pricing",
        json={"pricing_model": "one_time", "tiers": [TIER_ANY]},
        headers=_auth(other_key),
    )
    assert r.status_code == 401


# --------------------------------------------------------------- quote
@respx.mock
def test_quote_returns_tier_for_verified_agent(make_store, registry):
    make_store(slug="q1", price_micro=9_000_000)
    _set_tiers("q1", [TIER_6961])
    _mock_owner(OWNER)
    r = client.get("/s/q1/quote?agent_id=6961")
    assert r.status_code == 200
    body = r.json()
    assert body["base_price_micro"] == 9_000_000
    assert body["tier_price_micro"] == 5_000_000
    assert body["owner"] == OWNER
    assert "settle payer wallet" in body["requires"]


@respx.mock
def test_quote_any_agent_tier(make_store, registry):
    make_store(slug="q2")
    _set_tiers("q2", [TIER_ANY])
    _mock_owner(OWNER)
    body = client.get("/s/q2/quote?agent_id=12345").json()
    assert body["tier_price_micro"] == 7_000_000


def test_quote_no_tier_returns_base_only(make_store):
    make_store(slug="q3", price_micro=9_000_000)
    _set_tiers("q3", [TIER_6961])
    # a different agent id with no matching tier -> base only, no RPC needed
    body = client.get("/s/q3/quote?agent_id=999").json()
    assert body["base_price_micro"] == 9_000_000
    assert "tier_price_micro" not in body


@respx.mock
def test_quote_rpc_outage_base_price(make_store, registry):
    make_store(slug="q4")
    _set_tiers("q4", [TIER_6961])
    _mock_rpc_error()
    body = client.get("/s/q4/quote?agent_id=6961").json()
    assert "tier_price_micro" not in body
    assert body["base_price_micro"] == 9_000_000


def test_quote_unconfigured_registry_base_price(make_store):
    make_store(slug="q5")
    _set_tiers("q5", [TIER_6961])
    body = client.get("/s/q5/quote?agent_id=6961").json()  # registry unset -> None
    assert "tier_price_micro" not in body


def test_quote_bad_agent_id_422(make_store):
    make_store(slug="q6")
    assert client.get("/s/q6/quote?agent_id=notanid").status_code == 422


def test_quote_unknown_store_404():
    assert client.get("/s/ghost/quote?agent_id=1").status_code == 404


# --------------------------------------------------------------- INV-1 core
@respx.mock
def test_tier_claim_wrong_wallet_pays_base(make_store, registry):
    """CORE INVARIANT: presenting agent 6961 (tier 5 USDT) but settling from a
    wallet that is NOT the agent's on-chain owner => base price, tier NOT applied."""
    make_store(slug="i1", price_micro=9_000_000)
    _set_tiers("i1", [TIER_6961])
    _mock_owner(OWNER)
    product = _product("i1")
    # payer == owner -> tier applies
    price, applied = b2b.effective_price_micro(product, 6961, OWNER)
    assert (price, applied) == (5_000_000, True)
    # payer != owner -> base, never a discount on trust
    price, applied = b2b.effective_price_micro(product, 6961, OTHER)
    assert (price, applied) == (9_000_000, False)


@respx.mock
def test_effective_price_rpc_outage_base(make_store, registry):
    make_store(slug="i2", price_micro=9_000_000)
    _set_tiers("i2", [TIER_6961])
    _mock_rpc_error()
    price, applied = b2b.effective_price_micro(_product("i2"), 6961, OWNER)
    assert (price, applied) == (9_000_000, False)


def test_effective_price_no_agent_id_base(make_store):
    make_store(slug="i3", price_micro=9_000_000)
    _set_tiers("i3", [TIER_ANY])
    price, applied = b2b.effective_price_micro(_product("i3"), None, OWNER)
    assert (price, applied) == (9_000_000, False)


def test_verify_agent_owner_unconfigured_returns_none():
    assert b2b.verify_agent_owner(6961) is None  # registry empty by default


# --------------------------------------------------------------- no leak
def test_feed_leaks_no_tiers(make_store):
    make_store(slug="l1")
    _set_tiers("l1", [TIER_6961, TIER_ANY])
    raw = client.get("/s/l1/feed.json").text
    assert "erc8004:6961" not in raw
    assert "5000000" not in raw
    assert "7000000" not in raw
    prod = client.get("/s/l1/feed.json").json()["products"][0]
    assert "tiers" not in prod["pricing"]["params"]
    assert prod["pricing"]["wholesale"] is True


def test_mcp_and_llms_leak_no_tiers(make_store):
    make_store(slug="l2")
    _set_tiers("l2", [TIER_6961])
    pid = _product("l2").id
    r = client.post(
        "/s/l2/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "get_product", "arguments": {"product_id": pid}},
        },
    )
    assert "erc8004:6961" not in r.text
    assert "5000000" not in r.text
    sc = r.json()["result"]["structuredContent"]
    assert "tiers" not in sc["pricing"]["params"]
    assert sc["pricing"]["wholesale"] is True
    assert "quote" not in sc  # no agent_id => no per-buyer quote echoed
    assert "erc8004" not in client.get("/s/l2/llms.txt").text


# ================================================================ 16.2 tiered buy
NONCE_A = "0x" + "a" * 64
NONCE_B = "0x" + "b" * 64
NONCE_C = "0x" + "c" * 64
NONCE_D = "0x" + "d" * 64
NONCE_E = "0x" + "e" * 64


def _settle_header():
    return encode_payment_response_header(
        SettleResponse(success=True, transaction="0x" + "e" * 64, network="eip155:196")
    )


def _fulfill(slug: str, payer: str, nonce: str, agent_id: int | None = None):
    """Drive the real fulfill path (order born at the INV-1 effective price)."""
    with SessionLocal() as s:
        store = s.scalar(select(Store).where(Store.slug == slug))
        product = agentic._active_product(s, store.id)
        order, body = agentic.fulfill_agent_order(
            s, store, product, payer, nonce, None, agent_id
        )
        s.commit()
        return order.id, order.expected_micro, body


def _settled_data(order_id: str) -> dict | None:
    with SessionLocal() as s:
        ev = s.scalar(
            select(EventLog).where(
                EventLog.order_id == order_id,
                EventLog.event == "agent_order.settled",
            )
        )
        return ev.data if ev else None


def _status(order_id: str) -> str:
    with SessionLocal() as s:
        return s.get(Order, order_id).status


# --------------------------------------------------------------- resolve_price
@respx.mock
def test_resolve_price_tier_for_verified_payer(make_store, registry):
    make_store(slug="s7", price_micro=9_000_000)
    _set_tiers("s7", [TIER_6961])
    _mock_owner(OWNER)
    # verified owner as payer => tier baked into the 402 challenge amount
    assert agentic.resolve_price("/s/s7/buy", "6961", OWNER).amount == "5000000"
    # a payer that is NOT the agent owner => base (fail-to-base, no discount)
    assert agentic.resolve_price("/s/s7/buy", "6961", OTHER).amount == "9000000"


def test_resolve_price_default_no_agent_id_byte_identical(make_store):
    """GOLDEN: a tiered store with NO agent_id resolves the base price exactly."""
    make_store(slug="s8", price_micro=9_000_000)
    _set_tiers("s8", [TIER_6961, TIER_ANY])
    assert agentic.resolve_price("/s/s8/buy").amount == "9000000"
    assert agentic.resolve_price("/s/s8/buy", None, OWNER).amount == "9000000"


def test_payer_from_payment_header():
    data = {
        "x402Version": 1,
        "scheme": "exact",
        "network": "eip155:196",
        "payload": {"authorization": {"from": "0xABCdef01", "nonce": "0x11"}},
    }
    header = base64.b64encode(json.dumps(data).encode()).decode()
    assert agentic.payer_from_payment_header(header) == "0xabcdef01"
    assert agentic.payer_from_payment_header(None) is None
    assert agentic.payer_from_payment_header("not-decodable") is None


# --------------------------------------------------------------- settle re-derive
@respx.mock
def test_settle_price_rederived(make_store, registry):
    """A tiered buy by the verified owner: order born at the tier price, and the
    settle seam re-derives (fresh read) tier_applied=True."""
    make_store(slug="s1", price_micro=9_000_000)
    _set_tiers("s1", [TIER_6961])
    _mock_owner(OWNER)
    oid, price, _ = _fulfill("s1", OWNER, NONCE_A, 6961)
    assert price == 5_000_000
    agentic.record_settlement(oid, _settle_header(), None, 6961)
    data = _settled_data(oid)
    assert data["tier_applied"] is True
    assert data["agent_id"] == 6961
    assert "tier_discrepancy" not in data
    assert _status(oid) == "delivered"


@respx.mock
def test_tier_buy_mismatched_payer_records_base(make_store, registry):
    """Adversarial WRONG PAYER: presenting the agent id but paying from a non-owner
    wallet => order born at BASE and settles at base, tier never applied."""
    make_store(slug="s2", price_micro=9_000_000)
    _set_tiers("s2", [TIER_6961])
    _mock_owner(OWNER)
    oid, price, _ = _fulfill("s2", OTHER, NONCE_B, 6961)
    assert price == 9_000_000
    agentic.record_settlement(oid, _settle_header(), None, 6961)
    data = _settled_data(oid)
    assert data["tier_applied"] is False
    assert "tier_discrepancy" not in data  # settled AT base — no floor breach
    assert _status(oid) == "delivered"


@respx.mock
def test_identity_rpc_outage_base_price(make_store, registry):
    """RPC outage => base price at create AND at settle (fail-to-base, never a
    discount on an unverifiable owner)."""
    make_store(slug="s3", price_micro=9_000_000)
    _set_tiers("s3", [TIER_6961])
    _mock_rpc_error()
    oid, price, _ = _fulfill("s3", OWNER, NONCE_C, 6961)
    assert price == 9_000_000
    agentic.record_settlement(oid, _settle_header(), None, 6961)
    assert _settled_data(oid)["tier_applied"] is False


@respx.mock
def test_settle_stale_cache_owner_flagged(make_store, registry):
    """Adversarial CACHE-STALE OWNER: a stale positive cache entry lets the create
    seam grant the tier, but the settle seam's FRESH read (the registry now returns
    a different owner) catches it — tier_applied=False and the below-base settle is
    flagged as a discrepancy (funds already moved; the honest record is what was
    paid, never a certified discount)."""
    make_store(slug="s4", price_micro=9_000_000)
    _set_tiers("s4", [TIER_6961])
    # stale cache: OWNER remembered as agent 6961's owner though the chain moved on
    b2b._owner_cache[6961] = (OWNER, time.monotonic() + 100)
    _mock_owner(OTHER)  # a FRESH read returns the real, now-different owner
    oid, price, _ = _fulfill("s4", OWNER, NONCE_D, 6961)
    assert price == 5_000_000  # tier granted off the stale cache
    agentic.record_settlement(oid, _settle_header(), None, 6961)
    data = _settled_data(oid)
    assert data["tier_applied"] is False  # fresh read overrode the stale cache
    assert data["tier_discrepancy"] is True
    assert data["base_micro"] == 9_000_000
    assert data["paid_micro"] == 5_000_000


@respx.mock
def test_replayed_nonce_different_agent_id_keeps_original_price(make_store, registry):
    """Adversarial REPLAY: replaying the nonce with a DIFFERENT agent_id returns the
    original order at its original price — the tier is never re-derived on replay."""
    make_store(slug="s5", price_micro=9_000_000)
    _set_tiers("s5", [TIER_6961, TIER_ANY])
    _mock_owner(OWNER)
    oid1, price1, _ = _fulfill("s5", OWNER, NONCE_E, 6961)
    assert price1 == 5_000_000
    oid2, price2, _ = _fulfill("s5", OWNER, NONCE_E, 12345)  # any_agent tier = 7M
    assert oid2 == oid1
    assert price2 == 5_000_000  # unchanged — no re-quote on a replayed authorization


# --------------------------------------------------------------- MCP quote echo
@respx.mock
def test_mcp_quote_fields(make_store, registry):
    make_store(slug="s6", price_micro=9_000_000)
    _set_tiers("s6", [TIER_6961])
    _mock_owner(OWNER)
    pid = _product("s6").id
    r = client.post(
        "/s/s6/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "get_product",
                "arguments": {"product_id": pid, "agent_id": "6961"},
            },
        },
    )
    sc = r.json()["result"]["structuredContent"]
    assert sc["quote"]["agent_id"] == 6961
    assert sc["quote"]["tier_price_micro"] == 5_000_000
    assert sc["quote"]["owner"] == OWNER
    assert "settle payer wallet" in sc["quote"]["requires"]
    # the public pricing block still never leaks the tier table
    assert "tiers" not in sc["pricing"]["params"]

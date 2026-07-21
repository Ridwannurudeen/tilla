"""M8 payment-method declaration tests: the manage-key-gated
``POST /api/stores/{slug}/pricing`` endpoint, per-model param validation, and the
additive feed/MCP/llms surfacing (pricing metadata + enabled-schemes only).

No network, no funds: this is pure product metadata. The rail flags default off,
so the surfacing lists only ``exact`` unless a test flips a flag on config.
"""

from sqlalchemy import select

import app.main as main
from app import agentic, config, delivery
from app.db import SessionLocal
from app.models import EventLog, Product, Store
from fastapi.testclient import TestClient

client = TestClient(main.app)


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


VALID_SUB = {
    "amount_per_period_micro": 5_000_000,
    "period_sec": 2_592_000,
    "max_periods": 12,
    "plan_id": "pro-monthly",
    "plan_tier": 2,
    "plan_name": "Pro Monthly",
}
VALID_METERED = {
    "unit": "token",
    "price_per_unit_micro": 1000,
    "min_deposit_micro": 1_000_000,
}


# --------------------------------------------------------------- auth
def test_pricing_requires_manage_key(make_store):
    make_store(slug="pr1")
    r = client.post("/api/stores/pr1/pricing", json={"pricing_model": "batch"})
    assert r.status_code == 401


def test_pricing_wrong_manage_key(make_store):
    make_store(slug="pr2")
    _give_key("pr2")
    r = client.post(
        "/api/stores/pr2/pricing",
        json={"pricing_model": "batch"},
        headers=_auth("wrong-key"),
    )
    assert r.status_code == 401


def test_pricing_unknown_store():
    r = client.post(
        "/api/stores/ghost/pricing",
        json={"pricing_model": "batch"},
        headers=_auth("x"),
    )
    assert r.status_code == 404


# --------------------------------------------------------------- defaults
def test_default_pricing_model_is_one_time(make_store):
    make_store(slug="pr3")
    assert _product("pr3").pricing_model == "one_time"


# --------------------------------------------------------------- happy paths
def test_declare_batch(make_store):
    make_store(slug="pb")
    key = _give_key("pb")
    r = client.post(
        "/api/stores/pb/pricing",
        json={"pricing_model": "batch"},
        headers=_auth(key),
    )
    assert r.status_code == 200
    assert r.json()["pricing_model"] == "batch"
    p = _product("pb")
    assert p.pricing_model == "batch"
    assert p.pricing_params is None


def test_declare_metered_round_trips(make_store):
    make_store(slug="pm")
    key = _give_key("pm")
    r = client.post(
        "/api/stores/pm/pricing",
        json={"pricing_model": "metered", "params": VALID_METERED},
        headers=_auth(key),
    )
    assert r.status_code == 200
    p = _product("pm")
    assert p.pricing_model == "metered"
    assert p.pricing_params == VALID_METERED


def test_declare_subscription_round_trips(make_store):
    make_store(slug="ps")
    key = _give_key("ps")
    r = client.post(
        "/api/stores/ps/pricing",
        json={"pricing_model": "subscription", "params": VALID_SUB},
        headers=_auth(key),
    )
    assert r.status_code == 200
    p = _product("ps")
    assert p.pricing_model == "subscription"
    assert p.pricing_params == VALID_SUB


def test_declare_back_to_one_time_clears_params(make_store):
    make_store(slug="pt")
    key = _give_key("pt")
    client.post(
        "/api/stores/pt/pricing",
        json={"pricing_model": "metered", "params": VALID_METERED},
        headers=_auth(key),
    )
    r = client.post(
        "/api/stores/pt/pricing",
        json={"pricing_model": "one_time"},
        headers=_auth(key),
    )
    assert r.status_code == 200
    p = _product("pt")
    assert p.pricing_model == "one_time"
    assert p.pricing_params is None


def test_pricing_writes_event_log(make_store):
    make_store(slug="pe")
    key = _give_key("pe")
    client.post(
        "/api/stores/pe/pricing",
        json={"pricing_model": "batch"},
        headers=_auth(key),
    )
    with SessionLocal() as s:
        row = s.scalar(select(EventLog).where(EventLog.event == "pricing.updated"))
        assert row is not None
        assert row.data["pricing_model"] == "batch"


# --------------------------------------------------------------- validation
def test_unknown_pricing_model_422(make_store):
    make_store(slug="pu")
    key = _give_key("pu")
    r = client.post(
        "/api/stores/pu/pricing",
        json={"pricing_model": "flat"},
        headers=_auth(key),
    )
    assert r.status_code == 422


def test_metered_missing_price_per_unit_422(make_store):
    make_store(slug="pv")
    key = _give_key("pv")
    r = client.post(
        "/api/stores/pv/pricing",
        json={
            "pricing_model": "metered",
            "params": {"unit": "tok", "min_deposit_micro": 1},
        },
        headers=_auth(key),
    )
    assert r.status_code == 422


def test_subscription_period_too_short_422(make_store):
    make_store(slug="pw")
    key = _give_key("pw")
    bad = dict(VALID_SUB, period_sec=60)
    r = client.post(
        "/api/stores/pw/pricing",
        json={"pricing_model": "subscription", "params": bad},
        headers=_auth(key),
    )
    assert r.status_code == 422


def test_subscription_bad_tier_422(make_store):
    make_store(slug="px")
    key = _give_key("px")
    bad = dict(VALID_SUB, plan_tier=999)
    r = client.post(
        "/api/stores/px/pricing",
        json={"pricing_model": "subscription", "params": bad},
        headers=_auth(key),
    )
    assert r.status_code == 422


def test_metered_unknown_key_forbidden_422(make_store):
    make_store(slug="py")
    key = _give_key("py")
    bad = dict(VALID_METERED, bogus=1)
    r = client.post(
        "/api/stores/py/pricing",
        json={"pricing_model": "metered", "params": bad},
        headers=_auth(key),
    )
    assert r.status_code == 422


def test_batch_rejects_params_422(make_store):
    make_store(slug="pz")
    key = _give_key("pz")
    r = client.post(
        "/api/stores/pz/pricing",
        json={"pricing_model": "batch", "params": {"unit": "x"}},
        headers=_auth(key),
    )
    assert r.status_code == 422


# --------------------------------------------------------------- surfacing
def test_feed_surfaces_pricing_and_exact_only_by_default(make_store):
    make_store(slug="pf1")
    key = _give_key("pf1")
    client.post(
        "/api/stores/pf1/pricing",
        json={"pricing_model": "batch"},
        headers=_auth(key),
    )
    body = client.get("/s/pf1/feed.json").json()
    prod = body["products"][0]
    assert prod["pricing"]["model"] == "batch"
    # flag off -> aggr_deferred is NOT advertised even for a batch product
    assert prod["x402"]["schemes"] == ["exact"]


def test_feed_advertises_aggr_deferred_only_for_batch_when_flag_on(
    make_store, monkeypatch
):
    make_store(slug="pf2")
    key = _give_key("pf2")
    client.post(
        "/api/stores/pf2/pricing",
        json={"pricing_model": "batch"},
        headers=_auth(key),
    )
    monkeypatch.setattr(config, "AGGR_DEFERRED_ENABLED", True)
    prod = client.get("/s/pf2/feed.json").json()["products"][0]
    assert prod["x402"]["schemes"] == ["exact", "aggr_deferred"]


def test_feed_non_batch_never_advertises_aggr_even_with_flag(make_store, monkeypatch):
    make_store(slug="pf3")  # default one_time
    monkeypatch.setattr(config, "AGGR_DEFERRED_ENABLED", True)
    prod = client.get("/s/pf3/feed.json").json()["products"][0]
    assert prod["x402"]["schemes"] == ["exact"]
    assert prod["pricing"]["model"] == "one_time"


def test_mcp_get_product_surfaces_pricing(make_store):
    make_store(slug="pf4")
    key = _give_key("pf4")
    client.post(
        "/api/stores/pf4/pricing",
        json={"pricing_model": "subscription", "params": VALID_SUB},
        headers=_auth(key),
    )
    pid = _product("pf4").id
    r = client.post(
        "/s/pf4/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "get_product", "arguments": {"product_id": pid}},
        },
    )
    sc = r.json()["result"]["structuredContent"]
    assert sc["pricing"]["model"] == "subscription"
    assert sc["pricing"]["params"] == VALID_SUB
    assert sc["x402"]["schemes"] == ["exact"]


def test_llms_txt_lists_pricing_model(make_store):
    make_store(slug="pf5")
    key = _give_key("pf5")
    client.post(
        "/api/stores/pf5/pricing",
        json={"pricing_model": "metered", "params": VALID_METERED},
        headers=_auth(key),
    )
    txt = client.get("/s/pf5/llms.txt").text
    assert "[metered]" in txt

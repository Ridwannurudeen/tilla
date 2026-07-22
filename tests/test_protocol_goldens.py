"""M16.3 protocol goldens — the LIVE Tilla surfaces validate against the frozen
JSON Schemas pinned by docs/specs/tilla-protocol-v1.md, so the open-protocol spec
can never silently drift from the running code. Each surface in the protocol's
registry table is exercised here against its pinned schema.
"""

import inspect
import pathlib

import httpx
import jsonschema
import pytest
import respx
import yaml
from sqlalchemy import select

import app.main as main
from app import agentic, b2b, config, delivery
from app.db import SessionLocal
from app.models import Store
from fastapi.testclient import TestClient

client = TestClient(main.app)

REPO = pathlib.Path(__file__).resolve().parent.parent
SCHEMAS = REPO / "docs" / "specs" / "schemas"
REGISTRY = "0x" + "1" * 40
OWNER = "0x" + "b" * 40


def _schema(path: pathlib.Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


AGENT_CARD_SCHEMA = _schema(SCHEMAS / "agent-card.yaml")
QUOTE_SCHEMA = _schema(SCHEMAS / "quote.yaml")
MCP_TOOLS_SCHEMA = _schema(SCHEMAS / "mcp-tools-list.yaml")
MCP_PRODUCT_SCHEMA = _schema(SCHEMAS / "mcp-get-product.yaml")
FEED_SCHEMA = _schema(REPO / "docs" / "openapi.feed.yaml")


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


def _set_tiers(slug: str, tiers):
    plain, key_hash = delivery.mint_manage_key()
    with SessionLocal() as s:
        store = s.scalar(select(Store).where(Store.slug == slug))
        store.manage_key_hash = key_hash
        s.commit()
    return client.post(
        f"/api/stores/{slug}/pricing",
        json={"pricing_model": "one_time", "tiers": tiers},
        headers={"Authorization": f"Bearer {plain}"},
    )


def _product_id(slug: str) -> int:
    return client.get(f"/s/{slug}/feed.json").json()["products"][0]["id"]


def _mcp(slug: str, method: str, params: dict | None = None) -> dict:
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return client.post(f"/s/{slug}/mcp", json=body).json()


TIER = {"buyer": "erc8004:6961", "price_micro": 5_000_000}


def test_agent_card_validates_against_pinned_schema():
    body = client.get("/.well-known/agent-card.json").json()
    jsonschema.validate(body, AGENT_CARD_SCHEMA)
    assert body["payment"]["network"] == "eip155:196"
    assert any(r["standard"] == "ERC-8004" for r in body["registrations"])


def _buy_query_fields() -> set[str]:
    """The x402 buy endpoint's REAL accepted request-shaping fields: the buy route's
    optional query params (slug/product_id are path-fixed, payment rides the x402
    header). Read from the live signature so the feed input_schema can't drift from
    what the endpoint actually accepts."""
    sig = inspect.signature(agentic.agent_buy_product)
    skip = {"request", "session", "slug", "product_id"}
    return {n for n, p in sig.parameters.items() if n not in skip and p.default is None}


def test_feed_validates_against_pinned_schema(make_store):
    make_store(slug="pg-feed", price_micro=9_000_000)
    _set_tiers("pg-feed", [TIER])
    body = client.get("/s/pg-feed/feed.json").json()
    jsonschema.validate(body, FEED_SCHEMA)
    prod = body["products"][0]
    assert prod["pricing"]["wholesale"] is True
    assert "tiers" not in prod["pricing"]["params"]  # INV-B: no tier table leaked
    # Phase 1.2: every product carries a typed buy input_schema whose fields are
    # exactly the buy endpoint's real accepted request fields — no more, no less.
    isch = prod["input_schema"]
    assert isch["type"] == "object"
    assert set(isch["properties"]) == _buy_query_fields() == {"ref", "agent_id"}


def test_quote_base_only_validates(make_store):
    make_store(slug="pg-qbase", price_micro=9_000_000)
    _set_tiers("pg-qbase", [TIER])
    body = client.get("/s/pg-qbase/quote?agent_id=6961").json()  # registry unset
    jsonschema.validate(body, QUOTE_SCHEMA)
    assert "tier_price_micro" not in body  # fail-to-base


@respx.mock
def test_quote_with_tier_validates(make_store, registry):
    make_store(slug="pg-qtier", price_micro=9_000_000)
    _set_tiers("pg-qtier", [TIER])
    _mock_owner(OWNER)
    body = client.get("/s/pg-qtier/quote?agent_id=6961").json()
    jsonschema.validate(body, QUOTE_SCHEMA)
    assert body["tier_price_micro"] == 5_000_000
    assert body["owner"] == OWNER


def test_mcp_tools_list_validates(make_store):
    make_store(slug="pg-tools", price_micro=9_000_000)
    result = _mcp("pg-tools", "tools/list")["result"]
    jsonschema.validate(result, MCP_TOOLS_SCHEMA)
    assert {t["name"] for t in result["tools"]} == {
        "list_products",
        "get_product",
        "preview_order",
        "create_checkout",
        "pay",
    }


def test_mcp_get_product_validates(make_store):
    make_store(slug="pg-gp", price_micro=9_000_000)
    _set_tiers("pg-gp", [TIER])
    pid = _product_id("pg-gp")
    result = _mcp(
        "pg-gp",
        "tools/call",
        {"name": "get_product", "arguments": {"product_id": pid}},
    )["result"]
    sc = result["structuredContent"]
    jsonschema.validate(sc, MCP_PRODUCT_SCHEMA)
    assert sc["pricing"]["wholesale"] is True
    assert "quote" not in sc  # no agent_id => no per-buyer quote


@respx.mock
def test_mcp_get_product_with_quote_validates(make_store, registry):
    make_store(slug="pg-gpq", price_micro=9_000_000)
    _set_tiers("pg-gpq", [TIER])
    _mock_owner(OWNER)
    pid = _product_id("pg-gpq")
    result = _mcp(
        "pg-gpq",
        "tools/call",
        {"name": "get_product", "arguments": {"product_id": pid, "agent_id": "6961"}},
    )["result"]
    sc = result["structuredContent"]
    jsonschema.validate(sc, MCP_PRODUCT_SCHEMA)
    assert sc["quote"]["tier_price_micro"] == 5_000_000

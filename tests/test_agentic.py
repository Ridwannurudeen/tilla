"""M7 agent-surface tests: feed.json (validated against the pinned ACP schema),
llms.txt, the A2A agent card, the per-store MCP JSON-RPC server, and the
Tilla-wide discovery API. All app-served, all JSON/text (no HTML context).
"""

import json
import pathlib

import httpx
import jsonschema
import respx
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main
from app import chain, config
from app.db import SessionLocal
from app.models import Order, Product, Store, get_or_create_merchant

client = TestClient(main.app)

_SCHEMA = yaml.safe_load(
    (
        pathlib.Path(__file__).resolve().parent.parent / "docs" / "openapi.feed.yaml"
    ).read_text()
)


def _seed(
    slug="shop",
    pay_to="0x" + "a" * 40,
    price_micro=9_500_000,
    content=None,
    description="a fine shop",
    status="live",
):
    with SessionLocal() as s:
        me = get_or_create_merchant(s, pay_to)
        store = Store(
            slug=slug,
            merchant_id=me.id,
            status=status,
            pay_to=pay_to,
            theme="original.html",
            description=description,
            content=content
            or {
                "store_name": "Shoppe",
                "product_blurb": "Great thing",
                "hero_subcopy": "Buy the thing",
            },
        )
        s.add(store)
        s.flush()
        s.add(
            Product(
                store_id=store.id, name="Thing", price_micro=price_micro, active=True
            )
        )
        s.commit()
        return store.id


def _order(
    store_id,
    status,
    expected_micro=9_500_100,
    channel="web",
    oid=None,
    from_addr=None,
    paid_at=None,
    eval_status="none",
):
    import uuid

    with SessionLocal() as s:
        s.add(
            Order(
                id=oid or uuid.uuid4().hex[:16],
                store_id=store_id,
                pay_to="0x" + "a" * 40,
                amount_micro=9_500_000,
                expected_micro=expected_micro,
                status=status,
                channel=channel,
                from_addr=from_addr,
                paid_at=paid_at,
                eval_status=eval_status,
            )
        )
        s.commit()


# ------------------------------------------------------------ agent card
def test_agent_card_shape():
    r = client.get("/.well-known/agent-card.json")
    assert r.status_code == 200
    assert r.headers["x-content-type-options"] == "nosniff"
    card = r.json()
    assert card["name"] == "Tilla"
    assert card["payment"]["protocol"] == "x402-v2"
    assert card["payment"]["network"] == "eip155:196"
    regs = card["registrations"]
    assert any(x["standard"] == "ERC-8004" and x["agentId"] == 6961 for x in regs)
    assert card["discovery"]["resources"] == "/discovery/resources"


def test_agent_card_skills_carry_input_schema_and_sla():
    card = client.get("/.well-known/agent-card.json").json()
    skills = {s["id"]: s for s in card["skills"]}
    cs = skills["create-store"]
    # the create-store input contract is the real CreateStoreBody schema —
    # description became OPTIONAL with the empty-body-creates-a-sample-store fix,
    # and the card must advertise that truthfully so an unattended agent knows a
    # bare paid POST completes.
    assert "description" in cs["input_schema"]["properties"]
    assert "description" not in cs["input_schema"].get("required", [])
    assert cs["sample_request"]["description"]
    assert isinstance(cs["sla_minutes"], int) and cs["sla_minutes"] > 0
    # buy advertises the optional product_id selector
    assert "product_id" in skills["buy"]["input_schema"]["properties"]


# ------------------------------------------------------------ feed.json
def test_feed_validates_against_pinned_schema():
    _seed(slug="feedshop", price_micro=9_500_000)
    r = client.get("/s/feedshop/feed.json")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers["x-content-type-options"] == "nosniff"
    body = r.json()
    jsonschema.validate(body, _SCHEMA)  # raises on any deviation
    assert body["store"]["slug"] == "feedshop"
    p = body["products"][0]
    assert p["price"] == {"amount": "9.5", "currency": "USDT"}
    assert p["sla_minutes"] == 10  # platform default when no per-product override
    assert p["x402"]["endpoint"] == f"/s/feedshop/buy/{p['id']}"
    assert p["x402"]["asset"] == config.USDT0


def _set_product_sla(slug, minutes):
    with SessionLocal() as s:
        store = s.scalar(select(Store).where(Store.slug == slug))
        product = s.scalar(select(Product).where(Product.store_id == store.id))
        product.sla_minutes = minutes
        s.commit()


def test_sla_default_and_per_product_override():
    _seed(slug="slashop", price_micro=2_000_000)
    # default: every purchasable surface carries the platform ETA
    feed = client.get("/s/slashop/feed.json").json()["products"][0]
    assert feed["sla_minutes"] == 10
    gp = _mcp(
        "slashop",
        "tools/call",
        {"name": "list_products"},
    ).json()["result"]["structuredContent"]["products"][0]
    assert gp["sla_minutes"] == 10
    cc = client.post("/api/checkout/slashop").json()
    assert cc["sla_minutes"] == 10

    # per-product override wins everywhere
    _set_product_sla("slashop", 30)
    assert client.get("/s/slashop/feed.json").json()["products"][0]["sla_minutes"] == 30
    pid = gp["id"]
    detail = _mcp(
        "slashop",
        "tools/call",
        {"name": "get_product", "arguments": {"product_id": pid}},
    ).json()["result"]["structuredContent"]
    assert detail["sla_minutes"] == 30
    assert client.post("/api/checkout/slashop").json()["sla_minutes"] == 30


def _pid(slug):
    with SessionLocal() as s:
        store = s.scalar(select(Store).where(Store.slug == slug))
        return s.scalar(select(Product).where(Product.store_id == store.id)).id


def test_feed_required_funds_equals_the_real_x402_charge():
    # requiredFunds MUST be byte-identical to what the x402 buy endpoint demands:
    # the resolvers the payment middleware calls per request are the single source of
    # truth. Derive both from them and assert the feed matches — never a hardcode.
    from app import agentic

    pay_to = "0x" + "b" * 40
    _seed(slug="rf", pay_to=pay_to, price_micro=7_250_000)
    pid = _pid("rf")
    p = client.get("/s/rf/feed.json").json()["products"][0]
    rf = p["requiredFunds"]
    path = f"/s/rf/buy/{pid}"
    charge = agentic.resolve_price(path)  # what the 402 challenge advertises + settles
    assert rf["amount_micro"] == int(charge.amount) == 7_250_000
    assert rf["amount"] == "7.25"
    assert rf["asset"] == charge.asset == config.USDT0
    assert rf["network"] == "eip155:196"
    # NON-CUSTODIAL: pay_to is the merchant, exactly what resolve_pay_to returns.
    assert rf["pay_to"] == agentic.resolve_pay_to(path, "0x" + "0" * 40) == pay_to


def test_feed_next_actions_name_reachable_actions():
    _seed(slug="na", price_micro=3_000_000)
    pid = _pid("na")
    p = client.get("/s/na/feed.json").json()["products"][0]
    actions = {a["type"]: a for a in p["nextActions"]}
    # the x402 buy action names the exact per-product endpoint the agent POSTs to
    assert actions["x402_buy"]["endpoint"] == f"/s/na/buy/{pid}"
    assert actions["x402_buy"]["method"] == "POST"
    # the MCP action names a reachable JSON-RPC server (initialize returns 200)
    assert actions["mcp"]["endpoint"] == "/s/na/mcp"
    assert (
        _mcp("na", "initialize", {"protocolVersion": "2025-06-18"}).status_code == 200
    )


def test_get_product_offering_envelope_matches_charge():
    from app import agentic

    pay_to = "0x" + "c" * 40
    _seed(slug="gpenv", pay_to=pay_to, price_micro=4_000_000)
    pid = _pid("gpenv")
    sc = _mcp(
        "gpenv", "tools/call", {"name": "get_product", "arguments": {"product_id": pid}}
    ).json()["result"]["structuredContent"]
    rf = sc["requiredFunds"]
    charge = agentic.resolve_price(f"/s/gpenv/buy/{pid}")
    assert rf["amount_micro"] == int(charge.amount) == 4_000_000
    assert rf["pay_to"] == pay_to  # merchant, non-custodial
    assert any(a["endpoint"] == f"/s/gpenv/buy/{pid}" for a in sc["nextActions"])


def test_agent_card_offering_envelope():
    card = client.get("/.well-known/agent-card.json").json()
    skills = {s["id"]: s for s in card["skills"]}
    # create-store carries a fixed requiredFunds equal to the real /create-store 402
    # charge (PAYMENT_AMOUNT = 0.05 USDT); its nextActions POST /create-store.
    from app import agentic
    from app.payment import PAYMENT_AMOUNT

    cs = skills["create-store"]
    assert cs["requiredFunds"]["amount_micro"] == int(PAYMENT_AMOUNT) == 50_000
    assert cs["requiredFunds"]["asset"] == config.USDT0
    assert any(a["endpoint"] == "/create-store" for a in cs["nextActions"])
    # Both human-readable price strings are DERIVED from the same requiredFunds
    # amount, so a fee change cannot desynchronize them from the real 402.
    price = f"{agentic._usdt_str(int(PAYMENT_AMOUNT))} {agentic.CURRENCY}"
    assert cs["requiredFunds"]["amount"] == "0.05"
    assert cs["x402"]["price"] == price == "0.05 USDT"
    assert f"({price})" in cs["nextActions"][0]["description"]
    # the buy skill routes to per-store surfaces for the per-offering requiredFunds
    buy_actions = {a["type"]: a for a in skills["buy"]["nextActions"]}
    assert buy_actions["x402_buy"]["endpoint"] == "/s/{slug}/buy"


def test_discovery_row_next_actions_route_to_store_surfaces():
    _seed(slug="disc-na", price_micro=2_000_000)
    row = next(
        r
        for r in client.get("/discovery/resources").json()["resources"]
        if r["slug"] == "disc-na"
    )
    actions = {a["type"]: a for a in row["nextActions"]}
    assert actions["feed"]["endpoint"] == "/s/disc-na/feed.json"
    assert actions["x402_buy"]["endpoint"] == "/s/disc-na/buy"
    # discovery still never bulk-exports the merchant wallet
    assert "pay_to" not in row


def test_feed_404_for_non_live():
    _seed(slug="pendfeed", status="pending_screening")
    assert client.get("/s/pendfeed/feed.json").status_code == 404
    assert client.get("/s/ghost/feed.json").status_code == 404


def test_feed_hostile_store_name_is_inert_json():
    _seed(
        slug="xss",
        content={
            "store_name": "<script>alert(1)</script>",
            "product_blurb": "x",
            "hero_subcopy": "y",
        },
    )
    r = client.get("/s/xss/feed.json")
    assert r.status_code == 200
    # served as data, never markup: application/json + nosniff, round-trips as a string
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.json()["store"]["name"] == "<script>alert(1)</script>"


# ------------------------------------------------------------ llms.txt
def test_llms_txt_plaintext_with_endpoints():
    _seed(slug="llmshop", price_micro=4_000_000)
    r = client.get("/s/llmshop/llms.txt")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert r.headers["x-content-type-options"] == "nosniff"
    body = r.text
    assert "Thing — 4 USDT" in body
    assert "/s/llmshop/feed.json" in body
    assert "/s/llmshop/mcp" in body
    assert "/s/llmshop/buy" in body


def test_llms_404_for_non_live():
    _seed(slug="pendllms", status="pending_screening")
    assert client.get("/s/ghost/llms.txt").status_code == 404
    assert client.get("/s/pendllms/llms.txt").status_code == 404


# ------------------------------------------------------------ MCP JSON-RPC
def _mcp(slug, method, params=None, req_id=1):
    body = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post(f"/s/{slug}/mcp", json=body)


def test_mcp_initialize_echoes_protocol_and_names_server():
    _seed(slug="mcp1")
    r = _mcp("mcp1", "initialize", {"protocolVersion": "2025-06-18"})
    assert r.status_code == 200
    res = r.json()["result"]
    assert res["protocolVersion"] == "2025-06-18"
    assert res["serverInfo"]["name"] == "tilla-mcp1"
    assert "tools" in res["capabilities"]


def test_mcp_notifications_initialized_returns_202():
    _seed(slug="mcp2")
    r = client.post(
        "/s/mcp2/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    assert r.status_code == 202
    assert r.content == b""


def test_mcp_ping_and_tools_list():
    _seed(slug="mcp3")
    assert _mcp("mcp3", "ping").json()["result"] == {}
    tools = _mcp("mcp3", "tools/list").json()["result"]["tools"]
    assert {t["name"] for t in tools} == {
        "list_products",
        "get_product",
        "preview_order",
        "create_checkout",
        "pay",
    }


def test_mcp_tools_call_list_and_get_product():
    sid = _seed(slug="mcp4", price_micro=3_000_000)
    with SessionLocal() as s:
        pid = s.scalar(select(Product.id).where(Product.store_id == sid))
    lp = _mcp("mcp4", "tools/call", {"name": "list_products"}).json()["result"]
    assert lp["structuredContent"]["products"][0]["currency"] == "USDT"
    assert lp["content"][0]["type"] == "text"
    gp = _mcp(
        "mcp4", "tools/call", {"name": "get_product", "arguments": {"product_id": pid}}
    ).json()["result"]
    sc = gp["structuredContent"]
    assert sc["x402"]["endpoint"] == f"/s/mcp4/buy/{pid}"
    assert sc["network"] == "eip155:196"


def test_mcp_unknown_method_and_bad_params():
    _seed(slug="mcp5")
    assert _mcp("mcp5", "does/not/exist").json()["error"]["code"] == -32601
    bad = _mcp("mcp5", "tools/call", {"name": "get_product", "arguments": {}})
    assert bad.json()["error"]["code"] == -32602


def test_mcp_malformed_json_is_parse_error():
    _seed(slug="mcp6")
    r = client.post(
        "/s/mcp6/mcp",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert r.json()["error"]["code"] == -32700


def test_mcp_get_is_405():
    _seed(slug="mcp7")
    assert client.get("/s/mcp7/mcp").status_code == 405


def test_mcp_404_for_dead_store():
    r = _mcp("ghost", "tools/list")
    assert r.status_code == 404


def test_mcp_rate_limited_after_30():
    _seed(slug="mcprl")
    codes = [_mcp("mcprl", "ping", req_id=i).status_code for i in range(31)]
    assert codes[:30] == [200] * 30
    assert codes[30] == 429


# ---- MCP create_checkout -> pay (respx-mocked chain fast path) ----
def _log(to_addr, value, tx_hash, block, from_addr="0x" + "7" * 40):
    return {
        "address": config.USDT0,
        "topics": [
            config.TRANSFER_TOPIC,
            chain.pad_address(from_addr),
            chain.pad_address(to_addr),
        ],
        "data": hex(value),
        "transactionHash": tx_hash,
        "logIndex": "0x0",
        "blockNumber": hex(block),
    }


class _Rpc:
    def __init__(self, head, receipts):
        self.head = head
        self.receipts = receipts

    def handler(self, request):
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        if method == "eth_blockNumber":
            result = hex(self.head)
        elif method == "eth_getTransactionReceipt":
            result = self.receipts.get(params[0].lower())
        else:
            result = None
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )


@respx.mock
def test_mcp_create_checkout_then_pay():
    pay_to = "0x" + "a" * 40
    _seed(slug="mcppay", pay_to=pay_to, price_micro=2_000_000)
    cc = _mcp("mcppay", "tools/call", {"name": "create_checkout"}).json()["result"]
    checkout_id = cc["structuredContent"]["checkout_id"]
    amount = cc["structuredContent"]["amount_micro"]
    tx = "0x" + "1" * 64
    receipt = {
        "status": "0x1",
        "blockNumber": hex(100),
        "logs": [_log(pay_to, amount, tx, 100)],
    }
    respx.post(config.RPC_URL).mock(side_effect=_Rpc(200, {tx: receipt}).handler)
    r = _mcp(
        "mcppay",
        "tools/call",
        {"name": "pay", "arguments": {"checkout_id": checkout_id, "tx_hash": tx}},
    )
    res = r.json()["result"]["structuredContent"]
    assert res["status"] == "paid"
    with SessionLocal() as s:
        assert s.get(Order, checkout_id).status in ("delivered", "paid")


def test_mcp_create_checkout_optional_product_id(make_store):
    # M10: a store with multiple products can target one via product_id; omitting it
    # keeps the primary-product behaviour byte-identical.
    sid = make_store(slug="mcp-multi", price_micro=9_000_000)
    with SessionLocal() as s:
        s.add(Product(store_id=sid, name="Second", price_micro=2_000_000, active=True))
        s.commit()
        pid2 = s.scalar(
            select(Product.id).where(
                Product.store_id == sid, Product.price_micro == 2_000_000
            )
        )
    targeted = _mcp(
        "mcp-multi",
        "tools/call",
        {"name": "create_checkout", "arguments": {"product_id": pid2}},
    ).json()["result"]["structuredContent"]
    assert 2_000_001 <= targeted["amount_micro"] <= 2_004_999
    default = _mcp("mcp-multi", "tools/call", {"name": "create_checkout"}).json()[
        "result"
    ]["structuredContent"]
    assert 9_000_001 <= default["amount_micro"] <= 9_004_999


def test_mcp_create_checkout_unknown_product_id_is_tool_error(make_store):
    make_store(slug="mcp-nope", price_micro=1_000_000)
    r = _mcp(
        "mcp-nope",
        "tools/call",
        {"name": "create_checkout", "arguments": {"product_id": 999999}},
    ).json()["result"]
    assert r["isError"] is True
    assert "not found" in r["structuredContent"]["error"]


# ---- Phase 4 butler flow: read-only preview + confirmation summary ----
def test_mcp_preview_order_summarizes_without_charging():
    # The read-only preview carries price/total/ETA/store/rail and a human line, and
    # creates NO order — money only ever moves on the explicit pay step.
    sid = _seed(slug="mcp-preview", price_micro=4_000_000)
    res = _mcp("mcp-preview", "tools/call", {"name": "preview_order"}).json()["result"]
    assert res.get("isError") is not True
    sc = res["structuredContent"]
    s = sc["summary"]
    assert s["unit_price"] == "4" and s["unit_price_micro"] == 4_000_000
    assert s["quantity"] == 1
    assert s["total"] == "4" and s["total_micro"] == 4_000_000
    assert s["sla_minutes"] == 10
    assert s["store"]["name"] == "Shoppe" and s["store"]["slug"] == "mcp-preview"
    assert s["product"]["name"] == "Thing"
    assert s["currency"] == "USDT" and s["network"] == "eip155:196"
    assert s["settlement"] == "non_custodial" and s["pay_to"] == "0x" + "a" * 40
    assert "Thing" in s["line"] and "USDT" in s["line"]
    assert "nothing is charged" in sc["next_step"]
    # read-only: no order reserved by a preview
    with SessionLocal() as db:
        assert db.scalars(select(Order).where(Order.store_id == sid)).all() == []


def test_mcp_preview_order_unknown_product_id_is_tool_error(make_store):
    make_store(slug="mcp-preview-nope", price_micro=1_000_000)
    r = _mcp(
        "mcp-preview-nope",
        "tools/call",
        {"name": "preview_order", "arguments": {"product_id": 999999}},
    ).json()["result"]
    assert r["isError"] is True
    assert "not found" in r["structuredContent"]["error"]


def test_mcp_create_checkout_returns_confirmation_summary():
    # create_checkout enriches its result with the same confirm-before-pay summary
    # and a next_step making the two-step (reserve -> pay) contract explicit.
    _seed(slug="mcp-ccsum", pay_to="0x" + "c" * 40, price_micro=7_000_000)
    sc = _mcp("mcp-ccsum", "tools/call", {"name": "create_checkout"}).json()["result"][
        "structuredContent"
    ]
    # existing top-level contract is unchanged (exact amount includes the offset)
    assert 7_000_001 <= sc["amount_micro"] <= 7_004_999
    assert sc["pay_to"] == "0x" + "c" * 40
    s = sc["summary"]
    assert s["unit_price"] == "7" and s["total"] == "7"
    assert s["total_micro"] == 7_000_000  # goods price, not the payment offset
    assert s["sla_minutes"] == 10
    assert s["store"]["slug"] == "mcp-ccsum"
    assert "amount_micro" in sc["next_step"] and "UNPAID" in sc["next_step"]


# ------------------------------------------------------------ discovery
def test_discovery_resources_live_only_and_caps():
    _seed(slug="live-a")
    _seed(slug="live-b")
    _seed(slug="pending-c", status="pending_screening")
    r = client.get("/discovery/resources?limit=100&offset=-5")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "tilla"
    assert body["limit"] == 50 and body["offset"] == 0
    assert body["total"] == 2  # pending excluded
    slugs = {row["slug"] for row in body["resources"]}
    assert slugs == {"live-a", "live-b"}
    row = next(r for r in body["resources"] if r["slug"] == "live-a")
    assert "pay_to" not in row  # merchant wallet never bulk-exported
    assert row["buy"] == "/s/live-a/buy"


def test_discovery_sold_count_counts_only_delivered():
    sid = _seed(slug="soldshop")
    _order(sid, "delivered", channel="web")
    _order(sid, "delivered", channel="agent")
    _order(sid, "canceled", channel="web")
    _order(sid, "pending", channel="web")
    r = client.get("/discovery/resources")
    row = next(x for x in r.json()["resources"] if x["slug"] == "soldshop")
    assert row["sold_count"] == 2


def test_discovery_reputation_fields():
    from datetime import datetime

    sid = _seed(slug="repshop")
    _order(sid, "delivered", from_addr="0x" + "1" * 40, paid_at=datetime(2026, 1, 1))
    _order(sid, "delivered", from_addr="0x" + "2" * 40, paid_at=datetime(2026, 3, 1))
    # repeat buyer 0x1 — must not double-count
    _order(sid, "delivered", from_addr="0x" + "1" * 40, paid_at=datetime(2026, 2, 1))
    _order(sid, "refunded", from_addr="0x" + "3" * 40)
    _order(sid, "canceled", from_addr="0x" + "4" * 40)  # abandoned — excluded
    _seed(slug="emptyshop")

    rows = client.get("/discovery/resources").json()["resources"]
    rep = next(x for x in rows if x["slug"] == "repshop")
    assert rep["sold_count"] == 3
    assert rep["unique_buyer_count"] == 2  # 0x1 counted once
    # 3 delivered / (3 delivered + 1 refunded) = 0.75; canceled/pending excluded
    assert rep["success_rate"] == 0.75
    assert rep["last_sale_at"].startswith("2026-03-01")  # most recent delivery

    none = next(x for x in rows if x["slug"] == "emptyshop")
    assert none["sold_count"] == 0
    assert none["success_rate"] is None
    assert none["unique_buyer_count"] == 0
    assert none["last_sale_at"] is None


def test_discovery_sort_by_success_rate():
    # A: perfect but low volume; B: high volume, lower clean-delivery rate.
    a = _seed(slug="cleanshop")
    _order(a, "delivered", from_addr="0x" + "1" * 40)
    b = _seed(slug="busyshop")
    for i in range(3):
        _order(b, "delivered", from_addr="0x" + str(i) * 40)
        _order(b, "refunded", from_addr="0x" + str(i) * 40)

    default = [
        x["slug"] for x in client.get("/discovery/resources").json()["resources"]
    ]
    assert default.index("busyshop") < default.index("cleanshop")  # sold desc: B first

    body = client.get("/discovery/resources?sort=success").json()
    assert body["sort"] == "success"
    order = [x["slug"] for x in body["resources"]]
    # rate desc: A (1.0) ahead of B (0.5), despite B's higher volume
    assert order.index("cleanshop") < order.index("busyshop")


def test_discovery_sort_unknown_falls_back_to_sold():
    body = client.get("/discovery/resources?sort=bogus").json()
    assert body["sort"] == "sold"
    assert set(body["sorts"]) == {"sold", "success", "buyers", "recent", "new"}


def test_discovery_trust_tier_graduation_and_demote():
    def deliver(sid, n):
        for i in range(n):
            _order(sid, "delivered", from_addr="0x" + str(i) * 40, oid=f"{sid}d{i}")

    _seed(slug="tier-new")  # 0 sales
    emerging = _seed(slug="tier-emerging")
    deliver(emerging, 1)
    established = _seed(slug="tier-established")
    deliver(established, 3)
    trusted = _seed(slug="tier-trusted")
    deliver(trusted, 8)
    # high volume but refund-heavy -> demoted to 'watch' despite the sales
    watch = _seed(slug="tier-watch")
    deliver(watch, 2)
    for i in range(3):
        _order(watch, "refunded", from_addr="0x" + str(i) * 40, oid=f"{watch}r{i}")

    rows = {
        r["slug"]: r for r in client.get("/discovery/resources").json()["resources"]
    }
    assert rows["tier-new"]["trust_tier"] == "new"
    assert rows["tier-emerging"]["trust_tier"] == "emerging"
    assert rows["tier-established"]["trust_tier"] == "established"
    assert rows["tier-trusted"]["trust_tier"] == "trusted"
    assert rows["tier-watch"]["trust_tier"] == "watch"  # auto-demoted (refunds)


def test_discovery_eval_window_excludes_pending_and_counts_disputes():
    from datetime import timedelta

    from app import checkout
    from app.agentic import EVAL_WINDOW_DAYS

    recent = checkout._now()
    old = checkout._now() - timedelta(days=EVAL_WINDOW_DAYS + 1)
    sid = _seed(slug="evalshop")
    # buyer-confirmed -> good
    _order(
        sid, "delivered", from_addr="0x" + "1" * 40, eval_status="confirmed", oid="ev1"
    )
    # pending, still inside the window -> not yet counted (auto-confirm pending)
    _order(
        sid,
        "delivered",
        from_addr="0x" + "2" * 40,
        paid_at=recent,
        eval_status="pending",
        oid="ev2",
    )
    # pending but past the window -> auto-confirmed at read -> good
    _order(
        sid,
        "delivered",
        from_addr="0x" + "3" * 40,
        paid_at=old,
        eval_status="pending",
        oid="ev3",
    )
    # buyer-rejected -> a failure the rate must reflect
    _order(
        sid, "delivered", from_addr="0x" + "4" * 40, eval_status="rejected", oid="ev4"
    )

    row = next(
        r
        for r in client.get("/discovery/resources").json()["resources"]
        if r["slug"] == "evalshop"
    )
    assert row["sold_count"] == 4  # raw delivered count is unchanged
    assert row["pending_eval_count"] == 1  # only the in-window pending
    assert row["disputed_count"] == 1
    # good = confirmed + window-elapsed = 2; settled = good + rejected = 3 -> 0.6667
    assert row["success_rate"] == round(2 / 3, 4)


def test_library_evaluate_confirm_then_locks():
    from app import delivery

    buyer = "0x" + "b" * 40
    sid = _seed(slug="evalep")
    _order(sid, "delivered", from_addr=buyer, eval_status="pending", oid="epord")
    auth = {"Authorization": f"Bearer {delivery.mint_session_token(buyer)}"}

    r = client.post(
        "/api/library/evaluate",
        json={"order_id": "epord", "verdict": "confirm"},
        headers=auth,
    )
    assert r.status_code == 200 and r.json()["eval_status"] == "confirmed"
    # a settled evaluation is not re-opened
    r2 = client.post(
        "/api/library/evaluate",
        json={"order_id": "epord", "verdict": "reject"},
        headers=auth,
    )
    assert r2.status_code == 409


def test_library_evaluate_reject_feeds_success_rate():
    from app import delivery

    buyer = "0x" + "b" * 40
    sid = _seed(slug="evalrej")
    _order(sid, "delivered", from_addr=buyer, eval_status="pending", oid="rejord")
    # a fresh pending delivery is not yet counted -> rate is None
    row0 = next(
        r
        for r in client.get("/discovery/resources").json()["resources"]
        if r["slug"] == "evalrej"
    )
    assert row0["success_rate"] is None and row0["pending_eval_count"] == 1

    auth = {"Authorization": f"Bearer {delivery.mint_session_token(buyer)}"}
    client.post(
        "/api/library/evaluate",
        json={"order_id": "rejord", "verdict": "reject"},
        headers=auth,
    )
    row1 = next(
        r
        for r in client.get("/discovery/resources").json()["resources"]
        if r["slug"] == "evalrej"
    )
    # now settled as a dispute: good 0 / settled 1 -> 0.0, and no longer pending
    assert row1["disputed_count"] == 1 and row1["pending_eval_count"] == 0
    assert row1["success_rate"] == 0.0


def test_library_evaluate_rejects_foreign_and_unauthed():
    from app import delivery

    buyer = "0x" + "b" * 40
    sid = _seed(slug="evalep2")
    _order(sid, "delivered", from_addr=buyer, eval_status="pending", oid="epord2")
    # a different wallet cannot evaluate someone else's order -> opaque 404
    foreign = {
        "Authorization": f"Bearer {delivery.mint_session_token('0x' + 'c' * 40)}"
    }
    r = client.post(
        "/api/library/evaluate",
        json={"order_id": "epord2", "verdict": "reject"},
        headers=foreign,
    )
    assert r.status_code == 404
    # no session at all -> 401
    r2 = client.post(
        "/api/library/evaluate", json={"order_id": "epord2", "verdict": "confirm"}
    )
    assert r2.status_code == 401


# ------------------------------------------------------------ hidden/sandbox (1.7)
def _set_visibility(slug, vis):
    with SessionLocal() as s:
        store = s.scalar(select(Store).where(Store.slug == slug))
        store.visibility = vis
        s.commit()


def test_hidden_store_excluded_from_bulk_but_reachable_directly():
    _seed(slug="pubshop")
    _seed(slug="hidshop")
    _set_visibility("hidshop", "hidden")

    # bulk surfaces all skip the hidden store...
    slugs = {r["slug"] for r in client.get("/discovery/resources").json()["resources"]}
    assert "pubshop" in slugs and "hidshop" not in slugs
    assert client.get("/discovery/search?q=hidshop").json()["resources"] == []
    browse = _root_call("browse_stores")["result"]["structuredContent"]["resources"]
    assert "hidshop" not in {r["slug"] for r in browse}
    assert "/s/hidshop/" not in client.get("/sitemap.xml").text
    agg = client.get("/feeds/openai.json").json()["products"]
    assert all(p["store"] != "hidshop" for p in agg)

    # ...but it stays fully reachable by direct link (owner + agent preview)
    assert client.get("/s/hidshop/feed.json").status_code == 200
    assert _mcp("hidshop", "tools/list").status_code == 200


def test_hidden_store_graduates_to_public_on_first_sale():
    from app import checkout

    sid = _seed(slug="gradshop")
    _set_visibility("gradshop", "hidden")
    with SessionLocal() as s:
        order = Order(
            id="gradord",
            store_id=sid,
            pay_to="0x" + "a" * 40,
            amount_micro=9_500_000,
            expected_micro=9_500_100,
            status="confirmed",
        )
        s.add(order)
        s.commit()
        checkout.deliver(s, order)
        s.commit()
    # the first delivered sale clears the threshold -> now shown in discovery
    slugs = {r["slug"] for r in client.get("/discovery/resources").json()["resources"]}
    assert "gradshop" in slugs


def test_discovery_search_escapes_like_and_bounds_query():
    _seed(slug="alpha", description="widgets and gadgets")
    # length bounds
    assert client.get("/discovery/search?q=a").status_code == 422
    assert client.get("/discovery/search?q=" + "x" * 101).status_code == 422
    # plain hit on slug
    r = client.get("/discovery/search?q=alph")
    assert r.status_code == 200
    assert {x["slug"] for x in r.json()["resources"]} == {"alpha"}
    # a literal '%' is escaped, so it matches nothing (not a wildcard)
    r2 = client.get("/discovery/search?q=a%25")  # 'a%'
    assert r2.status_code == 200
    assert r2.json()["resources"] == []


# ------------------------------------------------------------ root MCP (concierge)
def _root_mcp(method, params=None, req_id=1):
    body = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body)


def _root_call(name, arguments=None, req_id=1):
    params = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return _root_mcp("tools/call", params, req_id=req_id).json()


def test_root_mcp_initialize_and_tools_list():
    r = _root_mcp("initialize", {"protocolVersion": "2025-06-18"})
    assert r.status_code == 200
    res = r.json()["result"]
    assert res["protocolVersion"] == "2025-06-18"
    assert res["serverInfo"]["name"] == "tilla"  # Tilla-wide, not per-store
    tools = _root_mcp("tools/list").json()["result"]["tools"]
    assert {t["name"] for t in tools} == {
        "browse_stores",
        "search_stores",
        "list_products",
    }


def test_root_mcp_browse_stores_ranks_and_carries_endpoints():
    a = _seed(slug="rootclean")
    _order(a, "delivered", from_addr="0x" + "1" * 40)
    b = _seed(slug="rootbusy")
    for i in range(3):
        _order(b, "delivered", from_addr="0x" + str(i) * 40)
        _order(b, "refunded", from_addr="0x" + str(i) * 40)

    default = _root_call("browse_stores")["result"]["structuredContent"]
    slugs = [x["slug"] for x in default["resources"]]
    assert slugs.index("rootbusy") < slugs.index("rootclean")  # sold desc
    row = next(x for x in default["resources"] if x["slug"] == "rootclean")
    assert row["mcp"] == "/s/rootclean/mcp" and row["buy"] == "/s/rootclean/buy"
    assert "pay_to" not in row

    ranked = _root_call("browse_stores", {"sort": "success"})["result"]
    body = ranked["structuredContent"]
    assert body["sort"] == "success"
    order = [x["slug"] for x in body["resources"]]
    assert order.index("rootclean") < order.index("rootbusy")  # rate desc


def test_root_mcp_search_and_list_products():
    sid = _seed(slug="rootsearch", description="rare vinyl records")
    with SessionLocal() as s:
        pid = s.scalar(select(Product.id).where(Product.store_id == sid))
    found = _root_call("search_stores", {"query": "vinyl"})["result"]
    assert {x["slug"] for x in found["structuredContent"]["resources"]} == {
        "rootsearch"
    }
    lp = _root_call("list_products", {"slug": "rootsearch"})["result"]
    assert lp["structuredContent"]["products"][0]["id"] == pid


def test_root_mcp_list_products_dead_store_is_tool_error():
    _seed(slug="rootpending", status="pending_screening")
    for slug in ("ghosttown", "rootpending"):
        res = _root_call("list_products", {"slug": slug})
        assert res["result"]["isError"] is True


def test_root_mcp_unknown_tool_and_bad_args():
    assert _root_call("does_not_exist")["error"]["code"] == -32602
    short = _root_call("search_stores", {"query": "a"})
    assert short["result"]["isError"] is True  # min length enforced


def test_root_mcp_get_is_405():
    assert client.get("/mcp").status_code == 405


def test_unpaid_schema_body_publishes_the_request_schema():
    # Regression: the unpaid 402 body was an empty {} on every paid route, so a
    # buyer following the plain x402 flow (POST -> 402 -> pay -> replay) never
    # fetched the agent card and had no machine-readable way to learn the
    # parameters before paying.
    from app.agentic import unpaid_schema_body
    from app.main import CreateStoreBody

    hook = unpaid_schema_body(
        CreateStoreBody.model_json_schema(),
        "POST {description, theme}",
        {"description": "coffee", "theme": "original"},
    )
    out = hook(object())
    assert out.content_type == "application/json"
    assert out.body["error"] == "payment_required"
    assert out.body["summary"] == "POST {description, theme}"
    assert out.body["sample_request"] == {"description": "coffee", "theme": "original"}
    # Derived from the model the handler validates against, so it cannot drift.
    assert "description" in out.body["input_schema"]["properties"]
    assert "theme" in out.body["input_schema"]["properties"]


def test_unpaid_schema_body_omits_sample_when_not_given():
    from app.agentic import unpaid_schema_body
    from app.main import AddProductBody

    body = unpaid_schema_body(
        AddProductBody.model_json_schema(), "Owner-only: manage_key required."
    )(object()).body
    assert "sample_request" not in body
    assert "manage_key" in body["summary"]
    assert set(body["input_schema"]["required"]) == {"slug", "name", "price_usdt"}


# ------------------------------- the input contract inside the x402 challenge
# A buyer (Rouma Desk) reported that our 402 BODY publishes the full input_schema
# but the base64 challenge does not — so an agent driving a stock x402 client,
# which reads the header and never the body, still had to probe for parameters.
def test_challenge_input_extension_names_the_fields_from_the_model():
    from app.agentic import challenge_input_extension
    from app.main import AddProductBody, CreateStoreBody

    create = challenge_input_extension(CreateStoreBody.model_json_schema())[
        "tilla.input"
    ]
    # create-store takes no required field at all — an empty body makes a sample
    # store — and saying so in the challenge is the whole point for a cold caller.
    assert create["required"] == []
    assert set(create["optional"]) == {
        "description",
        "receive_address",
        "theme",
        "delivery",
        "deliverable",
        # Who to tell when something happens to this store. Optional like the rest:
        # a create must never fail over a notification preference.
        "notify_agent_id",
    }
    assert create["full_schema"] == "input_schema in this response body"

    # Derived from the model the handler validates against, so it cannot drift.
    add = challenge_input_extension(AddProductBody.model_json_schema())["tilla.input"]
    assert set(add["required"]) == {"slug", "name", "price_usdt"}
    assert "price_usdt" not in add["optional"]


def test_challenge_input_extension_leaves_the_signing_struct_untouched():
    """The contract rides ``extensions``, never ``accepts[].extra``.

    ``extra`` is what the client reads to build the EIP-712 domain it signs
    against; documentation put there is how a payment becomes unsignable on a
    stricter client than the one we run. Round-trips a real challenge through the
    SDK codec and pins that every payment-bearing field survives byte-identical."""
    from x402.http.utils import (
        decode_payment_required_header,
        encode_payment_required_header,
    )
    from x402.schemas import PaymentRequired, PaymentRequirements
    from x402.schemas.payments import ResourceInfo

    from app.agentic import challenge_input_extension
    from app.main import CreateStoreBody

    pr = PaymentRequired(
        error="Payment required",
        resource=ResourceInfo(
            url="https://tilla.gudman.xyz/create-store", mime_type="application/json"
        ),
        accepts=[
            PaymentRequirements(
                scheme="exact",
                network="eip155:196",
                asset="0x779ded0c9e1022225f8e0630b35a9b54be713736",
                amount="50000",
                pay_to="0xf4c9fa07f3bb852547fdc4df7c1d9fd9991cfa51",
                max_timeout_seconds=300,
                extra={"name": "USD₮0", "version": "1"},
            )
        ],
    )
    before = encode_payment_required_header(pr)
    assert decode_payment_required_header(before).extensions is None

    pr.extensions = challenge_input_extension(CreateStoreBody.model_json_schema())
    after = decode_payment_required_header(encode_payment_required_header(pr))

    assert after.accepts[0].extra == {"name": "USD₮0", "version": "1"}
    assert (
        after.accepts[0].model_dump()
        == decode_payment_required_header(before).accepts[0].model_dump()
    )
    assert after.extensions["tilla.input"]["required"] == []


def test_challenge_stays_far_below_the_proxy_header_buffer():
    """A PAYMENT-REQUIRED header that outgrows nginx's buffer (4k by default, and
    we set no override) fails as a 502 on EVERY 402 rather than degrading — the
    whole paid surface, not one field. This is why the challenge carries field
    names and not the schema inline: the full CreateStoreBody schema is ~1.3kB of
    JSON on its own. Guards the margin, not the exact size."""
    from x402.http.utils import encode_payment_required_header
    from x402.schemas import PaymentRequired, PaymentRequirements

    from app.agentic import challenge_input_extension
    from app.main import CreateStoreBody

    schema = CreateStoreBody.model_json_schema()
    pr = PaymentRequired(
        error="Payment required",
        accepts=[
            PaymentRequirements(
                scheme="exact",
                network="eip155:196",
                asset="0x779ded0c9e1022225f8e0630b35a9b54be713736",
                amount="50000",
                pay_to="0xf4c9fa07f3bb852547fdc4df7c1d9fd9991cfa51",
                max_timeout_seconds=300,
                extra={"name": "USD₮0", "version": "1"},
            )
        ],
        extensions=challenge_input_extension(schema),
    )
    assert len(encode_payment_required_header(pr)) < 2048
    # The reason the schema is not inlined, pinned so a "just put it all in"
    # refactor has to argue with a number.
    assert len(json.dumps(schema)) > 1000


def test_every_schema_publishing_route_also_publishes_its_challenge_contract():
    """Each ``RouteConfig`` that publishes ``unpaid_schema_body`` must also pass
    ``extensions``. The paid routes are built under ``if os.getenv("OKX_API_KEY")``
    and conftest pops that key, so they do not exist at test time — read the source
    instead, so a fifth paid route cannot be added with the gap this test closed."""
    import ast

    tree = ast.parse(pathlib.Path(main.__file__).read_text(encoding="utf-8"))
    routes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "RouteConfig"
    ]
    publishing = [
        r for r in routes if any(k.arg == "unpaid_response_body" for k in r.keywords)
    ]
    assert len(publishing) == 4, "expected create-store, upgrade, verify, add-product"
    for route in publishing:
        assert any(k.arg == "extensions" for k in route.keywords)


def test_discovery_trust_tier_requires_independent_buyers():
    # Regression: `invoice-flow` was published as 'established' on four delivered
    # orders that all came from Tilla's own wallet. Volume from a single buyer is
    # not demand, and a tier an agent ranks on must not be reachable by a merchant
    # buying from themselves.
    def deliver_one_buyer(sid, n, addr):
        for i in range(n):
            _order(sid, "delivered", from_addr=addr, oid=f"{sid}s{i}")

    solo = _seed(slug="tier-solo")
    deliver_one_buyer(solo, 8, "0x" + "7" * 40)  # volume + perfect rate, ONE buyer
    two = _seed(slug="tier-two")
    for i in range(3):
        _order(two, "delivered", from_addr="0x" + str(i % 2) * 40, oid=f"{two}d{i}")

    rows = {
        r["slug"]: r for r in client.get("/discovery/resources").json()["resources"]
    }
    # eight clean sales, but one wallet -> capped below 'established'
    assert rows["tier-solo"]["sold_count"] == 8
    assert rows["tier-solo"]["unique_buyer_count"] == 1
    assert rows["tier-solo"]["trust_tier"] == "emerging"
    # three sales across two distinct buyers -> graduates
    assert rows["tier-two"]["unique_buyer_count"] == 2
    assert rows["tier-two"]["trust_tier"] == "established"


def test_trust_tier_thresholds_directly():
    from app.agentic import _trust_tier

    assert _trust_tier(0, None, 0) == "new"
    assert _trust_tier(9, 0.4, 9) == "watch"  # refund-heavy demotes at any volume
    assert _trust_tier(8, 1.0, 1) == "emerging"  # volume, no independence
    assert _trust_tier(8, 1.0, 2) == "established"  # not yet 3 buyers
    assert _trust_tier(8, 1.0, 3) == "trusted"
    assert _trust_tier(3, 0.95, 2) == "established"
    assert _trust_tier(3, 0.95, 1) == "emerging"

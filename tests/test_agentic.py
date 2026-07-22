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
    # the create-store input contract is the real CreateStoreBody schema
    assert "description" in cs["input_schema"]["properties"]
    assert "description" in cs["input_schema"]["required"]
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

"""M10 marketplace citizenship tests (no network, no funds, no on-chain).

Covers the two new x402 platform endpoints (upgrade-store / add-product) — auth is
the manage key, screening is fail-closed, every failure returns >=400 BEFORE settle
— the GET /s/:slug/buy 405 refusal, the read-only dashboard marketplace panel + its
IDOR gate, and the mark_listed command. Warden + chain are mocked; TILLA_WARDEN_PAID
is off throughout so no paid path is ever constructed.
"""

import json
import secrets
import shlex

import httpx
import pytest
import respx
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from x402.http.utils import encode_payment_response_header
from x402.schemas import SettleResponse

import app.main as main
from app import agentic, checkout, delivery, engine
from app.config import WARDEN_SCREEN_URL
from app.db import SessionLocal
from app.mark_listed import describe, mark_listed
from app.models import EventLog, Order, Product, Review, ScreeningReceipt, Store

client = TestClient(main.app)

PAYER = "0x" + "3" * 40
NONCE = "0x" + "5" * 64

CONTENT = {
    "store_name": "Upgraded Co",
    "tagline": "fresh look",
    "hero_headline": "New and improved",
    "hero_subcopy": "A crisp new storefront.",
    "product_name": "Widget Plus",
    "product_blurb": "Now better.",
    "cta_text": "Buy now",
    "price_usdt": 9,
    "emoji": "🛍️",
    "palette": {},
    "theme": "original",
}


def _auth(key: str) -> dict:
    return {"Authorization": "Bearer " + key}


def _store_with_key(make_store, slug="up1", **kw):
    """A live store with a known manage key (and a baseline content blob)."""
    sid = make_store(slug=slug, **kw)
    key, key_hash = delivery.mint_manage_key()
    with SessionLocal() as s:
        st = s.get(Store, sid)
        st.manage_key_hash = key_hash
        st.content = {"store_name": "Old Name", "product_name": "Old Product"}
        s.commit()
    return sid, key


def _mock_allow():
    return respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(
            200, json={"verdict": "ALLOW", "risk_level": "none"}
        )
    )


# ======================================================= upgrade-store
@respx.mock
def test_upgrade_store_happy_path(make_store, monkeypatch):
    monkeypatch.setenv("TILLA_LLM_KEY", "x")
    monkeypatch.setattr(engine, "generate", lambda desc: dict(CONTENT))
    _mock_allow()
    sid, key = _store_with_key(make_store, slug="up-happy")

    r = client.post(
        "/upgrade-store",
        json={"slug": "up-happy", "description": "new pitch", "theme": "bold"},
        headers=_auth(key),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["slug"] == "up-happy"
    assert body["status"] == "upgraded"
    assert body["theme"] == "bold.html"

    with SessionLocal() as s:
        st = s.get(Store, sid)
        assert st.content["store_name"] == "Upgraded Co"
        assert st.description == "new pitch"
        assert st.theme == "bold.html"
        rec = s.scalar(select(ScreeningReceipt).where(ScreeningReceipt.store_id == sid))
        assert rec is not None
        assert rec.mode == "demo"
        assert rec.verdict == "ALLOW"
        assert rec.tx_hash is None
        assert rec.amount_micro is None
        events = [
            e.event for e in s.scalars(select(EventLog).where(EventLog.store_id == sid))
        ]
        assert "store.upgraded" in events
    # index.html was re-rendered with the new copy
    index = (engine.STORES_DIR / "up-happy" / "index.html").read_text(encoding="utf-8")
    assert "Upgraded Co" in index


@respx.mock
def test_upgrade_store_wrong_key_401_and_unchanged(make_store, monkeypatch):
    monkeypatch.setenv("TILLA_LLM_KEY", "x")
    monkeypatch.setattr(engine, "generate", lambda desc: dict(CONTENT))
    _mock_allow()
    sid, key = _store_with_key(make_store, slug="up-key")

    r = client.post(
        "/upgrade-store",
        json={"slug": "up-key", "description": "x"},
        headers=_auth("wrong-key"),
    )
    assert r.status_code == 401
    r2 = client.post("/upgrade-store", json={"slug": "up-key", "description": "x"})
    assert r2.status_code == 401
    with SessionLocal() as s:
        st = s.get(Store, sid)
        assert st.content["store_name"] == "Old Name"  # untouched
        assert s.scalar(select(func.count()).select_from(ScreeningReceipt)) == 0


@respx.mock
def test_upgrade_store_block_422_old_content_intact(make_store, monkeypatch):
    monkeypatch.setenv("TILLA_LLM_KEY", "x")
    monkeypatch.setattr(engine, "generate", lambda desc: dict(CONTENT))
    respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(
            200, json={"verdict": "BLOCK", "risk_level": "high"}
        )
    )
    sid, key = _store_with_key(make_store, slug="up-block")

    r = client.post(
        "/upgrade-store",
        json={"slug": "up-block", "description": "bad pitch"},
        headers=_auth(key),
    )
    assert r.status_code == 422
    with SessionLocal() as s:
        st = s.get(Store, sid)
        assert st.content["store_name"] == "Old Name"  # old content still serving
        assert st.status == "live"
        assert s.scalar(select(func.count()).select_from(ScreeningReceipt)) == 0


@respx.mock
def test_upgrade_store_unavailable_503_no_pending(make_store, monkeypatch):
    monkeypatch.setenv("TILLA_LLM_KEY", "x")
    monkeypatch.setattr(engine, "generate", lambda desc: dict(CONTENT))
    respx.post(WARDEN_SCREEN_URL).mock(return_value=httpx.Response(503))
    sid, key = _store_with_key(make_store, slug="up-unavail")

    r = client.post(
        "/upgrade-store",
        json={"slug": "up-unavail", "description": "x"},
        headers=_auth(key),
    )
    assert r.status_code == 503
    with SessionLocal() as s:
        st = s.get(Store, sid)
        assert st.status == "live"  # never dropped to pending_screening
        assert st.content["store_name"] == "Old Name"


@respx.mock
def test_upgrade_store_unknown_slug_404(make_store, monkeypatch):
    monkeypatch.setenv("TILLA_LLM_KEY", "x")
    monkeypatch.setattr(engine, "generate", lambda desc: dict(CONTENT))
    _mock_allow()
    _store_with_key(make_store, slug="real-store")
    r = client.post(
        "/upgrade-store",
        json={"slug": "ghost-store", "description": "x"},
        headers=_auth("any"),
    )
    assert r.status_code == 404


@respx.mock
def test_upgrade_store_non_live_409(make_store, monkeypatch):
    monkeypatch.setenv("TILLA_LLM_KEY", "x")
    monkeypatch.setattr(engine, "generate", lambda desc: dict(CONTENT))
    _mock_allow()
    sid, key = _store_with_key(make_store, slug="up-pend", status="pending_screening")
    r = client.post(
        "/upgrade-store",
        json={"slug": "up-pend", "description": "x"},
        headers=_auth(key),
    )
    assert r.status_code == 409


def test_upgrade_store_no_llm_key_503(make_store, monkeypatch):
    monkeypatch.delenv("TILLA_LLM_KEY", raising=False)
    _store_with_key(make_store, slug="up-nollm")
    r = client.post(
        "/upgrade-store",
        json={"slug": "up-nollm", "description": "x"},
        headers=_auth("k"),
    )
    assert r.status_code == 503


def test_paid_get_refused_before_settle():
    # A PAID GET must be refused 405 (>=400 skips x402 settlement — no funds can
    # move on GET). The test app has no paywall, so GET reaches the handler
    # directly, exactly like a paid GET does in prod.
    for path in ("/create-store", "/upgrade-store", "/add-product"):
        r = client.get(path)
        assert r.status_code == 405, path
        assert r.headers["allow"] == "POST"
        assert "POST" in r.json()["how"]


# ======================================================= add-product
@respx.mock
def test_add_product_happy_and_additive(make_store):
    _mock_allow()
    sid, key = _store_with_key(make_store, slug="ap1", price_micro=9_000_000)

    r = client.post(
        "/add-product",
        json={"slug": "ap1", "name": "Second Thing", "price_usdt": 4.5},
        headers=_auth(key),
    )
    assert r.status_code == 200, r.text
    pid = r.json()["product_id"]

    with SessionLocal() as s:
        products = s.scalars(
            select(Product).where(Product.store_id == sid, Product.active.is_(True))
        ).all()
        assert len(products) == 2
        added = s.get(Product, pid)
        assert added.price_micro == 4_500_000
        assert added.pricing_model == "one_time"  # M8 default, byte-identical
        rec = s.scalar(select(ScreeningReceipt).where(ScreeningReceipt.store_id == sid))
        assert rec is not None and rec.mode == "demo"

    # feed enumerates both products; /buy still resolves the PRIMARY (lowest id)
    feed = client.get("/s/ap1/feed.json").json()
    assert len(feed["products"]) == 2
    assert agentic.resolve_price("/s/ap1/buy").amount == "9000000"


@respx.mock
def test_add_product_price_bounds_422(make_store):
    _mock_allow()
    _store_with_key(make_store, slug="ap-bounds")
    r = client.post(
        "/add-product",
        json={"slug": "ap-bounds", "name": "X", "price_usdt": 0},
        headers=_auth("k"),
    )
    assert r.status_code == 422
    r2 = client.post(
        "/add-product",
        json={"slug": "ap-bounds", "name": "X", "price_usdt": 99999},
        headers=_auth("k"),
    )
    assert r2.status_code == 422


@respx.mock
def test_add_product_wrong_key_401_no_row(make_store):
    _mock_allow()
    sid, key = _store_with_key(make_store, slug="ap-key")
    r = client.post(
        "/add-product",
        json={"slug": "ap-key", "name": "Nope", "price_usdt": 1},
        headers=_auth("wrong"),
    )
    assert r.status_code == 401
    with SessionLocal() as s:
        assert (
            s.scalar(
                select(func.count()).select_from(Product).where(Product.store_id == sid)
            )
            == 1
        )


@respx.mock
def test_add_product_blocked_name_422_no_row(make_store):
    respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(
            200, json={"verdict": "BLOCK", "risk_level": "high"}
        )
    )
    sid, key = _store_with_key(make_store, slug="ap-block")
    r = client.post(
        "/add-product",
        json={"slug": "ap-block", "name": "scammy name", "price_usdt": 2},
        headers=_auth(key),
    )
    assert r.status_code == 422
    with SessionLocal() as s:
        assert (
            s.scalar(
                select(func.count()).select_from(Product).where(Product.store_id == sid)
            )
            == 1
        )


@respx.mock
def test_mcp_create_checkout_optional_product_id(make_store):
    _mock_allow()
    sid, key = _store_with_key(make_store, slug="ap-mcp", price_micro=9_000_000)
    client.post(
        "/add-product",
        json={"slug": "ap-mcp", "name": "Product Two", "price_usdt": 3},
        headers=_auth(key),
    )
    with SessionLocal() as s:
        second = s.scalar(
            select(Product).where(
                Product.store_id == sid, Product.price_micro == 3_000_000
            )
        )
        pid2 = second.id

    def _call(args):
        return client.post(
            "/s/ap-mcp/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "create_checkout", "arguments": args},
            },
        ).json()["result"]["structuredContent"]

    # explicit product_id -> product 2 price + a matching offset
    r2 = _call({"product_id": pid2})
    assert 3_000_001 <= r2["amount_micro"] <= 3_004_999
    # omitted -> primary product (unchanged behaviour)
    r1 = _call({})
    assert 9_000_001 <= r1["amount_micro"] <= 9_004_999


# ======================================================= GET /s/:slug/buy refusal
def test_get_buy_returns_405_no_orders(make_store):
    make_store(slug="getbuy")
    r = client.get("/s/getbuy/buy")
    assert r.status_code == 405
    assert r.headers.get("Allow") == "POST"
    with SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(Order)) == 0


# ======================================================= dashboard marketplace panel
def _merchant_token(acct) -> str:
    msg = client.post(
        "/api/merchant/auth/nonce", json={"address": acct.address}
    ).json()["message"]
    sig = acct.sign_message(encode_defunct(text=msg)).signature.hex()
    return client.post(
        "/api/merchant/auth/verify", json={"address": acct.address, "signature": sig}
    ).json()["session_token"]


def test_marketplace_panel_unauthenticated_401():
    assert client.get("/api/merchant/marketplace").status_code == 401


def test_marketplace_panel_defaults_and_receipts(make_store):
    acct = Account.create()
    make_store(slug="mk1", pay_to=acct.address.lower(), price_micro=2_000_000)
    with SessionLocal() as s:
        sid = s.scalar(select(Store.id).where(Store.slug == "mk1"))
        s.add(
            ScreeningReceipt(
                store_id=sid, mode="demo", verdict="ALLOW", endpoint="demo-url"
            )
        )
        s.add(
            ScreeningReceipt(
                store_id=sid,
                mode="paid",
                verdict="ALLOW",
                risk_level="none",
                endpoint="paid-url",
                amount_micro=10000,
                tx_hash="0x" + "e" * 64,
            )
        )
        s.commit()

    token = _merchant_token(acct)
    r = client.get("/api/merchant/marketplace", headers=_auth(token))
    assert r.status_code == 200
    stores = r.json()["stores"]
    assert len(stores) == 1
    st = stores[0]
    assert st["marketplace_status"] == "unlisted"  # default
    assert st["marketplace_listed_at"] is None
    assert st["sold_count"] == {"web": 0, "agent": 0, "total": 0}
    modes = {row["mode"]: row for row in st["screening"]}
    assert modes["demo"]["tx_url"] is None
    assert modes["paid"]["tx_url"].endswith("0x" + "e" * 64)
    assert modes["paid"]["amount_usdt"] == "0.010000"


def test_marketplace_panel_sold_count_split(make_store):
    acct = Account.create()
    make_store(slug="mk2", pay_to=acct.address.lower(), price_micro=1_000_000)
    sid = None
    with SessionLocal() as s:
        sid = s.scalar(select(Store.id).where(Store.slug == "mk2"))
    # one web sale (delivered)
    cid = client.post("/api/checkout/mk2").json()["id"]
    with SessionLocal() as s:
        o = s.get(Order, cid)
        checkout.apply_transfer(
            s,
            o,
            o.expected_micro,
            tx_hash="0x" + secrets.token_hex(32),
            log_index=0,
            block_number=1,
            from_addr="0x" + "2" * 40,
            head=10**9,
        )
        s.commit()
    # one agent sale, settled -> delivered
    with SessionLocal() as s:
        store = s.get(Store, sid)
        product = agentic._active_product(s, sid)
        order, _ = agentic.fulfill_agent_order(s, store, product, PAYER, NONCE)
        s.commit()
        oid = order.id
    header = encode_payment_response_header(
        SettleResponse(success=True, transaction="0x" + "e" * 64, network="eip155:196")
    )
    agentic.record_settlement(oid, header)

    token = _merchant_token(acct)
    st = client.get("/api/merchant/marketplace", headers=_auth(token)).json()["stores"][
        0
    ]
    assert st["sold_count"] == {"web": 1, "agent": 1, "total": 2}


def test_marketplace_panel_idor_other_merchant_absent(make_store):
    mine = Account.create()
    theirs = Account.create()
    make_store(slug="mine-store", pay_to=mine.address.lower())
    make_store(slug="their-store", pay_to=theirs.address.lower())
    token = _merchant_token(mine)
    stores = client.get("/api/merchant/marketplace", headers=_auth(token)).json()[
        "stores"
    ]
    slugs = {s["slug"] for s in stores}
    assert slugs == {"mine-store"}  # never sees another merchant's store


def test_marketplace_panel_renders_via_textcontent():
    """The panel is XSS-safe by construction: every API string is written via
    textContent, never innerHTML, so hostile store/receipt text can't become markup."""
    from app.config import THEMES_DIR

    html = (THEMES_DIR / "_dashboard.html").read_text(encoding="utf-8")
    start = html.index("function loadMarketplace")
    end = html.index("// ---- wiring ----")
    section = html[start:end]
    assert "textContent" in section
    assert "innerHTML" not in section


# ======================================================= mark_listed command
def test_mark_listed_sets_status_timestamp_and_event(make_store):
    sid = make_store(slug="ml1")
    result = mark_listed("ml1", "listed")
    assert result["marketplace_status"] == "listed"
    assert result["marketplace_listed_at"] is not None
    with SessionLocal() as s:
        st = s.get(Store, sid)
        assert st.marketplace_status == "listed"
        assert st.marketplace_listed_at is not None
        events = [
            e.event for e in s.scalars(select(EventLog).where(EventLog.store_id == sid))
        ]
        assert "store.marketplace_status" in events


def test_mark_listed_invalid_status_rejected(make_store):
    make_store(slug="ml2")
    with pytest.raises(ValueError):
        mark_listed("ml2", "bogus")


def test_mark_listed_unknown_slug_rejected():
    with pytest.raises(LookupError):
        mark_listed("nope", "listed")


def test_mark_listed_non_listed_status_no_timestamp(make_store):
    sid = make_store(slug="ml3")
    mark_listed("ml3", "submitted")
    with SessionLocal() as s:
        st = s.get(Store, sid)
        assert st.marketplace_status == "submitted"
        assert st.marketplace_listed_at is None


# ======================================================= describe command
def _listable(make_store, slug, *, sales=0, reviews=(), status="live"):
    """A store shaped like a listing candidate: a named storefront, a named product,
    and `sales` delivered orders (optionally reviewed) so the live reputation the
    listing quotes is real, not stubbed."""
    sid = make_store(slug=slug, price_micro=9_000_000, status=status)
    with SessionLocal() as s:
        st = s.get(Store, sid)
        st.content = {"store_name": "Invoice Flow", "product_name": "Command Center"}
        product = s.scalar(select(Product).where(Product.store_id == sid))
        product.name = "Freelancer Command Center"
        for i in range(sales):
            s.add(
                Order(
                    id=f"{slug}o{i}",
                    store_id=sid,
                    pay_to="0x" + "a" * 40,
                    amount_micro=9_000_000,
                    expected_micro=9_000_000,
                    status="delivered",
                    channel="agent",
                    from_addr="0x" + str(i) * 40,
                )
            )
        s.flush()
        for i, rating in enumerate(reviews):
            s.add(
                Review(
                    store_id=sid,
                    order_id=f"{slug}o{i}",
                    from_addr="0x" + str(i) * 40,
                    rating=rating,
                    body="good",
                )
            )
        s.commit()
    return sid


def test_describe_quotes_the_live_reputation_numbers(make_store):
    _listable(make_store, "desc-rep", sales=4, reviews=(5, 4))
    listing = describe("desc-rep")
    service = listing["service"]

    assert service["serviceName"] == "Buy Freelancer Command Center from Invoice Flow"
    text = service["serviceDescription"]
    assert text == (
        "Freelancer Command Center from Invoice Flow, delivered as soon as the "
        "payment clears. Price 9 USDT. Sold 4 times, 100% of sales delivered with "
        "no dispute or refund, buyer rating 4.5 out of 5, seller trust tier "
        "established."
    )
    assert service["fee"] == "9"
    assert service["endpoint"].endswith("/s/desc-rep/buy")
    assert service["serviceType"] == "A2MCP"
    assert service["operation"] == "create"


def test_describe_matches_what_discovery_publishes(make_store):
    """The listing may never drift from the numbers an agent buyer already sees."""
    _listable(make_store, "desc-live", sales=4, reviews=(5, 4))
    row = next(
        r
        for r in client.get("/discovery/resources").json()["resources"]
        if r["slug"] == "desc-live"
    )
    text = describe("desc-live")["service"]["serviceDescription"]
    assert f"Sold {row['sold_count']} times" in text
    assert f"seller trust tier {row['trust_tier']}" in text
    assert f"buyer rating {row['review_avg']} out of 5" in text
    assert f"{round(row['success_rate'] * 100)}% of sales" in text


def test_describe_omits_stats_for_a_store_with_no_sales(make_store):
    _listable(make_store, "desc-new")
    text = describe("desc-new")["service"]["serviceDescription"]
    assert text == (
        "Freelancer Command Center from Invoice Flow, delivered as soon as the "
        "payment clears. Price 9 USDT."
    )
    assert "None" not in text
    assert "trust tier" not in text  # a new store reads as new, not as 'tier new'


def test_describe_omits_only_the_missing_review_clause(make_store):
    """Sales but no reviews yet: keep the sales facts, drop the rating clause."""
    _listable(make_store, "desc-noreview", sales=4)
    text = describe("desc-noreview")["service"]["serviceDescription"]
    assert "Sold 4 times" in text
    assert "seller trust tier established" in text
    assert "buyer rating" not in text


def test_describe_builds_the_runnable_onchainos_command(make_store):
    _listable(make_store, "desc-cmd", sales=4)
    listing = describe("desc-cmd")
    command = listing["command"]
    assert command.startswith("onchainos agent update --agent-id 6961 --service ")
    # the argument is one shell-quoted JSON array carrying exactly this service
    quoted = command.split(" --service ", 1)[1]
    (payload,) = json.loads(shlex.split(quoted)[0])
    assert payload == listing["service"]
    assert payload["fee"] == "9"  # a JSON string, per the runbook's content rules
    assert "http" not in payload["serviceDescription"]  # no links in listing copy


def test_describe_is_read_only(make_store):
    sid = _listable(make_store, "desc-ro", sales=4)
    describe("desc-ro")
    with SessionLocal() as s:
        assert s.get(Store, sid).marketplace_status == "unlisted"
        assert s.scalar(select(func.count()).select_from(EventLog)) == 0


def test_describe_unknown_slug_rejected():
    with pytest.raises(LookupError):
        describe("nope")


def test_describe_refuses_a_store_that_is_not_publicly_live(make_store):
    _listable(make_store, "desc-draft", status="draft")
    with pytest.raises(LookupError):
        describe("desc-draft")


def test_describe_refuses_a_store_with_no_active_product(make_store):
    sid = _listable(make_store, "desc-noprod")
    with SessionLocal() as s:
        s.scalar(select(Product).where(Product.store_id == sid)).active = False
        s.commit()
    with pytest.raises(LookupError):
        describe("desc-noprod")

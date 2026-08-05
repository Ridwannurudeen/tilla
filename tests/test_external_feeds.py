"""M13 external feed tests: the OpenAI JSON validates against the committed schema,
the Google RSS parses and round-trips a hostile store name escaped, non-live stores
404 on every endpoint, no merchant wallet leaks into any body, and the rate limit
trips. No network — feeds are generated purely from DB rows.
"""

import pathlib
import xml.etree.ElementTree as ET

import jsonschema
import yaml
from fastapi.testclient import TestClient

import app.main as main
from app.db import SessionLocal
from app.models import Product, Store, get_or_create_merchant

client = TestClient(main.app)

REPO = pathlib.Path(__file__).resolve().parent.parent
PAY_TO = "0x" + "a" * 40
HOSTILE = '<script>alert(1)</script> & ]]> "quote"'

_SCHEMA = yaml.safe_load(
    (REPO / "docs" / "openapi.external-feeds.yaml").read_text(encoding="utf-8")
)


def _seed(slug, status="live", store_name="My Store", product_name="Thing"):
    with SessionLocal() as s:
        merchant = get_or_create_merchant(s, PAY_TO)
        store = Store(
            slug=slug,
            merchant_id=merchant.id,
            status=status,
            pay_to=PAY_TO,
            theme="original.html",
            content={"store_name": store_name, "product_blurb": product_name},
        )
        s.add(store)
        s.flush()
        s.add(
            Product(
                store_id=store.id, name=product_name, price_micro=9_000_000, active=True
            )
        )
        s.commit()
        return store.id


# ---------------------------------------------------------------- OpenAI JSON
def test_openai_feed_validates_against_schema(make_store):
    _seed("of-1")
    r = client.get("/s/of-1/feed/openai.json")
    assert r.status_code == 200, r.text
    body = r.json()
    jsonschema.validate(instance=body, schema=_SCHEMA)
    assert body["feed"]["store"] == "of-1"
    assert body["products"][0]["enable_checkout"] is True
    assert body["products"][0]["price"]["currency"] == "USDT"


def test_openai_aggregate_validates_against_schema():
    _seed("agg-1")
    _seed("agg-2")
    r = client.get("/feeds/openai.json")
    assert r.status_code == 200, r.text
    body = r.json()
    jsonschema.validate(instance=body, schema=_SCHEMA)
    assert body["feed"]["store_count"] == 2
    slugs = {p["store"] for p in body["products"]}
    assert slugs == {"agg-1", "agg-2"}


def test_openai_feed_no_pay_to_leak():
    _seed("of-nopii")
    body = client.get("/s/of-nopii/feed/openai.json").text
    assert PAY_TO not in body  # merchant wallet never appears
    # the USDT0 contract IS present as the x402 asset (public), and that's expected
    from app import config

    assert config.USDT0 in body


def test_openai_headers_nosniff_and_cache():
    _seed("of-hdr")
    r = client.get("/s/of-hdr/feed/openai.json")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "max-age=300" in r.headers["Cache-Control"]


# ---------------------------------------------------------------- Google RSS
def test_google_feed_parses_as_xml():
    _seed("gf-1")
    r = client.get("/s/gf-1/feed/google.xml")
    assert r.status_code == 200, r.text
    root = ET.fromstring(r.text)  # well-formed or this raises
    assert root.tag == "rss"
    item = root.find("./channel/item")
    ns = {"g": "http://base.google.com/ns/1.0"}
    assert item.find("g:id", ns) is not None
    assert "USDT" in item.find("g:price", ns).text
    # Issue #8: condition is the merchant's fact and Tilla has no field for it, so no
    # item asserts one (optional for new goods, so the feed still validates).
    assert root.findall("./channel/item/g:condition", ns) == []


def test_google_feed_escapes_hostile_store_name():
    _seed("gf-xss", store_name=HOSTILE, product_name=HOSTILE)
    r = client.get("/s/gf-xss/feed/google.xml")
    assert r.status_code == 200
    # raw body must NOT contain the unescaped markup ...
    assert "<script>" not in r.text
    assert "]]>" not in r.text
    # ... but must still parse and round-trip the text decoded
    root = ET.fromstring(r.text)
    ns = {"g": "http://base.google.com/ns/1.0"}
    title = root.find("./channel/item/g:title", ns).text
    assert "<script>" in title  # ElementTree decodes the escaped entity back


def test_google_feed_no_pay_to_leak():
    _seed("gf-nopii")
    assert PAY_TO not in client.get("/s/gf-nopii/feed/google.xml").text


# ------------------------------------------------------- availability is earned
def test_feeds_emit_active_products_only():
    """The hard-coded "in_stock" is honest only because a feed can never carry an
    unbuyable product: every emitter goes through `_active_products`, the same active
    gate the buy path enforces. An inactive row must appear in no feed."""
    store_id = _seed("feed-active", product_name="Live Thing")
    with SessionLocal() as s:
        s.add(
            Product(
                store_id=store_id,
                name="Retired Thing",
                price_micro=5_000_000,
                active=False,
            )
        )
        s.commit()
    root = ET.fromstring(client.get("/s/feed-active/feed/google.xml").text)
    ns = {"g": "http://base.google.com/ns/1.0"}
    assert [i.text for i in root.findall("./channel/item/g:title", ns)] == [
        "Live Thing"
    ]
    body = client.get("/s/feed-active/feed/openai.json").json()
    assert [p["title"] for p in body["products"]] == ["Live Thing"]
    agg = client.get("/feeds/openai.json").json()
    assert [p["title"] for p in agg["products"]] == ["Live Thing"]


# ---------------------------------------------------------------- status gate
def test_pending_store_404_on_all_feeds():
    _seed("feed-pending", status="pending_screening")
    assert client.get("/s/feed-pending/feed/openai.json").status_code == 404
    assert client.get("/s/feed-pending/feed/google.xml").status_code == 404


def test_blocked_store_404_on_all_feeds():
    _seed("feed-blocked", status="blocked")
    assert client.get("/s/feed-blocked/feed/openai.json").status_code == 404
    assert client.get("/s/feed-blocked/feed/google.xml").status_code == 404


def test_unknown_store_404():
    assert client.get("/s/nope-xyz/feed/openai.json").status_code == 404
    assert client.get("/s/nope-xyz/feed/google.xml").status_code == 404


def test_pending_store_absent_from_aggregate():
    _seed("agg-live")
    _seed("agg-pending", status="pending_screening")
    body = client.get("/feeds/openai.json").json()
    slugs = {p["store"] for p in body["products"]}
    assert "agg-live" in slugs
    assert "agg-pending" not in slugs


# ---------------------------------------------------------------- rate limit
def test_openai_feed_rate_limit():
    _seed("of-rl")
    saw_429 = False
    for _ in range(65):
        if client.get("/s/of-rl/feed/openai.json").status_code == 429:
            saw_429 = True
            break
    assert saw_429

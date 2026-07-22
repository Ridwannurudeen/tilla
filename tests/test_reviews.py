"""Phase 3 verified-buyer reviews: only a wallet that COMPLETED a delivered
purchase can review (un-fakeable), the body is Warden-screened before it is stored,
one review per order, and the aggregate surfaces on discovery + the public reviews
endpoint. Screening is exercised through the real ``screen`` path with the Warden
HTTP endpoint respx-mocked (the test_api.py pattern); the TestClient's own ASGI
requests are untouched by respx.
"""

import httpx
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main
from app import delivery
from app.config import WARDEN_SCREEN_URL
from app.db import SessionLocal
from app.models import Order, Product, Review, Store, get_or_create_merchant

client = TestClient(main.app)

BUYER = "0x" + "b" * 40


def _mock_allow():
    respx.post(WARDEN_SCREEN_URL).mock(
        side_effect=lambda request: httpx.Response(200, json={"verdict": "ALLOW"})
    )


def _seed(slug="revshop", pay_to="0x" + "a" * 40, price_micro=9_500_000):
    with SessionLocal() as s:
        me = get_or_create_merchant(s, pay_to)
        store = Store(
            slug=slug,
            merchant_id=me.id,
            status="live",
            pay_to=pay_to,
            theme="original.html",
            description="a fine shop",
            content={"store_name": "Shoppe"},
        )
        s.add(store)
        s.flush()
        product = Product(
            store_id=store.id, name="Thing", price_micro=price_micro, active=True
        )
        s.add(product)
        s.commit()
        return store.id, product.id


def _order(store_id, product_id=None, status="delivered", from_addr=BUYER, oid="rord"):
    with SessionLocal() as s:
        s.add(
            Order(
                id=oid,
                store_id=store_id,
                product_id=product_id,
                pay_to="0x" + "a" * 40,
                amount_micro=9_500_000,
                expected_micro=9_500_100,
                status=status,
                from_addr=from_addr,
            )
        )
        s.commit()


def _auth(addr=BUYER):
    return {"Authorization": f"Bearer {delivery.mint_session_token(addr)}"}


@respx.mock
def test_buyer_can_review_owned_delivered_order():
    sid, pid = _seed()
    _order(sid, pid, status="delivered", oid="ok1")
    _mock_allow()
    r = client.post(
        "/api/library/review",
        json={"order_id": "ok1", "rating": 5, "body": "great product"},
        headers=_auth(),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"order_id": "ok1", "rating": 5}
    with SessionLocal() as s:
        row = s.scalar(select(Review).where(Review.order_id == "ok1"))
        assert row.rating == 5 and row.body == "great product"
        assert row.store_id == sid and row.product_id == pid
        assert row.from_addr == BUYER


@respx.mock
def test_paid_terminal_status_is_reviewable():
    # TERMINAL_DELIVERED = ('delivered', 'paid') — a 'paid' order is a completed sale.
    sid, pid = _seed(slug="revpaid")
    _order(sid, pid, status="paid", oid="paid1")
    _mock_allow()
    r = client.post(
        "/api/library/review",
        json={"order_id": "paid1", "rating": 4, "body": "worked for me"},
        headers=_auth(),
    )
    assert r.status_code == 200, r.text


def test_non_buyer_non_delivered_and_unauthed_are_refused():
    # No screening is reached on any of these paths, so no Warden mock is registered.
    sid, pid = _seed(slug="rev404")
    _order(sid, pid, status="delivered", from_addr=BUYER, oid="own1")
    # a different wallet cannot review someone else's order -> opaque 404
    foreign = client.post(
        "/api/library/review",
        json={"order_id": "own1", "rating": 4, "body": "nice"},
        headers=_auth("0x" + "c" * 40),
    )
    assert foreign.status_code == 404
    # the buyer's OWN order but not delivered -> opaque 404 (no completed purchase)
    _order(sid, pid, status="pending", from_addr=BUYER, oid="pend1")
    not_delivered = client.post(
        "/api/library/review",
        json={"order_id": "pend1", "rating": 4, "body": "nice"},
        headers=_auth(),
    )
    assert not_delivered.status_code == 404
    # no session at all -> 401
    unauthed = client.post(
        "/api/library/review",
        json={"order_id": "own1", "rating": 4, "body": "nice"},
    )
    assert unauthed.status_code == 401
    with SessionLocal() as s:
        assert s.scalar(select(Review)) is None  # nothing was written


@respx.mock
def test_duplicate_review_is_409():
    sid, pid = _seed(slug="revdup")
    _order(sid, pid, status="delivered", oid="dup1")
    _mock_allow()
    body = {"order_id": "dup1", "rating": 5, "body": "first and only"}
    first = client.post("/api/library/review", json=body, headers=_auth())
    assert first.status_code == 200
    second = client.post("/api/library/review", json=body, headers=_auth())
    assert second.status_code == 409
    with SessionLocal() as s:
        n = s.scalar(
            select(Review.id).where(Review.order_id == "dup1")
        )  # exactly one row
        assert n is not None
        assert (
            len(s.scalars(select(Review).where(Review.order_id == "dup1")).all()) == 1
        )


@respx.mock
def test_unscreened_body_is_422_and_stores_nothing():
    sid, pid = _seed(slug="revblock")
    _order(sid, pid, status="delivered", oid="blk1")
    respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(
            200, json={"verdict": "BLOCK", "risk_level": "high"}
        )
    )
    r = client.post(
        "/api/library/review",
        json={"order_id": "blk1", "rating": 1, "body": "something unsafe"},
        headers=_auth(),
    )
    assert r.status_code == 422
    assert r.json()["detail"] == "content did not pass safety screening"
    with SessionLocal() as s:
        assert s.scalar(select(Review).where(Review.order_id == "blk1")) is None


def test_rating_out_of_range_is_422():
    # pydantic bounds (1..5, strict int) reject before the handler body runs — no
    # screening call, so no Warden mock is needed.
    sid, pid = _seed(slug="revrange")
    _order(sid, pid, status="delivered", oid="rng1")
    r = client.post(
        "/api/library/review",
        json={"order_id": "rng1", "rating": 6, "body": "off the scale"},
        headers=_auth(),
    )
    assert r.status_code == 422


@respx.mock
def test_aggregate_surfaces_on_discovery_and_reviews_endpoint():
    sid, pid = _seed(slug="revagg")
    _order(sid, pid, status="delivered", from_addr="0x" + "1" * 40, oid="a1")
    _order(sid, pid, status="delivered", from_addr="0x" + "2" * 40, oid="a2")
    _mock_allow()
    client.post(
        "/api/library/review",
        json={"order_id": "a1", "rating": 5, "body": "love it"},
        headers=_auth("0x" + "1" * 40),
    )
    client.post(
        "/api/library/review",
        json={"order_id": "a2", "rating": 3, "body": "its ok"},
        headers=_auth("0x" + "2" * 40),
    )
    # public reviews endpoint: aggregate + screened bodies + abbreviated buyer
    rev = client.get("/s/revagg/reviews").json()
    assert rev["review_count"] == 2
    assert rev["review_avg"] == 4.0
    assert {x["rating"] for x in rev["reviews"]} == {5, 3}
    assert {x["body"] for x in rev["reviews"]} == {"love it", "its ok"}
    # buyer is surfaced abbreviated (0x1234…cdef), never the full 42-char wallet
    assert all("…" in x["buyer"] and len(x["buyer"]) < 42 for x in rev["reviews"])
    # discovery row carries the same aggregate
    row = next(
        r
        for r in client.get("/discovery/resources").json()["resources"]
        if r["slug"] == "revagg"
    )
    assert row["review_count"] == 2 and row["review_avg"] == 4.0


def test_no_reviews_aggregate_is_empty():
    _seed(slug="revnone")
    row = next(
        r
        for r in client.get("/discovery/resources").json()["resources"]
        if r["slug"] == "revnone"
    )
    assert row["review_count"] == 0 and row["review_avg"] is None
    rev = client.get("/s/revnone/reviews").json()
    assert (
        rev["review_count"] == 0 and rev["review_avg"] is None and rev["reviews"] == []
    )


def test_reviews_endpoint_404_for_unknown_store():
    assert client.get("/s/ghoststore/reviews").status_code == 404

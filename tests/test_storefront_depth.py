"""Roadmap Phase 3 storefront depth: membership tiers, pay-what-you-want, and
versioned releases pushed to past buyers.

Membership/PWYW ride the existing products.pricing_params JSON column (no schema
change); versioned releases use the additive deliverables.version marker. No real
network or funds: orders are confirmed by driving the M3 state machine directly, and
the merchant is manage-key gated.
"""

from sqlalchemy import select

import app.main as main
from app import checkout, delivery
from app.db import SessionLocal
from app.models import Deliverable, Order, Store
from fastapi.testclient import TestClient

client = TestClient(main.app)

PDF_V1 = b"%PDF-1.4\n" + b"version-one-bytes" * 500
PDF_V2 = b"%PDF-1.4\n" + b"version-two-bytes" * 500


def _give_key(slug: str) -> str:
    plain, key_hash = delivery.mint_manage_key()
    with SessionLocal() as s:
        store = s.scalar(select(Store).where(Store.slug == slug))
        store.manage_key_hash = key_hash
        s.commit()
    return plain


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _set_pricing(slug: str, key: str, body: dict):
    return client.post(f"/api/stores/{slug}/pricing", headers=_auth(key), json=body)


def _confirm(cid: str, from_addr: str, tx_hash: str):
    with SessionLocal() as s:
        order = s.get(Order, cid)
        checkout.apply_transfer(
            s,
            order,
            order.expected_micro - (order.paid_micro or 0),
            tx_hash=tx_hash,
            log_index=0,
            block_number=1,
            from_addr=from_addr,
            head=10**9,
        )
        s.commit()


TIERS = [
    {"name": "Basic", "price_micro": 5_000_000},
    {"name": "Pro", "price_micro": 9_000_000},
]


# ----------------------------------------------------------- membership tiers
def test_set_membership_tiers_persists_and_surfaces(make_store):
    make_store(slug="m1")
    key = _give_key("m1")
    r = _set_pricing(
        "m1", key, {"pricing_model": "one_time", "membership_tiers": TIERS}
    )
    assert r.status_code == 200
    assert r.json()["membership_tiers"] == TIERS
    # Feed + MCP surface the tiers under pricing.params.
    feed = client.get("/s/m1/feed.json").json()
    assert feed["products"][0]["pricing"]["params"]["membership_tiers"] == TIERS


def test_checkout_with_tier_maps_amount_and_records_tier(make_store):
    make_store(slug="m2")
    key = _give_key("m2")
    _set_pricing("m2", key, {"pricing_model": "one_time", "membership_tiers": TIERS})
    r = client.post("/api/checkout/m2", json={"tier": "Basic"})
    assert r.status_code == 200
    body = r.json()
    assert body["tier"] == "Basic"
    with SessionLocal() as s:
        order = s.get(Order, body["id"])
        assert order.amount_micro == 5_000_000  # tier price recorded on the amount
        assert order.expected_micro > 5_000_000  # plus the unique matching offset
    # The order response also surfaces the tier.
    assert client.get(f"/api/checkout/{body['id']}").json()["tier"] == "Basic"


def test_checkout_membership_requires_valid_tier(make_store):
    make_store(slug="m3")
    key = _give_key("m3")
    _set_pricing("m3", key, {"pricing_model": "one_time", "membership_tiers": TIERS})
    assert client.post("/api/checkout/m3", json={}).status_code == 400
    assert client.post("/api/checkout/m3", json={"tier": "Ghost"}).status_code == 404


def test_membership_tier_shows_in_library(make_store):
    make_store(slug="m4", pay_to="0x" + "a" * 40, delivery="SECRET")
    key = _give_key("m4")
    _set_pricing("m4", key, {"pricing_model": "one_time", "membership_tiers": TIERS})
    cid = client.post("/api/checkout/m4", json={"tier": "Pro"}).json()["id"]
    buyer = "0x" + "b" * 40
    _confirm(cid, buyer, "0x" + "1" * 64)
    token = delivery.mint_redeliver_token(cid)
    body = client.get(f"/api/redeliver/{token}").json()
    assert body["tier"] == "Pro"


def test_duplicate_tier_name_rejected(make_store):
    make_store(slug="m5")
    key = _give_key("m5")
    dupe = [
        {"name": "Gold", "price_micro": 5_000_000},
        {"name": "gold", "price_micro": 6_000_000},
    ]
    r = _set_pricing("m5", key, {"pricing_model": "one_time", "membership_tiers": dupe})
    assert r.status_code == 422


# ------------------------------------------------------------ pay-what-you-want
def test_pwyw_accepts_at_or_above_floor(make_store):
    make_store(slug="p1")
    key = _give_key("p1")
    _set_pricing("p1", key, {"pricing_model": "one_time", "pwyw_min_micro": 1_000_000})
    feed = client.get("/s/p1/feed.json").json()
    assert feed["products"][0]["pricing"]["params"]["pwyw_min_micro"] == 1_000_000
    r = client.post("/api/checkout/p1", json={"amount_micro": 3_000_000})
    assert r.status_code == 200
    with SessionLocal() as s:
        order = s.get(Order, r.json()["id"])
        assert order.amount_micro == 3_000_000
    # Exactly the floor is accepted.
    assert (
        client.post("/api/checkout/p1", json={"amount_micro": 1_000_000}).status_code
        == 200
    )


def test_pwyw_below_floor_refused(make_store):
    make_store(slug="p2")
    key = _give_key("p2")
    _set_pricing("p2", key, {"pricing_model": "one_time", "pwyw_min_micro": 2_000_000})
    assert (
        client.post("/api/checkout/p2", json={"amount_micro": 1_999_999}).status_code
        == 400
    )
    assert client.post("/api/checkout/p2", json={}).status_code == 400


def test_membership_and_pwyw_together_rejected(make_store):
    make_store(slug="p3")
    key = _give_key("p3")
    r = _set_pricing(
        "p3",
        key,
        {
            "pricing_model": "one_time",
            "membership_tiers": TIERS,
            "pwyw_min_micro": 1_000_000,
        },
    )
    assert r.status_code == 422


# ------------------------------------------------------------ versioned releases
def _upload(slug, key, content, versioned=False):
    files = {"file": ("guide.pdf", content, "application/pdf")}
    data = {"version": "true"} if versioned else {}
    return client.post(
        f"/api/stores/{slug}/deliverable", headers=_auth(key), files=files, data=data
    )


def _download_bytes(cid: str) -> bytes:
    token = delivery.mint_redeliver_token(cid)
    url = client.get(f"/api/redeliver/{token}").json()["download_url"]
    dtoken = url.rsplit("/", 1)[-1]
    r = client.get(f"/api/download/{dtoken}")
    assert r.status_code == 200
    return r.content


def test_versioned_release_pushes_new_version_to_past_buyer(make_store):
    make_store(slug="v1", pay_to="0x" + "a" * 40, delivery="LEGACY")
    key = _give_key("v1")
    assert _upload("v1", key, PDF_V1).status_code == 200
    cid = client.post("/api/checkout/v1").json()["id"]
    _confirm(cid, "0x" + "b" * 40, "0x" + "1" * 64)
    assert _download_bytes(cid) == PDF_V1  # bought version 1
    # Publish a NEW version — the past buyer rolls forward to it.
    r2 = _upload("v1", key, PDF_V2, versioned=True)
    assert r2.status_code == 200 and r2.json()["version"] == 2
    assert _download_bytes(cid) == PDF_V2


def test_plain_replace_does_not_roll_past_buyer_forward(make_store):
    make_store(slug="v2", pay_to="0x" + "a" * 40, delivery="LEGACY")
    key = _give_key("v2")
    assert _upload("v2", key, PDF_V1).status_code == 200
    cid = client.post("/api/checkout/v2").json()["id"]
    _confirm(cid, "0x" + "c" * 40, "0x" + "2" * 64)
    # A plain replace (no version flag) keeps version 1 — the past buyer stays on V1.
    r2 = _upload("v2", key, PDF_V2)
    assert r2.status_code == 200 and r2.json()["version"] == 1
    assert _download_bytes(cid) == PDF_V1


def test_version_without_prior_deliverable_conflicts(make_store):
    make_store(slug="v3", pay_to="0x" + "a" * 40)
    key = _give_key("v3")
    # No file deliverable exists yet — a versioned release has nothing to bump.
    assert _upload("v3", key, PDF_V2, versioned=True).status_code == 409


def test_version_column_backfills_to_one(make_store):
    make_store(slug="v4", pay_to="0x" + "a" * 40)
    key = _give_key("v4")
    _upload("v4", key, PDF_V1)
    with SessionLocal() as s:
        store = s.scalar(select(Store).where(Store.slug == "v4"))
        d = s.scalar(select(Deliverable).where(Deliverable.store_id == store.id))
        assert d.version == 1

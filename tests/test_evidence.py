"""Delivery-evidence tests: the signed bundle, its trust boundaries, and the free
re-check route. No network — every fact comes from rows this module inserts."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main
from app import config, evidence
from app.db import SessionLocal
from app.models import Order, Product, Store

client = TestClient(main.app)

TX = "0x" + "ab" * 32
UID = "0x" + "cd" * 32


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch):
    monkeypatch.setattr(config, "SIGNING_KEY", "k" * 40)


def _delivered(make_store, slug="ev-shop", status="delivered", **over):
    make_store(slug=slug, pay_to="0x" + "a" * 40)
    with SessionLocal() as s:
        store = s.scalar(select(Store).where(Store.slug == slug))
        prod = s.scalar(select(Product).where(Product.store_id == store.id))
        fields = dict(
            id=over.pop("oid", "ev-order-1"),
            store_id=store.id,
            product_id=prod.id,
            pay_to=store.pay_to,
            amount_micro=9_000_000,
            expected_micro=9_000_000,
            paid_micro=9_000_000,
            status=status,
            channel="agent",
            from_addr="0x" + "2" * 40,
            tx_hash=TX,
            block_number=66_000_000,
            content_hash="0x" + "ef" * 32,
            attestation_uid=UID,
            attest_tx="0x" + "12" * 32,
            attest_status="attested",
            eval_status="confirmed",
        )
        fields.update(over)
        s.add(Order(**fields))
        s.commit()
        return fields["id"]


# ------------------------------------------------------------------- signing
def test_signature_is_tamper_evident():
    b = {"found": True, "order_id": "x"}
    sig = evidence.sign_bundle(b)
    assert evidence.verify_bundle(b, sig)
    assert not evidence.verify_bundle({"found": True, "order_id": "y"}, sig)
    assert not evidence.verify_bundle(b, "v1=deadbeef")


def test_canonical_is_key_order_independent():
    a = {"b": 2, "a": 1}
    z = {"a": 1, "b": 2}
    assert evidence.canonical(a) == evidence.canonical(z)
    assert evidence.sign_bundle(a) == evidence.sign_bundle(z)


def test_signing_fails_closed_without_a_key(monkeypatch):
    # An unsigned bundle would still LOOK authoritative, so refuse to produce one.
    monkeypatch.setattr(config, "SIGNING_KEY", "")
    with pytest.raises(evidence.EvidenceUnavailable):
        evidence.sign_bundle({"found": True})
    assert evidence.verify_bundle({"found": True}, "v1=x") is False


# -------------------------------------------------------------------- lookup
def test_bundle_by_each_handle(make_store):
    oid = _delivered(make_store)
    with SessionLocal() as s:
        for kw in ({"order_id": oid}, {"tx_hash": TX}, {"attestation_uid": UID}):
            b = evidence.build_bundle(s, **kw)
            assert b["found"] is True, kw
            assert b["order_id"] == oid
            assert b["store_slug"] == "ev-shop"
            assert b["content_hash"] == "0x" + "ef" * 32
            assert b["settlement_tx"] == TX
            assert b["attestation_uid"] == UID


def test_bundle_never_leaks_pii_or_the_payload(make_store):
    _delivered(make_store, buyer_email="secret@example.com")
    with SessionLocal() as s:
        b = evidence.build_bundle(s, tx_hash=TX)
    blob = evidence.canonical(b)
    assert "secret@example.com" not in blob
    for banned in ("buyer_email", "x402_nonce", "referrer_addr", "payload"):
        assert banned not in b


def test_only_delivered_orders_are_evidence(make_store):
    # A pending or expired checkout says nothing about delivery.
    _delivered(make_store, slug="ev-pending", status="expired", oid="ev-exp")
    with SessionLocal() as s:
        b = evidence.build_bundle(s, order_id="ev-exp")
    assert b["found"] is False


def test_unknown_key_is_a_miss_not_an_error():
    with SessionLocal() as s:
        assert evidence.build_bundle(s, order_id="nope")["found"] is False
        assert evidence.build_bundle(s)["found"] is False


def test_pending_eval_auto_confirms_once_past_the_window(make_store):
    from datetime import timedelta

    from app import checkout
    from app.agentic import EVAL_WINDOW_DAYS

    old = checkout._now() - timedelta(days=EVAL_WINDOW_DAYS + 1)
    _delivered(
        make_store, slug="ev-old", oid="ev-old-1", eval_status="pending", paid_at=old
    )
    with SessionLocal() as s:
        assert evidence.build_bundle(s, order_id="ev-old-1")["eval_status"] == (
            "auto_confirmed"
        )
    # still inside the window -> stays pending, never dressed up as accepted
    _delivered(
        make_store,
        slug="ev-fresh",
        oid="ev-fresh-1",
        eval_status="pending",
        tx_hash="0x" + "99" * 32,
    )
    with SessionLocal() as s:
        assert evidence.build_bundle(s, order_id="ev-fresh-1")["eval_status"] == (
            "pending"
        )


# -------------------------------------------------------------------- routes
def test_verify_evidence_route_roundtrip(make_store):
    oid = _delivered(make_store)
    with SessionLocal() as s:
        bundle = evidence.build_bundle(s, order_id=oid)
    sig = evidence.sign_bundle(bundle)
    r = client.post("/api/verify-evidence", json={"bundle": bundle, "signature": sig})
    assert r.status_code == 200 and r.json()["valid"] is True
    bundle["paid_micro"] = 999_999_999  # a holder inflating the amount
    r = client.post("/api/verify-evidence", json={"bundle": bundle, "signature": sig})
    assert r.json()["valid"] is False

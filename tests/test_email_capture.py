"""M13 email-capture tests: the public waitlist stores CRLF-stripped, lowercased,
validated emails; duplicates are a silent no-op (no oracle); the per-store cap and
status gate hold; the merchant list/export are IDOR-gated and formula-injection-safe;
and any SENDING stays dormant (SMTP unset no-ops with an event_log row).
"""

from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.main as main
from app import config
from app.db import SessionLocal
from app.models import EmailSubscriber, EventLog, Order


client = TestClient(main.app)


def _auth(token):
    return {"Authorization": "Bearer " + token}


def _merchant_token(acct):
    r = client.post("/api/merchant/auth/nonce", json={"address": acct.address})
    sig = acct.sign_message(encode_defunct(text=r.json()["message"])).signature.hex()
    rv = client.post(
        "/api/merchant/auth/verify",
        json={"address": acct.address, "signature": sig},
    )
    assert rv.status_code == 200, rv.text
    return rv.json()["session_token"]


def _count(slug_store_id):
    with SessionLocal() as s:
        return s.scalar(
            select(func.count())
            .select_from(EmailSubscriber)
            .where(EmailSubscriber.store_id == slug_store_id)
        )


# ------------------------------------------------------------------ capture
def test_waitlist_stores_normalized_email(make_store):
    sid = make_store(slug="wl-1", pay_to="0x" + "a" * 40)
    r = client.post("/api/stores/wl-1/waitlist", json={"email": "  Foo@Bar.COM\r\n"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    with SessionLocal() as s:
        row = s.scalar(select(EmailSubscriber).where(EmailSubscriber.store_id == sid))
        assert row.email == "foo@bar.com"  # CRLF stripped, trimmed, lowercased
        assert row.source == "waitlist"


def test_waitlist_duplicate_is_silent(make_store):
    sid = make_store(slug="wl-dup", pay_to="0x" + "a" * 40)
    a = client.post("/api/stores/wl-dup/waitlist", json={"email": "dup@x.com"})
    b = client.post("/api/stores/wl-dup/waitlist", json={"email": "dup@x.com"})
    assert a.json() == b.json() == {"ok": True}  # identical silent response
    assert _count(sid) == 1  # no second row, no oracle


def test_waitlist_rejects_invalid_email(make_store):
    make_store(slug="wl-bad", pay_to="0x" + "a" * 40)
    assert (
        client.post(
            "/api/stores/wl-bad/waitlist", json={"email": "not-an-email"}
        ).status_code
        == 422
    )


def test_waitlist_rejects_header_injection(make_store):
    make_store(slug="wl-inj", pay_to="0x" + "a" * 40)
    # a CRLF-injected address collapses to an invalid address after stripping
    r = client.post(
        "/api/stores/wl-inj/waitlist",
        json={"email": "a@b.com\r\nBcc: victim@x.com"},
    )
    assert r.status_code == 422


def test_waitlist_per_store_cap(make_store, monkeypatch):
    make_store(slug="wl-cap", pay_to="0x" + "a" * 40)
    monkeypatch.setattr(config, "TILLA_SUBSCRIBERS_MAX", 2)
    assert (
        client.post(
            "/api/stores/wl-cap/waitlist", json={"email": "a@x.com"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/stores/wl-cap/waitlist", json={"email": "b@x.com"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/stores/wl-cap/waitlist", json={"email": "c@x.com"}
        ).status_code
        == 429
    )


def test_waitlist_pending_store_404(make_store):
    make_store(slug="wl-pending", pay_to="0x" + "a" * 40, status="pending_screening")
    assert (
        client.post(
            "/api/stores/wl-pending/waitlist", json={"email": "a@x.com"}
        ).status_code
        == 404
    )


def test_waitlist_unknown_store_404():
    assert (
        client.post("/api/stores/nope/waitlist", json={"email": "a@x.com"}).status_code
        == 404
    )


# --------------------------------------------------------------- merchant read
def test_merchant_lists_subscribers(make_store):
    acct = Account.create()
    make_store(slug="wl-list", pay_to=acct.address.lower())
    client.post("/api/stores/wl-list/waitlist", json={"email": "one@x.com"})
    client.post("/api/stores/wl-list/waitlist", json={"email": "two@x.com"})
    token = _merchant_token(acct)
    r = client.get("/api/merchant/stores/wl-list/subscribers", headers=_auth(token))
    assert r.status_code == 200, r.text
    emails = {s["email"] for s in r.json()["subscribers"]}
    assert emails == {"one@x.com", "two@x.com"}


def test_merchant_subscribers_idor(make_store):
    a, b = Account.create(), Account.create()
    make_store(slug="wl-own", pay_to=a.address.lower())
    client.post("/api/stores/wl-own/waitlist", json={"email": "x@x.com"})
    tok_b = _merchant_token(b)  # a different merchant
    r = client.get("/api/merchant/stores/wl-own/subscribers", headers=_auth(tok_b))
    assert r.status_code == 404  # not B's store -> indistinguishable from missing


def test_merchant_delete_subscriber(make_store):
    acct = Account.create()
    make_store(slug="wl-del", pay_to=acct.address.lower())
    client.post("/api/stores/wl-del/waitlist", json={"email": "gone@x.com"})
    token = _merchant_token(acct)
    r = client.request(
        "DELETE",
        "/api/merchant/stores/wl-del/subscribers",
        headers=_auth(token),
        json={"email": "gone@x.com"},
    )
    assert r.status_code == 200
    r2 = client.get("/api/merchant/stores/wl-del/subscribers", headers=_auth(token))
    assert r2.json()["subscribers"] == []  # soft-deleted, absent from the active list


def test_subscribers_csv_formula_injection_guard(make_store):
    acct = Account.create()
    make_store(slug="wl-csv", pay_to=acct.address.lower())
    # "=cmd@evil.com" is a valid address per the regex but a spreadsheet formula
    client.post("/api/stores/wl-csv/waitlist", json={"email": "=cmd@evil.com"})
    token = _merchant_token(acct)
    r = client.get(
        "/api/merchant/export/subscribers.csv?store=wl-csv", headers=_auth(token)
    )
    assert r.status_code == 200
    assert "'=cmd@evil.com" in r.text  # leading '=' neutralised with a single quote


def test_subscribers_export_idor(make_store):
    a, b = Account.create(), Account.create()
    make_store(slug="wl-exp", pay_to=a.address.lower())
    client.post("/api/stores/wl-exp/waitlist", json={"email": "x@x.com"})
    tok_b = _merchant_token(b)
    r = client.get(
        "/api/merchant/export/subscribers.csv?store=wl-exp", headers=_auth(tok_b)
    )
    assert r.status_code == 404  # other merchant cannot scope to this store


# ------------------------------------------------------------- SMTP dormant
def test_redelivery_send_is_dormant_without_smtp(make_store):
    """SENDING stays dormant: with SMTP unset, the redelivery sender no-ops and logs
    an event_log row instead of mailing anything."""
    from app import delivery

    make_store(slug="wl-smtp", pay_to="0x" + "a" * 40)
    cid = client.post("/api/checkout/wl-smtp").json()["id"]
    with SessionLocal() as s:
        order = s.get(Order, cid)
        order.buyer_email = "buyer@x.com"
        sent = delivery.send_redelivery_email(s, order, "https://x/y")
        s.commit()
        assert sent is False
        events = {
            e.event for e in s.scalars(select(EventLog).where(EventLog.order_id == cid))
        }
    assert "email.skipped" in events

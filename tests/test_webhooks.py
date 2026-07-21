"""M9 webhook tests: SSRF URL gate, HMAC signature, the backoff dispatcher (respx
-mocked httpx — no real network), and the deliver() enqueue seam.
"""

import hashlib
import hmac
import secrets
from datetime import datetime

import httpx
import pytest
import respx
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient
from sqlalchemy import select, update

import app.main as main
from app import checkout, config, webhooks
from app.db import SessionLocal
from app.models import Merchant, Order, WebhookDelivery

client = TestClient(main.app)

PUBLIC_URL = "https://93.184.216.34/hook"  # literal public IP → resolves offline
BUYER = "0x" + "2" * 40


def _auth(token):
    return {"Authorization": "Bearer " + token}


# ------------------------------------------------------------------ SSRF gate
@pytest.mark.parametrize(
    "url",
    [
        "http://93.184.216.34/",  # not https
        "https://localhost/",
        "https://127.0.0.1/",
        "https://10.0.0.1/",
        "https://172.16.0.1/",
        "https://192.168.1.1/",
        "https://169.254.169.254/",  # cloud metadata
        "https://100.64.0.1/",  # CGNAT
        "https://[::1]/",  # IPv6 loopback
        "https://[fe80::1]/",  # IPv6 link-local
    ],
)
def test_validate_rejects_internal_and_non_https(url):
    with pytest.raises(webhooks.WebhookURLError):
        webhooks.validate_webhook_url(url)


def test_validate_accepts_public_https():
    assert webhooks.validate_webhook_url(PUBLIC_URL) == PUBLIC_URL


# ------------------------------------------------------------- sign-in + setup
def _merchant_token(acct):
    r = client.post("/api/merchant/auth/nonce", json={"address": acct.address})
    message = r.json()["message"]
    sig = acct.sign_message(encode_defunct(text=message)).signature.hex()
    rv = client.post(
        "/api/merchant/auth/verify",
        json={"address": acct.address, "signature": sig},
    )
    assert rv.status_code == 200, rv.text
    return rv.json()["session_token"]


def _merchant_id(wallet):
    with SessionLocal() as s:
        m = s.scalar(select(Merchant).where(Merchant.wallet_address == wallet.lower()))
        return m.id


def _set_webhook(wallet, url=PUBLIC_URL, secret="whsec_testsecret"):
    with SessionLocal() as s:
        m = s.scalar(select(Merchant).where(Merchant.wallet_address == wallet.lower()))
        m.webhook_url = url
        m.webhook_secret = secret
        s.commit()


def _enqueue_row(merchant_id, event="order.paid", payload=None):
    with SessionLocal() as s:
        row = WebhookDelivery(
            merchant_id=merchant_id,
            event=event,
            order_id=None,
            payload=payload or {"order_id": "abc"},
            status="pending",
            attempts=0,
            next_attempt_at=datetime(2000, 1, 1),
        )
        s.add(row)
        s.commit()
        return row.id


def _make_due(row_id):
    with SessionLocal() as s:
        s.execute(
            update(WebhookDelivery)
            .where(WebhookDelivery.id == row_id)
            .values(next_attempt_at=datetime(2000, 1, 1))
        )
        s.commit()


def _row(row_id):
    with SessionLocal() as s:
        return s.get(WebhookDelivery, row_id)


# ---------------------------------------------------------- registration route
def test_register_rejects_internal_url_422(make_store):
    acct = Account.create()
    make_store(slug="wh-reg", pay_to=acct.address.lower())
    token = _merchant_token(acct)
    r = client.post(
        "/api/merchant/webhook",
        headers=_auth(token),
        json={"url": "https://127.0.0.1/x"},
    )
    assert r.status_code == 422


def test_register_and_get_never_leaks_secret(make_store):
    acct = Account.create()
    make_store(slug="wh-ok", pay_to=acct.address.lower())
    token = _merchant_token(acct)
    r = client.post(
        "/api/merchant/webhook", headers=_auth(token), json={"url": PUBLIC_URL}
    )
    assert r.status_code == 200
    assert r.json()["url"] == PUBLIC_URL
    assert r.json()["secret"].startswith("whsec_")
    # GET returns the url but never the secret
    g = client.get("/api/merchant/webhook", headers=_auth(token)).json()
    assert g["url"] == PUBLIC_URL
    assert "secret" not in g
    # DELETE clears it
    assert (
        client.delete("/api/merchant/webhook", headers=_auth(token)).status_code == 200
    )
    assert (
        client.get("/api/merchant/webhook", headers=_auth(token)).json()["url"] is None
    )


# ------------------------------------------------------------------- signature
@respx.mock
def test_delivery_signature_is_verifiable(make_store):
    acct = Account.create()
    make_store(slug="wh-sig", pay_to=acct.address.lower())
    mid = _merchant_id(acct.address)
    _set_webhook(acct.address)
    _enqueue_row(mid, payload={"order_id": "ord1"})
    route = respx.post(PUBLIC_URL).mock(return_value=httpx.Response(200))
    webhooks.dispatch_once()
    assert route.called
    req = route.calls.last.request
    body = req.content.decode()
    header = req.headers["X-Tilla-Signature"]  # "t=<ts>,v1=<mac>"
    parts = dict(kv.split("=", 1) for kv in header.split(","))
    expected = hmac.new(
        b"whsec_testsecret", f"{parts['t']}.{body}".encode(), hashlib.sha256
    ).hexdigest()
    assert parts["v1"] == expected


# -------------------------------------------------------------- dispatch cycle
@respx.mock
def test_success_marks_delivered(make_store):
    acct = Account.create()
    make_store(slug="wh-del", pay_to=acct.address.lower())
    mid = _merchant_id(acct.address)
    _set_webhook(acct.address)
    rid = _enqueue_row(mid)
    respx.post(PUBLIC_URL).mock(return_value=httpx.Response(200))
    webhooks.dispatch_once()
    row = _row(rid)
    assert row.status == "delivered"
    assert row.last_status_code == 200
    assert row.delivered_at is not None


@respx.mock
def test_failure_then_success_follows_backoff(make_store):
    acct = Account.create()
    make_store(slug="wh-retry", pay_to=acct.address.lower())
    mid = _merchant_id(acct.address)
    _set_webhook(acct.address)
    rid = _enqueue_row(mid)
    respx.post(PUBLIC_URL).mock(return_value=httpx.Response(500))
    webhooks.dispatch_once()
    row = _row(rid)
    assert row.status == "pending" and row.attempts == 1
    assert row.last_status_code == 500
    assert row.next_attempt_at > datetime(2000, 1, 1)  # backed off into the future
    # now it succeeds once due again
    respx.post(PUBLIC_URL).mock(return_value=httpx.Response(200))
    _make_due(rid)
    webhooks.dispatch_once()
    assert _row(rid).status == "delivered"


@respx.mock
def test_five_failures_go_dead(make_store):
    acct = Account.create()
    make_store(slug="wh-dead", pay_to=acct.address.lower())
    mid = _merchant_id(acct.address)
    _set_webhook(acct.address)
    rid = _enqueue_row(mid)
    respx.post(PUBLIC_URL).mock(return_value=httpx.Response(500))
    for _ in range(config.WEBHOOK_MAX_ATTEMPTS):
        _make_due(rid)
        webhooks.dispatch_once()
    row = _row(rid)
    assert row.status == "dead"
    assert row.attempts == config.WEBHOOK_MAX_ATTEMPTS


@respx.mock
def test_redirect_not_followed_counts_as_failure(make_store):
    acct = Account.create()
    make_store(slug="wh-redir", pay_to=acct.address.lower())
    mid = _merchant_id(acct.address)
    _set_webhook(acct.address)
    rid = _enqueue_row(mid)
    # a 302 to an internal host must NOT be followed; it is a plain failure
    respx.post(PUBLIC_URL).mock(
        return_value=httpx.Response(
            302, headers={"location": "http://169.254.169.254/"}
        )
    )
    webhooks.dispatch_once()
    row = _row(rid)
    assert row.status == "pending"
    assert row.last_status_code == 302


# ------------------------------------------------------------- deliver() seam
def _seed_sale(slug, from_addr=BUYER):
    cid = client.post("/api/checkout/" + slug).json()["id"]
    with SessionLocal() as s:
        o = s.get(Order, cid)
        exp = o.expected_micro
        checkout.apply_transfer(
            s,
            o,
            exp,
            tx_hash="0x" + secrets.token_hex(32),
            log_index=0,
            block_number=1,
            from_addr=from_addr,
            head=10**9,
        )
        s.commit()
    return cid


def test_deliver_enqueues_paid_and_delivered_once(make_store):
    acct = Account.create()
    make_store(slug="wh-seam", pay_to=acct.address.lower())
    _set_webhook(acct.address)
    cid = _seed_sale("wh-seam")
    with SessionLocal() as s:
        rows = s.scalars(
            select(WebhookDelivery).where(WebhookDelivery.order_id == cid)
        ).all()
        events = sorted(r.event for r in rows)
    assert events == ["order.delivered", "order.paid"]

    # re-delivering the already-delivered order enqueues nothing more (the
    # transition winner-only guard) — no duplicate events
    with SessionLocal() as s:
        o = s.get(Order, cid)
        checkout.deliver(s, o)
        s.commit()
    with SessionLocal() as s:
        n = len(
            s.scalars(
                select(WebhookDelivery).where(WebhookDelivery.order_id == cid)
            ).all()
        )
    assert n == 2


def test_no_webhook_configured_enqueues_nothing(make_store):
    acct = Account.create()
    make_store(slug="wh-none", pay_to=acct.address.lower())
    cid = _seed_sale("wh-none")  # merchant has no webhook_url
    with SessionLocal() as s:
        n = len(
            s.scalars(
                select(WebhookDelivery).where(WebhookDelivery.order_id == cid)
            ).all()
        )
    assert n == 0

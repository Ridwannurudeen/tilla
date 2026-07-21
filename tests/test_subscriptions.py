"""M8 subscription-proxy tests (no network, no funds): the sidecar is respx-mocked,
so nothing reaches a real Node process or the OKX facilitator. Covers the flag-off
503, the non-subscription 409 / unknown-store 404 gates, the challenge relay
(body + APP-PAYMENT-REQUIRED verbatim, request built from pricing_params), and the
verify/settle path — only a mocked facilitator settle SUCCESS creates an Order; a
verify reject, a settle failure, or an unreachable sidecar creates NOTHING.
"""

import httpx
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main
from app import config
from app.db import SessionLocal
from app.models import Order, Product, Store, get_or_create_merchant

client = TestClient(main.app)

SIDECAR = config.SUBSCRIPTION_SIDECAR_URL.rstrip("/")

SUB_PARAMS = {
    "amount_per_period_micro": 5_000_000,
    "period_sec": 2_592_000,
    "max_periods": 12,
    "plan_id": "pro-monthly",
    "plan_tier": 2,
    "plan_name": "Pro Monthly",
}


def _sub_store(slug: str, model: str = "subscription") -> int:
    with SessionLocal() as s:
        pay_to = "0x" + "a" * 40
        merchant = get_or_create_merchant(s, pay_to)
        store = Store(
            slug=slug,
            merchant_id=merchant.id,
            status="live",
            pay_to=pay_to,
            delivery="subscriber content",
            theme="original.html",
        )
        s.add(store)
        s.flush()
        s.add(
            Product(
                store_id=store.id,
                name="Sub",
                price_micro=5_000_000,
                active=True,
                pricing_model=model,
                pricing_params=SUB_PARAMS if model == "subscription" else None,
            )
        )
        s.commit()
        return store.id


def _enable(monkeypatch):
    monkeypatch.setattr(config, "SUBSCRIPTIONS_ENABLED", True)


def _orders():
    with SessionLocal() as s:
        return s.scalars(select(Order)).all()


# --------------------------------------------------------------- gates
def test_subscribe_503_when_flag_off():
    _sub_store("s503")
    r = client.post("/s/s503/subscribe")
    assert r.status_code == 503


def test_subscribe_non_subscription_409(monkeypatch):
    _sub_store("snone", model="one_time")
    _enable(monkeypatch)
    r = client.post("/s/snone/subscribe")
    assert r.status_code == 409


def test_subscribe_unknown_store_404(monkeypatch):
    _enable(monkeypatch)
    r = client.post("/s/ghost/subscribe")
    assert r.status_code == 404


# --------------------------------------------------------------- challenge relay
@respx.mock
def test_challenge_relayed_verbatim(monkeypatch):
    _sub_store("sc1")
    _enable(monkeypatch)
    route = respx.post(f"{SIDECAR}/subscriptions/challenge").mock(
        return_value=httpx.Response(
            402,
            json={"x402Version": 2, "accepts": [{"scheme": "period"}], "error": "pay"},
            headers={"APP-PAYMENT-REQUIRED": "BASE64HEADER"},
        )
    )
    r = client.post("/s/sc1/subscribe")
    assert r.status_code == 402
    assert r.json()["accepts"][0]["scheme"] == "period"
    assert r.headers["APP-PAYMENT-REQUIRED"] == "BASE64HEADER"
    # the proxy built the challenge request from pricing_params
    sent = route.calls.last.request
    import json

    body = json.loads(sent.content)
    assert body["payTo"] == "0x" + "a" * 40
    assert body["amount"] == "5000000"
    assert body["period"] == 2_592_000
    assert body["plan"]["id"] == "pro-monthly"


@respx.mock
def test_sidecar_unreachable_503(monkeypatch):
    _sub_store("sc2")
    _enable(monkeypatch)
    respx.post(f"{SIDECAR}/subscriptions/challenge").mock(
        side_effect=httpx.ConnectError("refused")
    )
    r = client.post("/s/sc2/subscribe")
    assert r.status_code == 503


# --------------------------------------------------------------- verify + settle
@respx.mock
def test_verify_reject_402_no_order(monkeypatch):
    _sub_store("sv1")
    _enable(monkeypatch)
    respx.post(f"{SIDECAR}/subscriptions/verify").mock(
        return_value=httpx.Response(200, json={"localVerify": {"ok": False}})
    )
    r = client.post(
        "/s/sv1/subscribe",
        headers={"PAYMENT-SIGNATURE": "sig"},
        json={"requirements": {"scheme": "period"}},
    )
    assert r.status_code == 402
    assert _orders() == []


@respx.mock
def test_settle_success_creates_order(monkeypatch):
    _sub_store("sv2")
    _enable(monkeypatch)
    respx.post(f"{SIDECAR}/subscriptions/verify").mock(
        return_value=httpx.Response(200, json={"localVerify": {"ok": True}})
    )
    respx.post(f"{SIDECAR}/subscriptions/settle").mock(
        return_value=httpx.Response(
            200,
            json={"settled": True, "facilitator": {"subscriptionId": "sub_123"}},
        )
    )
    r = client.post(
        "/s/sv2/subscribe",
        headers={"PAYMENT-SIGNATURE": "sig"},
        json={"requirements": {"scheme": "period"}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["subscription"] is True
    assert body["delivery"] == "subscriber content"
    orders = _orders()
    assert len(orders) == 1
    assert orders[0].channel == "agent"
    assert orders[0].status in ("delivered", "paid")


@respx.mock
def test_settle_failure_no_order(monkeypatch):
    _sub_store("sv3")
    _enable(monkeypatch)
    respx.post(f"{SIDECAR}/subscriptions/verify").mock(
        return_value=httpx.Response(200, json={"localVerify": {"ok": True}})
    )
    respx.post(f"{SIDECAR}/subscriptions/settle").mock(
        return_value=httpx.Response(502, json={"error": "facilitator subscribe failed"})
    )
    r = client.post(
        "/s/sv3/subscribe",
        headers={"PAYMENT-SIGNATURE": "sig"},
        json={"requirements": {"scheme": "period"}},
    )
    assert r.status_code == 502
    assert _orders() == []


@respx.mock
def test_settle_not_settled_flag_no_order(monkeypatch):
    _sub_store("sv4")
    _enable(monkeypatch)
    respx.post(f"{SIDECAR}/subscriptions/verify").mock(
        return_value=httpx.Response(200, json={"localVerify": {"ok": True}})
    )
    # 200 but settled=false must NOT deliver
    respx.post(f"{SIDECAR}/subscriptions/settle").mock(
        return_value=httpx.Response(200, json={"settled": False})
    )
    r = client.post(
        "/s/sv4/subscribe",
        headers={"PAYMENT-SIGNATURE": "sig"},
        json={"requirements": {"scheme": "period"}},
    )
    assert r.status_code == 502
    assert _orders() == []

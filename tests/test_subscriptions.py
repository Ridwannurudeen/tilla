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
# The server rebuilds the requirements by re-calling /subscriptions/challenge and
# using accepts[0] — never the buyer's copy. This is the store-bound object the
# challenge injected (payTo/amount from pricing_params + the SDK's enhanced fields).
REBUILT_REQS = {
    "scheme": "period",
    "network": "eip155:196",
    "payTo": "0x" + "a" * 40,
    "maxAmountRequired": "5000000",
}


def _mock_challenge():
    return respx.post(f"{SIDECAR}/subscriptions/challenge").mock(
        return_value=httpx.Response(
            402, json={"x402Version": 2, "accepts": [REBUILT_REQS], "error": "pay"}
        )
    )


@respx.mock
def test_verify_reject_402_no_order(monkeypatch):
    _sub_store("sv1")
    _enable(monkeypatch)
    _mock_challenge()
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
def test_verify_skipped_ok_rejected_402(monkeypatch):
    # A localVerify without ok:True (e.g. the sidecar's "skipped" shape) must NOT
    # settle — the gate now requires ok is True, not merely "not False".
    _sub_store("sv5")
    _enable(monkeypatch)
    _mock_challenge()
    respx.post(f"{SIDECAR}/subscriptions/verify").mock(
        return_value=httpx.Response(200, json={"localVerify": {"skipped": "x"}})
    )
    settle = respx.post(f"{SIDECAR}/subscriptions/settle").mock(
        return_value=httpx.Response(200, json={"settled": True})
    )
    r = client.post(
        "/s/sv5/subscribe",
        headers={"PAYMENT-SIGNATURE": "sig"},
        json={"requirements": {"scheme": "period"}},
    )
    assert r.status_code == 402
    assert settle.call_count == 0
    assert _orders() == []


@respx.mock
def test_settle_success_creates_order_with_rebuilt_requirements(monkeypatch):
    _sub_store("sv2")
    _enable(monkeypatch)
    _mock_challenge()
    respx.post(f"{SIDECAR}/subscriptions/verify").mock(
        return_value=httpx.Response(200, json={"localVerify": {"ok": True}})
    )
    settle = respx.post(f"{SIDECAR}/subscriptions/settle").mock(
        return_value=httpx.Response(
            200,
            json={"settled": True, "facilitator": {"subscriptionId": "sub_123"}},
        )
    )
    r = client.post(
        "/s/sv2/subscribe",
        headers={"PAYMENT-SIGNATURE": "sig"},
        # A buyer-supplied requirements paying themselves 1 micro must be IGNORED.
        json={"requirements": {"scheme": "period", "payTo": "0x" + "9" * 40}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["subscription"] is True
    assert body["delivery"] == "subscriber content"
    orders = _orders()
    assert len(orders) == 1
    assert orders[0].channel == "agent"
    assert orders[0].status in ("delivered", "paid")
    # The settle was bound to the SERVER-rebuilt requirements, not the client copy.
    import json

    sent = json.loads(settle.calls.last.request.content)
    assert sent["requirements"] == REBUILT_REQS


@respx.mock
def test_replayed_signature_is_idempotent(monkeypatch):
    # A replayed PAYMENT-SIGNATURE returns the existing order WITHOUT re-settling.
    _sub_store("sv6")
    _enable(monkeypatch)
    _mock_challenge()
    respx.post(f"{SIDECAR}/subscriptions/verify").mock(
        return_value=httpx.Response(200, json={"localVerify": {"ok": True}})
    )
    settle = respx.post(f"{SIDECAR}/subscriptions/settle").mock(
        return_value=httpx.Response(
            200, json={"settled": True, "facilitator": {"subscriptionId": "sub_9"}}
        )
    )
    args = dict(
        headers={"PAYMENT-SIGNATURE": "replay-sig"},
        json={"requirements": {"scheme": "period"}},
    )
    r1 = client.post("/s/sv6/subscribe", **args)
    r2 = client.post("/s/sv6/subscribe", **args)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["order_id"] == r2.json()["order_id"]
    assert len(_orders()) == 1
    assert settle.call_count == 1  # the replay never re-hit the facilitator


@respx.mock
def test_settle_failure_no_order(monkeypatch):
    _sub_store("sv3")
    _enable(monkeypatch)
    _mock_challenge()
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
    _mock_challenge()
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

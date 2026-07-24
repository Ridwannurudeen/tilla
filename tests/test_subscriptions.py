"""M8 subscription-proxy tests (no network, no funds): the sidecar is respx-mocked,
so nothing reaches a real Node process or the OKX facilitator. Covers the flag-off
503, the non-subscription 409 / unknown-store 404 gates, the challenge relay
(body + APP-PAYMENT-REQUIRED verbatim, request built from pricing_params), and the
verify/settle path — only a mocked facilitator settle carrying a real settlement tx
creates an Order; a verify reject, a settle failure, a rejection served as HTTP 200,
or an unreachable sidecar creates NOTHING.

Every settle double mirrors the REAL sidecar response ({settled, localVerify,
facilitator} — sidecar/server.js) wrapping the REAL OKX facilitator result body, whose
success and rejection shapes are taken from the prod event_log rows of 2026-07-23.
"""

import json
import httpx
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main
from app import config
from app.db import SessionLocal
from app.models import EventLog, Order, Product, Store, get_or_create_merchant

client = TestClient(main.app)

SIDECAR = config.SUBSCRIPTION_SIDECAR_URL.rstrip("/")

# The two real facilitator result bodies:
#   success   -> code 0 + data.{state, subId, txHash}
#   rejection -> HTTP 200 + code 30001 + EMPTY data + error_code/error_message
SUB_ID = "0x" + "7e" * 32
SETTLE_TX = "0x" + "f8" * 32


def _facilitator_ok(sub_id: str = SUB_ID) -> dict:
    return {
        "code": 0,
        "data": {"state": 0, "subId": sub_id, "txHash": SETTLE_TX},
        "detailMsg": "",
        "error_code": "0",
        "error_message": "",
        "msg": "",
    }


def _facilitator_error(message: str) -> dict:
    return {
        "code": 30001,
        "data": {},
        "detailMsg": "",
        "error_code": "30001",
        "error_message": message,
        "msg": message,
    }


def _settle_body(facilitator: dict) -> dict:
    """The sidecar's real /settle success body (server.js returns ``settled: true``
    whenever the facilitator call did not throw)."""
    return {"settled": True, "localVerify": {"ok": True}, "facilitator": facilitator}


def _events() -> list[str]:
    with SessionLocal() as s:
        return [e.event for e in s.scalars(select(EventLog)).all()]


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
        return_value=httpx.Response(200, json=_settle_body(_facilitator_ok()))
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
        return_value=httpx.Response(200, json=_settle_body(_facilitator_ok()))
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
    # The settlement tx lives ON THE ORDER ROW, not only inside the event payload.
    assert orders[0].tx_hash == SETTLE_TX
    assert orders[0].settle_ref == SUB_ID
    # The settle was bound to the SERVER-rebuilt requirements, not the client copy.
    import json

    sent = json.loads(settle.calls.last.request.content)
    assert sent["requirements"] == REBUILT_REQS


@respx.mock
def test_facilitator_error_in_settled_response_creates_no_order(monkeypatch):
    # THE PROD DEFECT (order cb662715a45e4231, 2026-07-23): the OKX facilitator serves
    # HTTP 200 for a REJECTED subscribe, carrying the rejection in the body. The sidecar
    # relayed it as settled=true, and the proxy delivered an order with a NULL tx.
    # An error_code/error_message in the facilitator result must fail CLOSED.
    _sub_store("sverr")
    _enable(monkeypatch)
    _mock_challenge()
    respx.post(f"{SIDECAR}/subscriptions/verify").mock(
        return_value=httpx.Response(200, json={"localVerify": {"ok": True}})
    )
    respx.post(f"{SIDECAR}/subscriptions/settle").mock(
        return_value=httpx.Response(
            200, json=_settle_body(_facilitator_error("permit_spender_mismatch"))
        )
    )
    r = client.post(
        "/s/sverr/subscribe",
        headers={"PAYMENT-SIGNATURE": "sig"},
        json={"requirements": {"scheme": "period"}},
    )
    assert r.status_code == 402
    assert _orders() == []
    # A failure is NEVER filed as 'subscription.settled'.
    events = _events()
    assert "subscription.settle_failed" in events
    assert "subscription.settled" not in events


@respx.mock
def test_settle_without_settlement_tx_creates_no_order(monkeypatch):
    # A success code with no txHash is not evidence that funds moved — no Order.
    _sub_store("svnotx")
    _enable(monkeypatch)
    _mock_challenge()
    respx.post(f"{SIDECAR}/subscriptions/verify").mock(
        return_value=httpx.Response(200, json={"localVerify": {"ok": True}})
    )
    reference = _facilitator_ok()
    del reference["data"]["txHash"]
    respx.post(f"{SIDECAR}/subscriptions/settle").mock(
        return_value=httpx.Response(200, json=_settle_body(reference))
    )
    r = client.post(
        "/s/svnotx/subscribe",
        headers={"PAYMENT-SIGNATURE": "sig"},
        json={"requirements": {"scheme": "period"}},
    )
    assert r.status_code == 502
    assert _orders() == []
    assert "subscription.settle_failed" in _events()
    assert "subscription.settled" not in _events()


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
        return_value=httpx.Response(200, json=_settle_body(_facilitator_ok()))
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


# --------------------------------------------------------------- payer binding
# A real period PAYMENT-SIGNATURE is a base64 JSON envelope carrying the buyer's
# signed terms; the recovered signer is terms.payer. _subscription_idem_key keys the
# replay on termsSignature, so a captured (public) terms-signature collides with the
# first-settle order even when an attacker swaps in their own payer field.
def _sub_sig(payer: str, terms_sig: str) -> str:
    import base64
    import json

    envelope = {"payload": {"terms": {"payer": payer}, "termsSignature": terms_sig}}
    return base64.b64encode(json.dumps(envelope).encode()).decode()


def _mock_settle(sub_id: str = SUB_ID):
    return respx.post(f"{SIDECAR}/subscriptions/settle").mock(
        return_value=httpx.Response(200, json=_settle_body(_facilitator_ok(sub_id)))
    )


PAYER = "0x" + "b" * 40
ATTACKER = "0x" + "e" * 40
TERMS_SIG = "0xdeadbeef"


def _add_license_deliverable(store_id: int) -> None:
    from app.models import Deliverable

    with SessionLocal() as s:
        s.add(Deliverable(store_id=store_id, kind="license", active=True))
        s.commit()


@respx.mock
def test_replay_withholds_entitlement_goods(monkeypatch):
    # The real defense: an entitlement-backed subscription (license key) is delivered
    # INLINE only on first settle (authenticated by the on-chain facilitator settle).
    # A replay of the PUBLIC envelope — by ANYONE, incl. an attacker swapping the
    # plaintext payer field — is NOT re-authenticated, so the secret is withheld and
    # replaced by the claim message; the real payer retrieves it via the wallet-session
    # /api/library (personal_sign as from_addr), which a replayer cannot forge.
    sid = _sub_store("sb1")
    _add_license_deliverable(sid)
    _enable(monkeypatch)
    _mock_challenge()
    respx.post(f"{SIDECAR}/subscriptions/verify").mock(
        return_value=httpx.Response(200, json={"localVerify": {"ok": True}})
    )
    settle = _mock_settle()
    reqs = {"requirements": {"scheme": "period"}}
    r1 = client.post(
        "/s/sb1/subscribe",
        headers={"PAYMENT-SIGNATURE": _sub_sig(PAYER, TERMS_SIG)},
        json=reqs,
    )
    assert r1.status_code == 200
    first = r1.json()
    assert first.get("license_key")  # first settle delivers the secret inline
    secret = first["license_key"]
    assert _orders()[0].from_addr == PAYER  # recorded from the on-chain-bound settle

    # Attacker replays with their own payer field swapped in — still same order (idem
    # on termsSignature), but the secret is gone.
    r2 = client.post(
        "/s/sb1/subscribe",
        headers={"PAYMENT-SIGNATURE": _sub_sig(ATTACKER, TERMS_SIG)},
        json=reqs,
    )
    assert r2.status_code == 200
    body2 = r2.json()
    assert "license_key" not in body2  # secret withheld on replay
    assert body2.get("claim") is True
    assert secret not in json.dumps(body2)  # the key never leaks anywhere in the body
    assert body2["order_id"] == first["order_id"]  # same order, no duplicate
    assert len(_orders()) == 1
    assert settle.call_count == 1  # replay never re-hit the facilitator


@respx.mock
def test_recharge_by_bound_payer_allowed(monkeypatch):
    # The bound payer re-presenting the same terms-signature is delivered the same
    # order idempotently (a legit re-charge), never refused.
    _sub_store("sb2")
    _enable(monkeypatch)
    _mock_challenge()
    respx.post(f"{SIDECAR}/subscriptions/verify").mock(
        return_value=httpx.Response(200, json={"localVerify": {"ok": True}})
    )
    settle = _mock_settle()
    args = dict(
        headers={"PAYMENT-SIGNATURE": _sub_sig(PAYER, TERMS_SIG)},
        json={"requirements": {"scheme": "period"}},
    )
    r1 = client.post("/s/sb2/subscribe", **args)
    r2 = client.post("/s/sb2/subscribe", **args)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["order_id"] == r2.json()["order_id"]
    assert r2.json()["delivery"] == "subscriber content"
    assert len(_orders()) == 1
    assert settle.call_count == 1


@respx.mock
def test_checksum_cased_payer_matches_binding(monkeypatch):
    # Binding is checksum-agnostic: the same wallet in mixed case still matches.
    _sub_store("sb3")
    _enable(monkeypatch)
    _mock_challenge()
    respx.post(f"{SIDECAR}/subscriptions/verify").mock(
        return_value=httpx.Response(200, json={"localVerify": {"ok": True}})
    )
    _mock_settle()
    r1 = client.post(
        "/s/sb3/subscribe",
        headers={"PAYMENT-SIGNATURE": _sub_sig(PAYER.lower(), TERMS_SIG)},
        json={"requirements": {"scheme": "period"}},
    )
    r2 = client.post(
        "/s/sb3/subscribe",
        headers={"PAYMENT-SIGNATURE": _sub_sig("0x" + "B" * 40, TERMS_SIG)},
        json={"requirements": {"scheme": "period"}},
    )
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["order_id"] == r2.json()["order_id"]


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


# ── browser-signing proxy: /prepare + /encode (the store-page Subscribe flow) ──
SELECTED_FULL = {
    "scheme": "period",
    "network": "eip155:196",
    "payTo": "0x" + "a" * 40,
    "maxAmountRequired": "5000000",
    "asset": "0x779ded0c9e1022225f8e0630b35a9b54be713736",
    "extra": {
        "contracts": {
            "subscription": "0x" + "e" * 40,
            "permit2": "0x000000000022D473030F116dDEE9F6B43aC78BA3",
        }
    },
}


def _mock_challenge_full():
    return respx.post(f"{SIDECAR}/subscriptions/challenge").mock(
        return_value=httpx.Response(402, json={"accepts": [SELECTED_FULL]})
    )


def test_prepare_503_when_flag_off():
    _sub_store("sp0")
    r = client.post("/s/sp0/subscribe/prepare", json={"payer": "0x" + "b" * 40})
    assert r.status_code == 503


@respx.mock
def test_prepare_rejects_bad_payer(monkeypatch):
    _sub_store("sp1")
    _enable(monkeypatch)
    r = client.post("/s/sp1/subscribe/prepare", json={"payer": "nope"})
    assert r.status_code == 422


@respx.mock
def test_prepare_returns_envelopes_and_approval(monkeypatch):
    import app.chain as chain

    _sub_store("sp2")
    _enable(monkeypatch)
    _mock_challenge_full()
    # Permit2 allowance(owner,token,spender) -> nonce is the 3rd 32-byte word (= 3).
    monkeypatch.setattr(
        chain,
        "eth_call",
        lambda cfg, to, data, timeout=None: "0x" + "0" * 128 + f"{3:064x}",
    )
    prep = respx.post(f"{SIDECAR}/subscriptions/prepare").mock(
        return_value=httpx.Response(
            200,
            json={
                "permitTypedData": {"primaryType": "PermitSingle", "message": {}},
                "termsTypedData": {"primaryType": "SubscriptionTerms", "message": {}},
                "permitSingle": {"details": {"nonce": 3}},
                "terms": {"payer": "0x" + "b" * 40},
            },
        )
    )
    r = client.post("/s/sp2/subscribe/prepare", json={"payer": "0x" + "b" * 40})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "permitTypedData" in body and "termsTypedData" in body
    assert body["approval"]["token"] == SELECTED_FULL["asset"]
    assert body["approval"]["amount_per_period_micro"] == 5_000_000
    # The proxy read the buyer's on-chain nonce (3) and passed it to the sidecar.
    sent = json.loads(prep.calls[0].request.content)
    assert sent["nonce"] == 3 and sent["payer"] == "0x" + "b" * 40


def test_encode_503_when_flag_off():
    _sub_store("se0")
    r = client.post("/s/se0/subscribe/encode", json={})
    assert r.status_code == 503


@respx.mock
def test_encode_missing_fields_422(monkeypatch):
    _sub_store("se1")
    _enable(monkeypatch)
    r = client.post("/s/se1/subscribe/encode", json={"permitSingle": {"x": 1}})
    assert r.status_code == 422


@respx.mock
def test_encode_returns_signature(monkeypatch):
    _sub_store("se2")
    _enable(monkeypatch)
    _mock_challenge_full()
    respx.post(f"{SIDECAR}/subscriptions/encode").mock(
        return_value=httpx.Response(200, json={"paymentSignature": "B64SIG"})
    )
    r = client.post(
        "/s/se2/subscribe/encode",
        json={
            "permitSingle": {"x": 1},
            "permitSingleSignature": "0xsig1",
            "terms": {"y": 2},
            "termsSignature": "0xsig2",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["paymentSignature"] == "B64SIG"


# ── cancel proxy (subscribe -> charge -> CANCEL lifecycle) ──
_SUBID = "0x" + "1" * 64


def test_cancel_prepare_503_when_flag_off():
    r = client.post("/s/anystore/subscribe/cancel/prepare", json={"subId": _SUBID})
    assert r.status_code == 503


def test_cancel_prepare_bad_subid_422(monkeypatch):
    _enable(monkeypatch)
    r = client.post("/s/anystore/subscribe/cancel/prepare", json={"subId": "nope"})
    assert r.status_code == 422


@respx.mock
def test_cancel_prepare_relays(monkeypatch):
    _enable(monkeypatch)
    respx.post(f"{SIDECAR}/subscriptions/cancel-prepare").mock(
        return_value=httpx.Response(
            200,
            json={
                "cancelTypedData": {"primaryType": "CancelAuth"},
                "cancelAuth": {"action": 0, "subId": _SUBID, "initiator": 0},
            },
        )
    )
    r = client.post("/s/anystore/subscribe/cancel/prepare", json={"subId": _SUBID})
    assert r.status_code == 200, r.text
    assert r.json()["cancelAuth"]["subId"] == _SUBID


def test_cancel_missing_signature_422(monkeypatch):
    _enable(monkeypatch)
    r = client.post(
        "/s/anystore/subscribe/cancel",
        json={"subId": _SUBID, "cancelAuth": {"action": 0}},
    )
    assert r.status_code == 422


@respx.mock
def test_cancel_submits_and_relays(monkeypatch):
    _enable(monkeypatch)
    respx.post(f"{SIDECAR}/subscriptions/cancel").mock(
        return_value=httpx.Response(
            200,
            json={"canceled": True, "facilitator": {"code": "0", "data": {"state": 3}}},
        )
    )
    r = client.post(
        "/s/anystore/subscribe/cancel",
        json={"subId": _SUBID, "cancelAuth": {"action": 0, "signature": "0xsig"}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["canceled"] is True


@respx.mock
def test_cancel_facilitator_reject_402(monkeypatch):
    _enable(monkeypatch)
    respx.post(f"{SIDECAR}/subscriptions/cancel").mock(
        return_value=httpx.Response(402, json={"canceled": False, "error": "rejected"})
    )
    r = client.post(
        "/s/anystore/subscribe/cancel",
        json={"subId": _SUBID, "cancelAuth": {"action": 0, "signature": "0xsig"}},
    )
    assert r.status_code == 402

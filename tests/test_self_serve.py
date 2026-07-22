"""Self-serve paid create-store — screen-before-pay, on-chain payment verification,
tx-reuse + owner-scoping guards, and the post-payment generation retry.

All chain access is mocked (no network, no funds); screening and generation are
stubbed so the flow itself is what's under test, not the LLM.
"""

import types

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

import app.main as main
from app import chain, config, engine, screening
from app.screening import ScreeningBlocked

client = TestClient(main.app)

TILLA = "0x" + "f" * 40  # Tilla's create-store fee address (PAY_TO_ADDRESS)
FEE = 1_000_000  # 1 USDT in micro


@pytest.fixture(autouse=True)
def _rail_and_llm(monkeypatch):
    """Every test needs Tilla's fee address configured + generation 'available'."""
    monkeypatch.setenv("PAY_TO_ADDRESS", TILLA)
    monkeypatch.setenv("TILLA_LLM_KEY", "test-key")


def _auth(token):
    return {"Authorization": "Bearer " + token}


def _merchant_token(acct) -> str:
    msg = client.post(
        "/api/merchant/auth/nonce", json={"address": acct.address}
    ).json()["message"]
    sig = acct.sign_message(encode_defunct(text=msg)).signature.hex()
    return client.post(
        "/api/merchant/auth/verify",
        json={"address": acct.address, "signature": sig},
    ).json()["session_token"]


def _allow_screen(monkeypatch):
    monkeypatch.setattr(
        screening, "screen", lambda *_a, **_k: types.SimpleNamespace(status="live")
    )


def _stub_create_store(monkeypatch, slug="cool-beans", status="live"):
    monkeypatch.setattr(
        engine, "create_store", lambda *_a, **_k: {"slug": slug, "status": status}
    )


def _pad(addr: str) -> str:
    return "0x" + "0" * 24 + addr.lower()[2:]


def _receipt(from_addr, to_addr, value, status="0x1"):
    return {
        "status": status,
        "logs": [
            {
                "address": config.USDT0,
                "topics": [config.TRANSFER_TOPIC, _pad(from_addr), _pad(to_addr)],
                "data": hex(value),
                "transactionHash": "0x" + "e" * 64,
                "logIndex": "0x0",
                "blockNumber": "0x1",
            }
        ],
    }


def _mock_tx(monkeypatch, receipt):
    monkeypatch.setattr(chain, "get_transaction_receipt", lambda *_a, **_k: receipt)


def _intent(token, description="single-origin coffee beans", theme=None):
    body = {"description": description}
    if theme:
        body["theme"] = theme
    return client.post("/api/merchant/create-store", json=body, headers=_auth(token))


# ------------------------------------------------------------------------- tests
def test_intent_screens_before_payment(monkeypatch):
    acct = Account.create()
    token = _merchant_token(acct)
    monkeypatch.setattr(
        screening,
        "screen",
        lambda *_a, **_k: (_ for _ in ()).throw(
            ScreeningBlocked({"risk_level": "high"})
        ),
    )
    r = _intent(token, description="something clearly prohibited")
    assert r.status_code == 422, r.text
    # No intent row was created — nothing to pay for.
    assert r.json()["detail"] == "content did not pass safety screening"


def test_llm_unconfigured_is_503(monkeypatch):
    acct = Account.create()
    token = _merchant_token(acct)
    _allow_screen(monkeypatch)
    monkeypatch.delenv("TILLA_LLM_KEY", raising=False)
    r = _intent(token)
    assert r.status_code == 503


def test_pay_happy_path_creates_store(monkeypatch):
    acct = Account.create()
    token = _merchant_token(acct)
    _allow_screen(monkeypatch)
    _stub_create_store(monkeypatch, slug="cool-beans")

    intent = _intent(token, theme="bold")
    assert intent.status_code == 200, intent.text
    body = intent.json()
    assert body["status"] == "pending"
    assert body["amount_micro"] == FEE
    assert body["pay_to"].lower() == TILLA

    _mock_tx(monkeypatch, _receipt(acct.address, TILLA, FEE))
    pay = client.post(
        f"/api/merchant/create-store/{body['id']}/pay",
        json={"tx_hash": "0x" + "a" * 64},
        headers=_auth(token),
    )
    assert pay.status_code == 200, pay.text
    assert pay.json()["status"] == "live"
    assert pay.json()["slug"] == "cool-beans"
    assert pay.json()["url"] == "/s/cool-beans/"


def test_pay_wrong_amount_rejected(monkeypatch):
    acct = Account.create()
    token = _merchant_token(acct)
    _allow_screen(monkeypatch)
    _stub_create_store(monkeypatch)
    cid = _intent(token).json()["id"]
    _mock_tx(monkeypatch, _receipt(acct.address, TILLA, FEE - 1))  # underpaid
    r = client.post(
        f"/api/merchant/create-store/{cid}/pay",
        json={"tx_hash": "0x" + "b" * 64},
        headers=_auth(token),
    )
    assert r.status_code == 400, r.text


def test_pay_wrong_recipient_rejected(monkeypatch):
    acct = Account.create()
    token = _merchant_token(acct)
    _allow_screen(monkeypatch)
    _stub_create_store(monkeypatch)
    cid = _intent(token).json()["id"]
    _mock_tx(monkeypatch, _receipt(acct.address, "0x" + "1" * 40, FEE))  # not Tilla
    r = client.post(
        f"/api/merchant/create-store/{cid}/pay",
        json={"tx_hash": "0x" + "c" * 64},
        headers=_auth(token),
    )
    assert r.status_code == 400, r.text


def test_pay_reused_tx_is_409(monkeypatch):
    acct = Account.create()
    token = _merchant_token(acct)
    _allow_screen(monkeypatch)
    _stub_create_store(monkeypatch)
    first = _intent(token).json()["id"]
    second = _intent(token).json()["id"]
    _mock_tx(monkeypatch, _receipt(acct.address, TILLA, FEE))
    tx = "0x" + "d" * 64
    r1 = client.post(
        f"/api/merchant/create-store/{first}/pay",
        json={"tx_hash": tx},
        headers=_auth(token),
    )
    assert r1.status_code == 200, r1.text
    r2 = client.post(
        f"/api/merchant/create-store/{second}/pay",
        json={"tx_hash": tx},
        headers=_auth(token),
    )
    assert r2.status_code == 409, r2.text


def test_creation_is_owner_scoped(monkeypatch):
    owner = Account.create()
    other = Account.create()
    _allow_screen(monkeypatch)
    _stub_create_store(monkeypatch)
    cid = _intent(_merchant_token(owner)).json()["id"]
    _mock_tx(monkeypatch, _receipt(owner.address, TILLA, FEE))
    # A different signed-in merchant cannot pay (or even see) someone else's intent.
    r = client.post(
        f"/api/merchant/create-store/{cid}/pay",
        json={"tx_hash": "0x" + "9" * 64},
        headers=_auth(_merchant_token(other)),
    )
    assert r.status_code == 404, r.text


def test_generation_outage_then_retry(monkeypatch):
    acct = Account.create()
    token = _merchant_token(acct)
    _allow_screen(monkeypatch)

    def _outage(*_a, **_k):
        raise engine.GenerationUnavailable("model down")

    monkeypatch.setattr(engine, "create_store", _outage)
    cid = _intent(token).json()["id"]
    _mock_tx(monkeypatch, _receipt(acct.address, TILLA, FEE))
    pay = client.post(
        f"/api/merchant/create-store/{cid}/pay",
        json={"tx_hash": "0x" + "7" * 64},
        headers=_auth(token),
    )
    # Payment verified but generation failed: 'paid', no slug, no re-charge.
    assert pay.status_code == 200, pay.text
    assert pay.json()["status"] == "paid"
    assert pay.json()["slug"] is None

    # Model recovers; retry generates without touching payment.
    _stub_create_store(monkeypatch, slug="recovered-store")
    retry = client.post(f"/api/merchant/create-store/{cid}/retry", headers=_auth(token))
    assert retry.status_code == 200, retry.text
    assert retry.json()["status"] == "live"
    assert retry.json()["slug"] == "recovered-store"

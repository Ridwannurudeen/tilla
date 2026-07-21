"""M13 ACP /checkout_sessions tests: the dormant-503 gate, the full create -> update
-> retrieve -> complete lifecycle over the proven M3 order machinery, the tx-hash
complete path (respx-mocked RPC), cancel, expired-order reconciliation, and
Idempotency-Key replay. No network, no funds — the complete path verifies an inbound
tx hash exactly like the human /tx route.
"""

import json
import secrets

import httpx
import respx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.main as main
from app import chain, config
from app.db import SessionLocal
from app.models import AcpSession, Order, ProcessedTransfer

client = TestClient(main.app)


@pytest.fixture
def acp_on(monkeypatch):
    monkeypatch.setattr(config, "ACP_ENABLED", True)


def _pid(sid):
    from app import agentic

    with SessionLocal() as s:
        return agentic._active_product(s, sid).id


# ------------------------------------------------------------- on-chain mock
def _log(to_addr, value, tx_hash, block=100):
    return {
        "address": config.USDT0,
        "topics": [
            config.TRANSFER_TOPIC,
            chain.pad_address("0x" + "b" * 40),
            chain.pad_address(to_addr),
        ],
        "data": hex(value),
        "transactionHash": tx_hash,
        "logIndex": hex(0),
        "blockNumber": hex(block),
    }


class Rpc:
    def __init__(self, receipts=None, head=10**9):
        self.receipts = receipts or {}
        self.head = head

    def handler(self, request):
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        if method == "eth_blockNumber":
            result = hex(self.head)
        elif method == "eth_getTransactionReceipt":
            result = self.receipts.get(params[0].lower())
        else:
            result = None
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )

    def install(self):
        respx.post(config.RPC_URL).mock(side_effect=self.handler)


def _receipt(logs, status="0x1", block=100):
    return {"status": status, "blockNumber": hex(block), "logs": logs}


# ------------------------------------------------------------- dormant gate
def test_all_endpoints_503_when_disabled(make_store):
    make_store(slug="acp-off", pay_to="0x" + "a" * 40)
    assert client.post("/s/acp-off/checkout_sessions", json={}).status_code == 503
    assert client.get("/s/acp-off/checkout_sessions/x").status_code == 503
    assert client.post("/s/acp-off/checkout_sessions/x").status_code == 503
    assert client.post("/s/acp-off/checkout_sessions/x/complete").status_code == 503
    assert client.post("/s/acp-off/checkout_sessions/x/cancel").status_code == 503


# ------------------------------------------------------------- create
def test_create_allocates_ready_session(make_store, acp_on):
    sid = make_store(slug="acp-c1", pay_to="0x" + "a" * 40, price_micro=9_000_000)
    r = client.post(
        "/s/acp-c1/checkout_sessions",
        json={"items": [{"id": _pid(sid), "quantity": 1}]},
        headers={"API-Version": "2026-04-17"},
    )
    assert r.status_code == 201, r.text
    assert r.headers.get("API-Version") == "2026-04-17"  # echoed
    body = r.json()
    assert body["id"].startswith("acp_")
    assert body["status"] == "ready_for_payment"
    assert body["fulfillment"]["type"] == "digital"
    assert body["payment"]["custom"]["provider"] == "x402"
    assert body["payment"]["fallback"]["provider"] == "onchain_usdt0"
    assert body["payment"]["fallback"]["pay_to"] == "0x" + "a" * 40
    # a real unique-amount order was allocated behind the session
    with SessionLocal() as s:
        acp = s.get(AcpSession, body["id"])
        assert acp.order_id is not None
        assert s.get(Order, acp.order_id).status == "pending"


def test_create_defaults_to_primary_product(make_store, acp_on):
    make_store(slug="acp-c2", pay_to="0x" + "a" * 40)
    r = client.post("/s/acp-c2/checkout_sessions", json={})
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "ready_for_payment"


def test_create_rejects_bad_line_item(make_store, acp_on):
    make_store(slug="acp-c3", pay_to="0x" + "a" * 40)
    r = client.post(
        "/s/acp-c3/checkout_sessions", json={"items": [{"id": 99999, "quantity": 1}]}
    )
    assert r.status_code == 422


def test_create_rejects_quantity_over_one(make_store, acp_on):
    sid = make_store(slug="acp-c4", pay_to="0x" + "a" * 40)
    r = client.post(
        "/s/acp-c4/checkout_sessions",
        json={"items": [{"id": _pid(sid), "quantity": 2}]},
    )
    assert r.status_code == 422


def test_create_captures_affiliate(make_store, acp_on):
    make_store(slug="acp-aff", pay_to="0x" + "a" * 40)
    ref = "0x" + "1" * 40
    r = client.post(
        "/s/acp-aff/checkout_sessions", json={"affiliate": {"address": ref}}
    )
    assert r.status_code == 201, r.text
    with SessionLocal() as s:
        acp = s.get(AcpSession, r.json()["id"])
        assert s.get(Order, acp.order_id).referrer_addr == ref


def test_create_pending_store_409(make_store, acp_on):
    make_store(slug="acp-pend", pay_to="0x" + "a" * 40, status="pending_screening")
    assert client.post("/s/acp-pend/checkout_sessions", json={}).status_code == 409


# ------------------------------------------------------------- idempotency
def test_idempotency_key_replays_same_session(make_store, acp_on):
    make_store(slug="acp-idem", pay_to="0x" + "a" * 40)
    h = {"Idempotency-Key": "key-123"}
    a = client.post("/s/acp-idem/checkout_sessions", json={}, headers=h)
    b = client.post("/s/acp-idem/checkout_sessions", json={}, headers=h)
    assert a.status_code == 201
    assert b.json()["id"] == a.json()["id"]  # replay returns the original session
    with SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(AcpSession)) == 1


# ------------------------------------------------------------- retrieve/update
def test_retrieve_and_update_buyer(make_store, acp_on):
    make_store(slug="acp-ru", pay_to="0x" + "a" * 40)
    sid = client.post("/s/acp-ru/checkout_sessions", json={}).json()["id"]
    up = client.post(
        f"/s/acp-ru/checkout_sessions/{sid}",
        json={"buyer": {"email": "b@x.com"}},
    )
    assert up.status_code == 200
    assert up.json()["buyer"]["email"] == "b@x.com"
    got = client.get(f"/s/acp-ru/checkout_sessions/{sid}")
    assert got.status_code == 200
    assert got.json()["buyer"]["email"] == "b@x.com"


def test_retrieve_unknown_session_404(make_store, acp_on):
    make_store(slug="acp-404", pay_to="0x" + "a" * 40)
    assert client.get("/s/acp-404/checkout_sessions/acp_nope").status_code == 404


# ------------------------------------------------------------- complete
@respx.mock
def test_complete_with_tx_hash_delivers(make_store, acp_on):
    make_store(
        slug="acp-done",
        pay_to="0x" + "a" * 40,
        price_micro=9_000_000,
        delivery="SECRET",
    )
    created = client.post("/s/acp-done/checkout_sessions", json={}).json()
    sid = created["id"]
    pay_to = created["payment"]["fallback"]["pay_to"]
    amount_micro = created["payment"]["fallback"]["amount_micro"]
    tx = "0x" + secrets.token_hex(32)
    rpc = Rpc(receipts={tx: _receipt([_log(pay_to, amount_micro, tx)])})
    rpc.install()
    r = client.post(
        f"/s/acp-done/checkout_sessions/{sid}/complete",
        json={"payment_data": {"provider": "onchain_usdt0", "tx_hash": tx}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed"
    assert body["order"]["status"] == "paid"
    assert body["order"]["delivery"] == "SECRET"  # legacy text deliverable in-band


@respx.mock
def test_complete_wrong_amount_does_not_settle(make_store, acp_on):
    make_store(slug="acp-wrong", pay_to="0x" + "a" * 40, price_micro=9_000_000)
    created = client.post("/s/acp-wrong/checkout_sessions", json={}).json()
    sid = created["id"]
    pay_to = created["payment"]["fallback"]["pay_to"]
    amount_micro = created["payment"]["fallback"]["amount_micro"]
    tx = "0x" + secrets.token_hex(32)
    # transfers ONE micro short of the exact amount -> rejected, nothing recorded
    rpc = Rpc(receipts={tx: _receipt([_log(pay_to, amount_micro - 1, tx)])})
    rpc.install()
    r = client.post(
        f"/s/acp-wrong/checkout_sessions/{sid}/complete",
        json={"payment_data": {"provider": "onchain_usdt0", "tx_hash": tx}},
    )
    assert r.status_code == 400
    with SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(ProcessedTransfer)) == 0
        acp = s.get(AcpSession, sid)
        assert acp.status != "completed"
        assert s.get(Order, acp.order_id).status == "pending"


def test_complete_requires_tx_hash(make_store, acp_on):
    make_store(slug="acp-notx", pay_to="0x" + "a" * 40)
    sid = client.post("/s/acp-notx/checkout_sessions", json={}).json()["id"]
    r = client.post(
        f"/s/acp-notx/checkout_sessions/{sid}/complete",
        json={"payment_data": {"provider": "onchain_usdt0"}},
    )
    assert r.status_code == 422


# ------------------------------------------------------------- cancel/expire
def test_cancel_flips_order_and_session(make_store, acp_on):
    make_store(slug="acp-cancel", pay_to="0x" + "a" * 40)
    created = client.post("/s/acp-cancel/checkout_sessions", json={}).json()
    sid = created["id"]
    r = client.post(f"/s/acp-cancel/checkout_sessions/{sid}/cancel")
    assert r.status_code == 200
    assert r.json()["status"] == "canceled"
    with SessionLocal() as s:
        acp = s.get(AcpSession, sid)
        assert acp.status == "canceled"
        assert s.get(Order, acp.order_id).status == "canceled"


def test_expired_order_surfaces_canceled_session(make_store, acp_on):
    from datetime import datetime, timedelta, timezone

    make_store(slug="acp-exp", pay_to="0x" + "a" * 40)
    sid = client.post("/s/acp-exp/checkout_sessions", json={}).json()["id"]
    with SessionLocal() as s:
        acp = s.get(AcpSession, sid)
        o = s.get(Order, acp.order_id)
        o.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            minutes=1
        )
        s.commit()
    got = client.get(f"/s/acp-exp/checkout_sessions/{sid}")
    assert got.status_code == 200
    assert got.json()["status"] == "canceled"  # expired order -> canceled session


def test_complete_after_cancel_409(make_store, acp_on):
    make_store(slug="acp-cc", pay_to="0x" + "a" * 40)
    sid = client.post("/s/acp-cc/checkout_sessions", json={}).json()["id"]
    client.post(f"/s/acp-cc/checkout_sessions/{sid}/cancel")
    r = client.post(
        f"/s/acp-cc/checkout_sessions/{sid}/complete",
        json={
            "payment_data": {"provider": "onchain_usdt0", "tx_hash": "0x" + "1" * 64}
        },
    )
    assert r.status_code == 409

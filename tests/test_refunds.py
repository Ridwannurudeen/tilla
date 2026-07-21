"""M9 non-custodial refund tests. The refund tx receipt is respx-mocked httpx —
no real network, no funds. Covers the happy full/overage flips, entitlement
revocation, the M3-deferred underpaid resolution, every rejection class, tx-reuse
409, idempotent resubmit, and cross-merchant IDOR.
"""

import json
import secrets

import httpx
import respx
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main
from app import chain, checkout, config
from app.db import SessionLocal
from app.models import Deliverable, Entitlement, EventLog, Order

client = TestClient(main.app)

BUYER = "0x" + "b" * 40
REF1 = "0x" + "e1" * 32
REF2 = "0x" + "e2" * 32


def _auth(token):
    return {"Authorization": "Bearer " + token}


# ------------------------------------------------------------- on-chain mocking
def _log(from_addr, to_addr, value, tx_hash, log_index=0, block=100, contract=None):
    return {
        "address": contract or config.USDT0,
        "topics": [
            config.TRANSFER_TOPIC,
            chain.pad_address(from_addr),
            chain.pad_address(to_addr),
        ],
        "data": hex(value),
        "transactionHash": tx_hash,
        "logIndex": hex(log_index),
        "blockNumber": hex(block),
    }


def _receipt(logs, status="0x1", block=100):
    return {"status": status, "blockNumber": hex(block), "logs": logs}


class Rpc:
    def __init__(self, receipts=None):
        self.receipts = receipts or {}

    def handler(self, request):
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        if method == "eth_getTransactionReceipt":
            result = self.receipts.get(params[0].lower())
        else:
            result = None
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )

    def install(self):
        respx.post(config.RPC_URL).mock(side_effect=self.handler)


# ---------------------------------------------------------------- sign-in + seed
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


def _seed(slug, buyer=BUYER, delta=0, pay_block=100):
    """Create + drive an order via the real state machine. delta 0 → delivered,
    <0 → underpaid, >0 → overpaid (delivered with surplus)."""
    cid = client.post("/api/checkout/" + slug).json()["id"]
    with SessionLocal() as s:
        o = s.get(Order, cid)
        exp = o.expected_micro
        checkout.apply_transfer(
            s,
            o,
            exp + delta,
            tx_hash="0x" + secrets.token_hex(32),
            log_index=0,
            block_number=pay_block,
            from_addr=buyer,
            head=10**9,
        )
        s.commit()
    return cid, exp


def _add_text_deliverable(store_id, payload="THE-SECRET"):
    with SessionLocal() as s:
        s.add(Deliverable(store_id=store_id, kind="text", payload=payload, active=True))
        s.commit()


def _refund(cid, token, tx_hash, kind, rpc):
    rpc.install()
    return client.post(
        f"/api/merchant/orders/{cid}/refund",
        headers=_auth(token),
        json={"tx_hash": tx_hash, "kind": kind},
    )


# --------------------------------------------------------------------- happy path
@respx.mock
def test_full_refund_flips_to_refunded_and_revokes(make_store):
    acct = Account.create()
    sid = make_store(slug="ref-full", pay_to=acct.address.lower())
    _add_text_deliverable(sid)
    cid, exp = _seed("ref-full")  # delivered, entitlement issued
    token = _merchant_token(acct)

    rpc = Rpc(receipts={REF1: _receipt([_log(acct.address.lower(), BUYER, exp, REF1)])})
    r = _refund(cid, token, REF1, "full", rpc)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "refunded"
    assert r.json()["refunded_micro"] == exp

    with SessionLocal() as s:
        o = s.get(Order, cid)
        assert o.status == "refunded"
        ent = s.scalar(select(Entitlement).where(Entitlement.order_id == cid))
        assert ent is not None and ent.revoked_at is not None  # delivery revoked
        events = {
            e.event for e in s.scalars(select(EventLog).where(EventLog.order_id == cid))
        }
    assert "order.refunded" in events


@respx.mock
def test_underpaid_order_refunds_fully(make_store):
    """The M3-deferred resolution: a stuck underpaid order refunds fully."""
    acct = Account.create()
    make_store(slug="ref-under", pay_to=acct.address.lower())
    cid, exp = _seed("ref-under", delta=-1_000_000)  # underpaid
    with SessionLocal() as s:
        paid = s.get(Order, cid).paid_micro
        assert s.get(Order, cid).status == "underpaid"
    token = _merchant_token(acct)
    rpc = Rpc(
        receipts={REF1: _receipt([_log(acct.address.lower(), BUYER, paid, REF1)])}
    )
    r = _refund(cid, token, REF1, "full", rpc)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "refunded"
    assert r.json()["refunded_micro"] == paid


@respx.mock
def test_overage_refund_keeps_delivered_status(make_store):
    acct = Account.create()
    make_store(slug="ref-over", pay_to=acct.address.lower())
    cid, exp = _seed("ref-over", delta=2_000_000)  # overpaid → delivered, surplus 2
    token = _merchant_token(acct)
    rpc = Rpc(
        receipts={REF1: _receipt([_log(acct.address.lower(), BUYER, 2_000_000, REF1)])}
    )
    r = _refund(cid, token, REF1, "overage", rpc)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "delivered"  # buyer keeps the goods
    assert r.json()["refunded_micro"] == 2_000_000
    with SessionLocal() as s:
        assert s.get(Order, cid).status == "delivered"


# ---------------------------------------------------------------- rejection class
@respx.mock
def test_reject_wrong_recipient(make_store):
    acct = Account.create()
    make_store(slug="rej-to", pay_to=acct.address.lower())
    cid, exp = _seed("rej-to")
    token = _merchant_token(acct)
    other = "0x" + "c" * 40
    rpc = Rpc(receipts={REF1: _receipt([_log(acct.address.lower(), other, exp, REF1)])})
    r = _refund(cid, token, REF1, "full", rpc)
    assert r.status_code == 400


@respx.mock
def test_reject_wrong_sender(make_store):
    acct = Account.create()
    make_store(slug="rej-from", pay_to=acct.address.lower())
    cid, exp = _seed("rej-from")
    token = _merchant_token(acct)
    not_merchant = "0x" + "d" * 40
    rpc = Rpc(receipts={REF1: _receipt([_log(not_merchant, BUYER, exp, REF1)])})
    r = _refund(cid, token, REF1, "full", rpc)
    assert r.status_code == 400


@respx.mock
def test_reject_wrong_contract(make_store):
    acct = Account.create()
    make_store(slug="rej-contract", pay_to=acct.address.lower())
    cid, exp = _seed("rej-contract")
    token = _merchant_token(acct)
    fake = "0x" + "f" * 40
    rpc = Rpc(
        receipts={
            REF1: _receipt(
                [_log(acct.address.lower(), BUYER, exp, REF1, contract=fake)]
            )
        }
    )
    r = _refund(cid, token, REF1, "full", rpc)
    assert r.status_code == 400


@respx.mock
def test_reject_failed_tx(make_store):
    acct = Account.create()
    make_store(slug="rej-status", pay_to=acct.address.lower())
    cid, exp = _seed("rej-status")
    token = _merchant_token(acct)
    rpc = Rpc(
        receipts={
            REF1: _receipt([_log(acct.address.lower(), BUYER, exp, REF1)], status="0x0")
        }
    )
    r = _refund(cid, token, REF1, "full", rpc)
    assert r.status_code == 400


@respx.mock
def test_reject_non_exact_amount(make_store):
    acct = Account.create()
    make_store(slug="rej-amt", pay_to=acct.address.lower())
    cid, exp = _seed("rej-amt")
    token = _merchant_token(acct)
    rpc = Rpc(
        receipts={REF1: _receipt([_log(acct.address.lower(), BUYER, exp - 1, REF1)])}
    )
    r = _refund(cid, token, REF1, "full", rpc)
    assert r.status_code == 400


@respx.mock
def test_reject_refund_predating_payment(make_store):
    acct = Account.create()
    make_store(slug="rej-block", pay_to=acct.address.lower())
    cid, exp = _seed("rej-block", pay_block=500)
    token = _merchant_token(acct)
    # refund mined at block 100, below the payment block 500 → historical, rejected
    rpc = Rpc(
        receipts={
            REF1: _receipt([_log(acct.address.lower(), BUYER, exp, REF1, block=100)])
        }
    )
    r = _refund(cid, token, REF1, "full", rpc)
    assert r.status_code == 400


@respx.mock
def test_reject_tx_not_found(make_store):
    acct = Account.create()
    make_store(slug="rej-404", pay_to=acct.address.lower())
    cid, exp = _seed("rej-404")
    token = _merchant_token(acct)
    rpc = Rpc(receipts={})  # receipt None
    r = _refund(cid, token, REF1, "full", rpc)
    assert r.status_code == 400


def test_reject_unpaid_order_has_no_buyer(make_store):
    acct = Account.create()
    make_store(slug="rej-unpaid", pay_to=acct.address.lower())
    cid = client.post("/api/checkout/rej-unpaid").json()["id"]  # pending, no payment
    token = _merchant_token(acct)
    r = client.post(
        f"/api/merchant/orders/{cid}/refund",
        headers=_auth(token),
        json={"tx_hash": REF1, "kind": "full"},
    )
    assert r.status_code == 409


# ---------------------------------------------------------- idempotency + reuse
@respx.mock
def test_idempotent_resubmit_same_tx(make_store):
    acct = Account.create()
    make_store(slug="idem", pay_to=acct.address.lower())
    cid, exp = _seed("idem")
    token = _merchant_token(acct)
    rpc = Rpc(receipts={REF1: _receipt([_log(acct.address.lower(), BUYER, exp, REF1)])})
    first = _refund(cid, token, REF1, "full", rpc)
    assert first.status_code == 200 and first.json()["status"] == "refunded"
    # resubmit the same refund tx → no-op, current state, no double-count
    second = _refund(cid, token, REF1, "full", rpc)
    assert second.status_code == 200
    assert second.json()["status"] == "refunded"
    assert second.json()["refunded_micro"] == exp


@respx.mock
def test_same_tx_claimed_by_second_order_409(make_store):
    acct = Account.create()
    make_store(slug="reuse", pay_to=acct.address.lower())
    cid1, exp1 = _seed("reuse")  # both orders share buyer BUYER
    cid2, exp2 = _seed("reuse")
    token = _merchant_token(acct)
    rpc = Rpc(
        receipts={REF1: _receipt([_log(acct.address.lower(), BUYER, exp1, REF1)])}
    )
    r1 = _refund(cid1, token, REF1, "full", rpc)
    assert r1.status_code == 200
    # the SAME refund transfer cannot also credit order 2
    r2 = _refund(cid2, token, REF1, "full", rpc)
    assert r2.status_code == 409


@respx.mock
def test_already_fully_refunded_rejects_new_tx(make_store):
    acct = Account.create()
    make_store(slug="twice", pay_to=acct.address.lower())
    cid, exp = _seed("twice")
    token = _merchant_token(acct)
    rpc1 = Rpc(
        receipts={REF1: _receipt([_log(acct.address.lower(), BUYER, exp, REF1)])}
    )
    assert _refund(cid, token, REF1, "full", rpc1).status_code == 200
    # a DIFFERENT tx trying to refund an already-refunded order is rejected
    rpc2 = Rpc(
        receipts={REF2: _receipt([_log(acct.address.lower(), BUYER, exp, REF2)])}
    )
    r = _refund(cid, token, REF2, "full", rpc2)
    assert r.status_code == 409


# ---------------------------------------------------------------------- IDOR
@respx.mock
def test_non_owner_cannot_refund(make_store):
    a, b = Account.create(), Account.create()
    make_store(slug="own-a", pay_to=a.address.lower())
    cid, exp = _seed("own-a")
    tok_b = _merchant_token(b)
    rpc = Rpc(receipts={REF1: _receipt([_log(a.address.lower(), BUYER, exp, REF1)])})
    r = _refund(cid, tok_b, REF1, "full", rpc)
    assert r.status_code == 404  # not B's order → indistinguishable from missing

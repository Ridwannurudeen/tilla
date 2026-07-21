"""M13 affiliate tests: attribution capture (web body, agent query, MCP arg, ACP
object), the accrual ledger at the delivered/settled seam, the self-referral guard,
the refund void, and the verify-and-record payout. Every on-chain receipt is
respx-mocked httpx — no real network, no funds. The critical invariant is asserted
directly: NO signer, NO auto-payout — a payout is only ever a verify-and-record of a
transfer the operator already sent from their own wallet.
"""

import json
import pathlib
import secrets

import httpx
import respx
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from x402.http.utils import encode_payment_response_header
from x402.schemas import SettleResponse

import app.main as main
from app import affiliates, agentic, checkout, config
from app.db import SessionLocal
from app.models import AffiliateAccrual, AffiliatePayout, EventLog, Order, Store

client = TestClient(main.app)

REPO = pathlib.Path(__file__).resolve().parent.parent
REF = "0x" + "1" * 40
REF2 = "0x" + "2" * 40
BUYER = "0x" + "b" * 40
PAYER = "0x" + "3" * 40
NONCE = "0x" + "7" * 64
PAYOUT_TX = "0x" + "a1" * 32
PAYOUT_TX2 = "0x" + "a2" * 32


def _auth(token):
    return {"Authorization": "Bearer " + token}


# ------------------------------------------------------------- on-chain mocking
def _log(from_addr, to_addr, value, tx_hash, log_index=0, block=100, contract=None):
    return {
        "address": contract or config.USDT0,
        "topics": [
            config.TRANSFER_TOPIC,
            chain_pad(from_addr),
            chain_pad(to_addr),
        ],
        "data": hex(value),
        "transactionHash": tx_hash,
        "logIndex": hex(log_index),
        "blockNumber": hex(block),
    }


def chain_pad(addr):
    from app import chain

    return chain.pad_address(addr)


def _receipt(logs, status="0x1", block=100):
    return {"status": status, "blockNumber": hex(block), "logs": logs}


class Rpc:
    def __init__(self, receipts=None):
        self.receipts = receipts or {}

    def handler(self, request):
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        result = (
            self.receipts.get(params[0].lower())
            if method == "eth_getTransactionReceipt"
            else None
        )
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )

    def install(self):
        respx.post(config.RPC_URL).mock(side_effect=self.handler)


def _merchant_token(acct):
    r = client.post("/api/merchant/auth/nonce", json={"address": acct.address})
    sig = acct.sign_message(encode_defunct(text=r.json()["message"])).signature.hex()
    rv = client.post(
        "/api/merchant/auth/verify",
        json={"address": acct.address, "signature": sig},
    )
    assert rv.status_code == 200, rv.text
    return rv.json()["session_token"]


def _buyer_token(acct):
    r = client.post("/api/auth/nonce", json={"address": acct.address})
    sig = acct.sign_message(encode_defunct(text=r.json()["message"])).signature.hex()
    rv = client.post(
        "/api/auth/verify", json={"address": acct.address, "signature": sig}
    )
    assert rv.status_code == 200, rv.text
    return rv.json()["session_token"]


def _drive_delivered(cid, from_addr=BUYER, block=1):
    with SessionLocal() as s:
        o = s.get(Order, cid)
        exp = o.expected_micro
        checkout.apply_transfer(
            s,
            o,
            exp,
            tx_hash="0x" + secrets.token_hex(32),
            log_index=0,
            block_number=block,
            from_addr=from_addr,
            head=10**9,
        )
        s.commit()
    return exp


def _store_product(sid):
    with SessionLocal() as s:
        store = s.get(Store, sid)
        product = agentic._active_product(s, sid)
        s.expunge_all()
        return store, product


def _accruals(cid):
    with SessionLocal() as s:
        return s.scalars(
            select(AffiliateAccrual).where(AffiliateAccrual.order_id == cid)
        ).all()


def _events(cid):
    with SessionLocal() as s:
        return {
            e.event for e in s.scalars(select(EventLog).where(EventLog.order_id == cid))
        }


# ---------------------------------------------------------------- capture
def test_web_checkout_captures_ref(make_store):
    make_store(slug="aff-cap", pay_to="0x" + "a" * 40)
    cid = client.post("/api/checkout/aff-cap", json={"ref": REF}).json()["id"]
    with SessionLocal() as s:
        assert s.get(Order, cid).referrer_addr == REF


def test_web_checkout_ref_normalizes_case(make_store):
    make_store(slug="aff-case", pay_to="0x" + "a" * 40)
    mixed = "0x" + "AbCd" * 10  # 40 hex chars, mixed case
    cid = client.post("/api/checkout/aff-case", json={"ref": mixed}).json()["id"]
    with SessionLocal() as s:
        assert s.get(Order, cid).referrer_addr == mixed.lower()


def test_web_checkout_no_ref_is_backward_compatible(make_store):
    make_store(slug="aff-none")
    # no body at all — the pre-M13 call — still 200 with no attribution
    r = client.post("/api/checkout/aff-none")
    assert r.status_code == 200
    with SessionLocal() as s:
        assert s.get(Order, r.json()["id"]).referrer_addr is None


def test_web_checkout_rejects_malformed_ref(make_store):
    make_store(slug="aff-bad")
    assert (
        client.post("/api/checkout/aff-bad", json={"ref": "0xnothex"}).status_code
        == 422
    )
    assert (
        client.post("/api/checkout/aff-bad", json={"ref": "not-an-addr"}).status_code
        == 422
    )


def test_web_checkout_rejects_zero_address_ref(make_store):
    make_store(slug="aff-zero")
    r = client.post("/api/checkout/aff-zero", json={"ref": "0x" + "0" * 40})
    assert r.status_code == 422


def test_ref_is_immutable_after_creation(make_store):
    make_store(slug="aff-immut", pay_to="0x" + "a" * 40)
    cid = client.post("/api/checkout/aff-immut", json={"ref": REF}).json()["id"]
    # polling the order never mutates the captured ref
    client.get(f"/api/checkout/{cid}")
    with SessionLocal() as s:
        assert s.get(Order, cid).referrer_addr == REF


# ---------------------------------------------------------------- accrual
def test_delivered_web_order_accrues_once(make_store):
    make_store(slug="aff-acc", pay_to="0x" + "a" * 40, price_micro=9_000_000)
    cid = client.post("/api/checkout/aff-acc", json={"ref": REF}).json()["id"]
    exp = _drive_delivered(cid)
    rows = _accruals(cid)
    assert len(rows) == 1
    a = rows[0]
    assert a.referrer_addr == REF
    assert a.status == "accrued"
    assert a.rate_bps == config.TILLA_AFFILIATE_BPS
    assert a.basis_micro == exp
    assert a.accrued_micro == exp * config.TILLA_AFFILIATE_BPS // 10000
    assert "affiliate.accrued" in _events(cid)


def test_unreferred_order_never_accrues(make_store):
    make_store(slug="aff-unref", pay_to="0x" + "a" * 40)
    cid = client.post("/api/checkout/aff-unref").json()["id"]
    _drive_delivered(cid)
    assert len(_accruals(cid)) == 0


def test_self_referral_buyer_rejected(make_store):
    make_store(slug="aff-selfb", pay_to="0x" + "a" * 40)
    cid = client.post("/api/checkout/aff-selfb", json={"ref": BUYER}).json()["id"]
    _drive_delivered(cid, from_addr=BUYER)
    assert len(_accruals(cid)) == 0
    assert "affiliate.self_referral_rejected" in _events(cid)


def test_self_referral_merchant_rejected(make_store):
    pay_to = "0x" + "a" * 40
    make_store(slug="aff-selfm", pay_to=pay_to)
    cid = client.post("/api/checkout/aff-selfm", json={"ref": pay_to}).json()["id"]
    _drive_delivered(cid, from_addr=BUYER)
    assert len(_accruals(cid)) == 0
    assert "affiliate.self_referral_rejected" in _events(cid)


def test_agent_order_accrues_only_on_settlement(make_store):
    sid = make_store(slug="aff-agent", pay_to="0x" + "a" * 40, price_micro=1_000_000)
    store, product = _store_product(sid)
    with SessionLocal() as s:
        order, _ = agentic.fulfill_agent_order(
            s, s.merge(store), s.merge(product), PAYER, NONCE, REF
        )
        s.commit()
        oid = order.id
    # provisional 'settling' — nothing accrued yet
    assert len(_accruals(oid)) == 0
    header = encode_payment_response_header(
        SettleResponse(success=True, transaction="0x" + "e" * 64, network="eip155:196")
    )
    agentic.record_settlement(oid, header)
    rows = _accruals(oid)
    assert len(rows) == 1
    assert rows[0].referrer_addr == REF


def test_voided_agent_order_never_accrues(make_store):
    sid = make_store(slug="aff-void", pay_to="0x" + "a" * 40, price_micro=1_000_000)
    store, product = _store_product(sid)
    with SessionLocal() as s:
        order, _ = agentic.fulfill_agent_order(
            s, s.merge(store), s.merge(product), PAYER, NONCE, REF
        )
        s.commit()
        oid = order.id
    # settle FAILS -> order voided; the settling->delivered flip never runs
    agentic.settle_failed_core(NONCE)
    with SessionLocal() as s:
        assert s.get(Order, oid).status == "canceled"
    assert len(_accruals(oid)) == 0


# ---------------------------------------------------------------- refund void
@respx.mock
def test_full_refund_voids_accrual(make_store):
    acct = Account.create()
    make_store(slug="aff-ref", pay_to=acct.address.lower(), price_micro=9_000_000)
    cid = client.post("/api/checkout/aff-ref", json={"ref": REF}).json()["id"]
    exp = _drive_delivered(cid)
    assert _accruals(cid)[0].status == "accrued"
    token = _merchant_token(acct)
    tx = "0x" + "c1" * 32
    rpc = Rpc(receipts={tx: _receipt([_log(acct.address.lower(), BUYER, exp, tx)])})
    rpc.install()
    r = client.post(
        f"/api/merchant/orders/{cid}/refund",
        headers=_auth(token),
        json={"tx_hash": tx, "kind": "full"},
    )
    assert r.status_code == 200, r.text
    assert _accruals(cid)[0].status == "void"


# ---------------------------------------------------------------- payout
@respx.mock
def test_payout_verify_and_record_marks_paid(make_store):
    acct = Account.create()
    make_store(slug="aff-pay", pay_to=acct.address.lower(), price_micro=9_000_000)
    cid = client.post("/api/checkout/aff-pay", json={"ref": REF}).json()["id"]
    _drive_delivered(cid)
    owed = _accruals(cid)[0].accrued_micro
    token = _merchant_token(acct)
    # the operator sends USDT0 to REF from their own wallet; we verify+record it
    rpc = Rpc(
        receipts={
            PAYOUT_TX: _receipt([_log(acct.address.lower(), REF, owed, PAYOUT_TX)])
        }
    )
    rpc.install()
    r = client.post(
        f"/api/merchant/affiliates/{REF}/payout",
        headers=_auth(token),
        json={"tx_hash": PAYOUT_TX},
    )
    assert r.status_code == 200, r.text
    assert r.json()["covered_micro"] == owed
    assert r.json()["owed_after_micro"] == 0
    with SessionLocal() as s:
        acc = s.scalars(
            select(AffiliateAccrual).where(AffiliateAccrual.order_id == cid)
        ).one()
        assert acc.status == "paid"
        assert acc.payout_id is not None
        assert s.scalar(select(func.count()).select_from(AffiliatePayout)) == 1


@respx.mock
def test_payout_duplicate_tx_409(make_store):
    acct = Account.create()
    make_store(slug="aff-dup", pay_to=acct.address.lower(), price_micro=9_000_000)
    cid = client.post("/api/checkout/aff-dup", json={"ref": REF}).json()["id"]
    _drive_delivered(cid)
    owed = _accruals(cid)[0].accrued_micro
    token = _merchant_token(acct)
    rpc = Rpc(
        receipts={
            PAYOUT_TX: _receipt([_log(acct.address.lower(), REF, owed, PAYOUT_TX)])
        }
    )
    rpc.install()
    assert (
        client.post(
            f"/api/merchant/affiliates/{REF}/payout",
            headers=_auth(token),
            json={"tx_hash": PAYOUT_TX},
        ).status_code
        == 200
    )
    # nothing owed anymore -> 409
    r2 = client.post(
        f"/api/merchant/affiliates/{REF}/payout",
        headers=_auth(token),
        json={"tx_hash": PAYOUT_TX},
    )
    assert r2.status_code == 409


@respx.mock
def test_payout_wrong_recipient_rejected(make_store):
    acct = Account.create()
    make_store(slug="aff-wrong", pay_to=acct.address.lower(), price_micro=9_000_000)
    cid = client.post("/api/checkout/aff-wrong", json={"ref": REF}).json()["id"]
    owed = _drive_delivered(cid) * config.TILLA_AFFILIATE_BPS // 10000
    token = _merchant_token(acct)
    # transfer goes to REF2, not REF -> no matching transfer
    rpc = Rpc(
        receipts={
            PAYOUT_TX: _receipt([_log(acct.address.lower(), REF2, owed, PAYOUT_TX)])
        }
    )
    rpc.install()
    r = client.post(
        f"/api/merchant/affiliates/{REF}/payout",
        headers=_auth(token),
        json={"tx_hash": PAYOUT_TX},
    )
    assert r.status_code == 400


@respx.mock
def test_payout_exceeding_owed_rejected(make_store):
    acct = Account.create()
    make_store(slug="aff-over", pay_to=acct.address.lower(), price_micro=9_000_000)
    cid = client.post("/api/checkout/aff-over", json={"ref": REF}).json()["id"]
    owed = _accruals_after(cid)
    token = _merchant_token(acct)
    rpc = Rpc(
        receipts={
            PAYOUT_TX: _receipt([_log(acct.address.lower(), REF, owed + 1, PAYOUT_TX)])
        }
    )
    rpc.install()
    r = client.post(
        f"/api/merchant/affiliates/{REF}/payout",
        headers=_auth(token),
        json={"tx_hash": PAYOUT_TX},
    )
    assert r.status_code == 400


def _accruals_after(cid):
    _drive_delivered(cid)
    return _accruals(cid)[0].accrued_micro


# ------------------------------------------------------------ read surfaces
def test_merchant_affiliates_read_surface(make_store):
    acct = Account.create()
    make_store(slug="aff-read", pay_to=acct.address.lower(), price_micro=9_000_000)
    cid = client.post("/api/checkout/aff-read", json={"ref": REF}).json()["id"]
    _drive_delivered(cid)
    token = _merchant_token(acct)
    r = client.get("/api/merchant/affiliates", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["owed_micro"] > 0
    assert len(body["referrers"]) == 1
    assert body["referrers"][0]["referrer"] == REF
    assert len(body["orders"]) == 1


def test_merchant_summary_has_affiliate_owed(make_store):
    acct = Account.create()
    make_store(slug="aff-sum", pay_to=acct.address.lower(), price_micro=9_000_000)
    cid = client.post("/api/checkout/aff-sum", json={"ref": REF}).json()["id"]
    _drive_delivered(cid)
    token = _merchant_token(acct)
    r = client.get("/api/merchant/summary", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["affiliate_owed_micro"] > 0


def test_referrer_summary_is_wallet_gated(make_store):
    ref_acct = Account.create()
    make_store(slug="aff-refsum", pay_to="0x" + "a" * 40, price_micro=9_000_000)
    cid = client.post(
        "/api/checkout/aff-refsum", json={"ref": ref_acct.address.lower()}
    ).json()["id"]
    _drive_delivered(cid, from_addr=BUYER)
    # no token -> 401
    assert client.get("/api/affiliate/summary").status_code == 401
    token = _buyer_token(ref_acct)  # the referrer signs as themselves
    r = client.get("/api/affiliate/summary", headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["totals"]["accrued_micro"] > 0
    assert r.json()["referrer"] == ref_acct.address.lower()


def test_referrer_summary_isolates_other_wallets(make_store):
    make_store(slug="aff-iso", pay_to="0x" + "a" * 40, price_micro=9_000_000)
    cid = client.post("/api/checkout/aff-iso", json={"ref": REF}).json()["id"]
    _drive_delivered(cid, from_addr=BUYER)
    other = Account.create()  # a wallet that referred nothing
    token = _buyer_token(other)
    r = client.get("/api/affiliate/summary", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["totals"]["accrued_micro"] == 0
    assert r.json()["stores"] == []


# ---------------------------------------------------------- NO signer / NO payout
def test_affiliates_module_has_no_signer():
    """Grep-level guarantee: the affiliate module constructs no signer and moves no
    funds — a payout is always a verify-and-record of an out-of-band transfer."""
    src = (REPO / "app" / "affiliates.py").read_text(encoding="utf-8")
    assert "eth_account" not in src
    assert "send_raw_transaction" not in src
    assert "sign_transaction" not in src.lower()
    assert "private_key" not in src.lower()


def test_normalize_ref_unit():
    assert affiliates.normalize_ref(None) is None
    assert affiliates.normalize_ref("") is None
    assert affiliates.normalize_ref("0x" + "AB" * 20) == "0x" + "ab" * 20
    for bad in ["0x123", "not-hex", "0x" + "0" * 40, "0x" + "g" * 40]:
        try:
            affiliates.normalize_ref(bad)
            raise AssertionError(f"expected reject for {bad}")
        except affiliates.RefRejected:
            pass

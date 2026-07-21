"""M3 hardened-checkout tests: unique amounts, exact-amount matching, the
sweeper, the txhash fast path, expiry/quarantine, late pay, idempotency and the
delivery race. All RPC is respx-mocked httpx — no real network.
"""

import json
import threading
from datetime import timedelta

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.main as main
from app import chain, checkout, config
from app.db import SessionLocal
from app.models import ChainCursor, Delivery, EventLog, Order, ProcessedTransfer

client = TestClient(main.app)

TX1 = "0x" + "1" * 64
TX2 = "0x" + "2" * 64
TX3 = "0x" + "3" * 64
FROM = "0x" + "7" * 40


def _log(to_addr, value, tx_hash, log_index, block, from_addr=FROM):
    return {
        "address": config.USDT0,
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


def _receipt(logs, block, status="0x1"):
    return {"status": status, "blockNumber": hex(block), "logs": logs}


class Rpc:
    """A tiny X Layer JSON-RPC stand-in driven off respx-mocked httpx."""

    def __init__(self, head=0, logs=None, receipts=None):
        self.head = head
        self.logs = logs or []
        self.receipts = receipts or {}
        self.getlogs_calls = []

    def handler(self, request):
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        if method == "eth_blockNumber":
            result = hex(self.head)
        elif method == "eth_getLogs":
            frm = int(params[0]["fromBlock"], 16)
            to = int(params[0]["toBlock"], 16)
            self.getlogs_calls.append((frm, to))
            tos = {a.lower() for a in params[0]["topics"][2]}
            result = [
                lg
                for lg in self.logs
                if frm <= int(lg["blockNumber"], 16) <= to
                and lg["topics"][2].lower() in tos
            ]
        elif method == "eth_getTransactionReceipt":
            result = self.receipts.get(params[0].lower())
        else:
            result = None
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )

    def install(self):
        respx.post(config.RPC_URL).mock(side_effect=self.handler)


def _mk(make_store, slug, pay_to, price=9_000_000, delivery="DELIV"):
    make_store(slug=slug, pay_to=pay_to, price_micro=price, delivery=delivery)


def _order(slug):
    """Create an order via the API; return (cid, expected_micro)."""
    cid = client.post(f"/api/checkout/{slug}").json()["id"]
    with SessionLocal() as s:
        return cid, s.get(Order, cid).expected_micro


def _seed_cursor(last_block):
    with SessionLocal() as s:
        s.merge(ChainCursor(id=1, last_block=last_block, updated_at=checkout._now()))
        s.commit()


def _status(cid):
    return client.get(f"/api/checkout/{cid}").json()["status"]


# ---------------------------------------------------------- unique amounts
def test_concurrent_buyers_get_distinct_amounts(make_store):
    _mk(make_store, "shop", "0x" + "a" * 40)
    amounts = {_order("shop")[1] for _ in range(8)}
    assert len(amounts) == 8  # every concurrent checkout drew a distinct amount


def test_offset_space_exhaustion_returns_503(make_store, monkeypatch):
    _mk(make_store, "tiny", "0x" + "b" * 40)
    monkeypatch.setattr(config, "AMOUNT_OFFSET_MIN", 1)
    monkeypatch.setattr(config, "AMOUNT_OFFSET_MAX", 2)
    codes = [client.post("/api/checkout/tiny").status_code for _ in range(3)]
    assert codes.count(200) == 2  # only two offsets exist
    assert 503 in codes  # the third can't find a free amount


def test_integrityerror_duplicate_insert_redraws(make_store, monkeypatch):
    # Force the check-then-insert race: _amount_taken blind, same offset drawn
    # twice, then a fresh one. The partial unique index raises IntegrityError on
    # the dupe; create_order must roll back and redraw.
    _mk(make_store, "race-alloc", "0x" + "c" * 40, price=1_000_000)
    cid0, e0 = _order("race-alloc")
    dup_offset = e0 - 1_000_000
    monkeypatch.setattr(checkout, "_amount_taken", lambda *a, **k: False)
    offsets = iter([dup_offset, dup_offset, dup_offset + 1000])
    monkeypatch.setattr(checkout.random, "randint", lambda a, b: next(offsets))
    with SessionLocal() as s:
        store = s.get(Order, cid0)
        from app.models import Product, Store

        st = s.get(Store, store.store_id)
        prod = s.scalar(select(Product).where(Product.store_id == st.id))
        order = checkout.create_order(s, st, prod)
        s.commit()
        assert order.expected_micro == 1_000_000 + dup_offset + 1000


# ------------------------------------------------------- sweeper matching
@respx.mock
def test_sweeper_confirms_only_exact_match_order(make_store):
    _mk(make_store, "sw", "0x" + "d" * 40, delivery="SWEEP-OK")
    pay_to = "0x" + "d" * 40
    cid_a, exp_a = _order("sw")
    cid_b, exp_b = _order("sw")
    assert exp_a != exp_b

    # window carries B's exact amount + one unrelated/dust credit
    rpc = Rpc(
        head=200,
        logs=[
            _log(pay_to, exp_b, TX2, 0, 150),
            _log(pay_to, 12345, TX3, 1, 151),  # matches nothing
        ],
    )
    rpc.install()
    _seed_cursor(100)
    checkout.sweep_tick()

    assert _status(cid_b) == "paid"  # exact match confirmed + delivered
    assert _status(cid_a) == "pending"  # untouched
    with SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(ProcessedTransfer)) == 1
        unmatched = s.scalars(
            select(EventLog).where(EventLog.event == "transfer.unmatched")
        ).all()
        assert len(unmatched) == 1
        assert client.get(f"/api/checkout/{cid_b}").json()["delivery"] == "SWEEP-OK"


@respx.mock
def test_sweeper_replay_is_idempotent(make_store):
    _mk(make_store, "rep", "0x" + "e" * 40)
    pay_to = "0x" + "e" * 40
    cid, exp = _order("rep")
    rpc = Rpc(head=200, logs=[_log(pay_to, exp, TX1, 0, 150)])
    rpc.install()

    _seed_cursor(100)
    checkout.sweep_tick()
    _seed_cursor(100)  # rewind: same window swept again
    checkout.sweep_tick()

    assert _status(cid) == "paid"
    with SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(ProcessedTransfer)) == 1
        n = s.scalar(
            select(func.count()).select_from(Delivery).where(Delivery.order_id == cid)
        )
        assert n == 1


@respx.mock
def test_sweeper_windows_never_exceed_101_blocks(make_store):
    _mk(make_store, "win", "0x" + "1" * 40)
    _order("win")  # one active address so getLogs is actually called
    rpc = Rpc(head=500, logs=[])
    rpc.install()

    _seed_cursor(0)
    checkout.sweep_tick()

    assert rpc.getlogs_calls, "expected at least one getLogs window"
    for frm, to in rpc.getlogs_calls:
        assert to - frm + 1 <= 101  # inclusive span cap
    with SessionLocal() as s:
        assert s.get(ChainCursor, 1).last_block == 497  # 500 - CONFIRMATIONS

    before = len(rpc.getlogs_calls)
    checkout.sweep_tick()  # nothing new -> no further getLogs
    assert len(rpc.getlogs_calls) == before


# ------------------------------------------------------- txhash fast path
@respx.mock
def test_underpay_then_topup_then_confirmed(make_store):
    _mk(make_store, "up", "0x" + "2" * 40, delivery="UP-DELIV")
    pay_to = "0x" + "2" * 40
    cid, exp = _order("up")
    partial = exp - 1_000
    rpc = Rpc(
        head=200,
        receipts={
            TX1: _receipt([_log(pay_to, partial, TX1, 0, 100)], 100),
            TX2: _receipt([_log(pay_to, 1_000, TX2, 0, 101)], 101),
        },
    )
    rpc.install()

    r1 = client.post(f"/api/checkout/{cid}/tx", json={"tx_hash": TX1})
    assert r1.status_code == 200 and r1.json()["status"] == "underpaid"
    # resubmitting the same tx must not double-count
    client.post(f"/api/checkout/{cid}/tx", json={"tx_hash": TX1})
    with SessionLocal() as s:
        assert s.get(Order, cid).paid_micro == partial

    r2 = client.post(f"/api/checkout/{cid}/tx", json={"tx_hash": TX2})
    assert r2.status_code == 200 and r2.json()["status"] == "paid"
    assert r2.json()["delivery"] == "UP-DELIV"


@respx.mock
def test_overpay_records_overage_and_delivers(make_store):
    _mk(make_store, "ov", "0x" + "3" * 40)
    pay_to = "0x" + "3" * 40
    cid, exp = _order("ov")
    rpc = Rpc(
        head=200,
        receipts={TX1: _receipt([_log(pay_to, exp + 5_000, TX1, 0, 100)], 100)},
    )
    rpc.install()

    r = client.post(f"/api/checkout/{cid}/tx", json={"tx_hash": TX1})
    assert r.status_code == 200 and r.json()["status"] == "paid"
    with SessionLocal() as s:
        o = s.get(Order, cid)
        assert o.overpaid_micro == 5_000
        assert o.status == "delivered"


@respx.mock
def test_txhash_detected_below_depth_then_confirms(make_store):
    _mk(make_store, "dep", "0x" + "4" * 40)
    pay_to = "0x" + "4" * 40
    cid, exp = _order("dep")
    rpc = Rpc(head=100, receipts={TX1: _receipt([_log(pay_to, exp, TX1, 0, 99)], 99)})
    rpc.install()

    r = client.post(f"/api/checkout/{cid}/tx", json={"tx_hash": TX1})
    assert r.json()["status"] == "detected"  # depth 1 < CONFIRMATIONS

    rpc.head = 200  # chain advances past the confirmation window
    assert _status(cid) == "paid"  # GET poll matures detected -> confirmed -> delivered


@respx.mock
def test_txhash_rejections(make_store):
    _mk(make_store, "rej", "0x" + "5" * 40)
    pay_to = "0x" + "5" * 40
    other = "0x" + "6" * 40

    # malformed hash -> 422 (never reaches the chain)
    assert client.post(
        "/api/checkout/x/tx", json={"tx_hash": "0xdeadbeef"}
    ).status_code in (
        404,
        422,
    )

    cid, exp = _order("rej")
    # wrong contract address
    bad_contract = _receipt([{**_log(pay_to, exp, TX1, 0, 100), "address": other}], 100)
    # transfer to a different recipient
    wrong_to = _receipt([_log(other, exp, TX2, 0, 100)], 100)
    # reverted tx
    reverted = _receipt([_log(pay_to, exp, TX3, 0, 100)], 100, status="0x0")
    rpc = Rpc(head=200, receipts={TX1: bad_contract, TX2: wrong_to, TX3: reverted})
    rpc.install()

    assert (
        client.post(f"/api/checkout/{cid}/tx", json={"tx_hash": TX1}).status_code == 400
    )
    assert (
        client.post(f"/api/checkout/{cid}/tx", json={"tx_hash": TX2}).status_code == 400
    )
    assert (
        client.post(f"/api/checkout/{cid}/tx", json={"tx_hash": TX3}).status_code == 400
    )
    assert _status(cid) == "pending"  # nothing confirmed it


@respx.mock
def test_same_tx_for_second_order_is_409(make_store):
    _mk(make_store, "used", "0x" + "8" * 40)
    pay_to = "0x" + "8" * 40
    cid_a, exp_a = _order("used")
    cid_b, exp_b = _order("used")
    rpc = Rpc(
        head=200, receipts={TX1: _receipt([_log(pay_to, exp_a, TX1, 0, 100)], 100)}
    )
    rpc.install()

    assert (
        client.post(f"/api/checkout/{cid_a}/tx", json={"tx_hash": TX1}).status_code
        == 200
    )
    # same on-chain transfer submitted for a different order
    assert (
        client.post(f"/api/checkout/{cid_b}/tx", json={"tx_hash": TX1}).status_code
        == 409
    )


# --------------------------------------------------- expiry / quarantine
def test_expiry_flip_via_poll(make_store):
    _mk(make_store, "exp", "0x" + "9" * 40)
    cid, exp = _order("exp")
    with SessionLocal() as s:
        o = s.get(Order, cid)
        o.expires_at = checkout._now() - timedelta(minutes=1)
        s.commit()
    assert _status(cid) == "expired"


def test_expired_amount_quarantined_then_released(make_store):
    _mk(make_store, "qr", "0x" + "a" * 39 + "b")
    pay_to = "0x" + "a" * 39 + "b"
    cid, exp = _order("qr")
    with SessionLocal() as s:
        o = s.get(Order, cid)
        o.status = "expired"
        o.expires_at = checkout._now() - timedelta(minutes=1)
        s.commit()
    with SessionLocal() as s:
        now = checkout._now()
        # inside the cooldown: the amount is refused (late tx can't be reassigned)
        assert checkout._amount_taken(s, pay_to, exp, now) is True
        # past the cooldown: the allocation query stops excluding it
        later = now + timedelta(hours=config.QUARANTINE_HOURS, minutes=5)
        assert checkout._amount_taken(s, pay_to, exp, later) is False


@respx.mock
def test_late_payment_honored_within_quarantine(make_store):
    _mk(make_store, "late", "0x" + "c" * 39 + "d", delivery="LATE-OK")
    pay_to = "0x" + "c" * 39 + "d"
    cid, exp = _order("late")
    with SessionLocal() as s:
        o = s.get(Order, cid)
        o.status = "expired"
        o.expires_at = checkout._now() - timedelta(minutes=1)  # quarantined
        s.commit()

    rpc = Rpc(head=200, logs=[_log(pay_to, exp, TX1, 0, 150)])
    rpc.install()
    _seed_cursor(100)
    checkout.sweep_tick()

    with SessionLocal() as s:
        o = s.get(Order, cid)
        assert o.status == "delivered"  # expired -> late_paid -> delivered
    assert client.get(f"/api/checkout/{cid}").json()["delivery"] == "LATE-OK"


# ------------------------------------------------------ delivery race fix
def test_delivery_race_yields_single_delivery(make_store):
    _mk(make_store, "drace", "0x" + "e" * 39 + "f", delivery="RACE")
    cid, exp = _order("drace")
    with SessionLocal() as s:
        assert checkout.transition(s, cid, ("pending",), "confirmed", paid_micro=exp)
        s.commit()

    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def worker():
        try:
            barrier.wait()
            with SessionLocal() as s:
                checkout.deliver(s, s.get(Order, cid))
                s.commit()
        except Exception as exc:  # noqa: BLE001 — the test asserts there are none
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []  # no UNIQUE(order_id) IntegrityError 500 on the loser
    with SessionLocal() as s:
        n = s.scalar(
            select(func.count()).select_from(Delivery).where(Delivery.order_id == cid)
        )
        assert n == 1
        assert s.get(Order, cid).status == "delivered"


# --------------------------------------------------------- legacy 'paid'
def test_legacy_paid_order_returns_delivery(make_store):
    _mk(make_store, "legacy", "0x" + "b" * 39 + "c", delivery="LEGACY-DELIV")
    cid, exp = _order("legacy")
    with SessionLocal() as s:
        o = s.get(Order, cid)
        o.status = "paid"  # pre-M3 terminal alias, never rewritten
        s.add(Delivery(order_id=cid, kind="text", payload="LEGACY-DELIV"))
        s.commit()
    body = client.get(f"/api/checkout/{cid}").json()
    assert body["status"] == "paid"
    assert body["delivery"] == "LEGACY-DELIV"


@pytest.mark.parametrize("bad", ["", "0x123", "nothex", "0x" + "z" * 64])
def test_tx_hash_validation_422(make_store, bad):
    _mk(make_store, "val", "0x" + "d" * 39 + "e")
    cid, _ = _order("val")
    assert (
        client.post(f"/api/checkout/{cid}/tx", json={"tx_hash": bad}).status_code == 422
    )

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
from app import chain, checkout, config, delivery, payment
from app.db import SessionLocal
from app.models import (
    ChainCursor,
    Deliverable,
    Delivery,
    Entitlement,
    EventLog,
    Order,
    ProcessedTransfer,
)

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
    # Per-chain cursor keyed by chain_id (18.2): the canonical X Layer ledger is 196.
    with SessionLocal() as s:
        s.merge(
            ChainCursor(
                id=payment.CANONICAL_CHAIN.chain_id,
                last_block=last_block,
                updated_at=checkout._now(),
            )
        )
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
        assert (
            s.get(ChainCursor, payment.CANONICAL_CHAIN.chain_id).last_block == 497
        )  # 500 - CONFIRMATIONS

    before = len(rpc.getlogs_calls)
    checkout.sweep_tick()  # nothing new -> no further getLogs
    assert len(rpc.getlogs_calls) == before


# ------------------------------------------------------- txhash fast path
@respx.mock
def test_partial_txhash_rejected_and_leaves_transfer_claimable(make_store):
    # The buyer path is exact-only: a partial credits nothing, records no transfer
    # (so its true owner order can still claim it), and does not move the order. A
    # later exact payment confirms.
    _mk(make_store, "up", "0x" + "2" * 40, delivery="UP-DELIV")
    pay_to = "0x" + "2" * 40
    cid, exp = _order("up")
    partial = exp - 1_000
    rpc = Rpc(
        head=200,
        receipts={
            TX1: _receipt([_log(pay_to, partial, TX1, 0, 100)], 100),
            TX2: _receipt([_log(pay_to, exp, TX2, 0, 101)], 101),
        },
    )
    rpc.install()

    r1 = client.post(f"/api/checkout/{cid}/tx", json={"tx_hash": TX1})
    assert r1.status_code == 400  # partial: not the exact total due
    with SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(ProcessedTransfer)) == 0
        o = s.get(Order, cid)
        assert o.status == "pending" and o.paid_micro == 0

    r2 = client.post(f"/api/checkout/{cid}/tx", json={"tx_hash": TX2})
    assert r2.status_code == 200 and r2.json()["status"] == "paid"
    assert r2.json()["delivery"] == "UP-DELIV"


@respx.mock
def test_overpay_rejected_on_fast_path(make_store):
    # A non-exact total (here an overpay) is not credited on the buyer path — it
    # mirrors the sweeper, which never matches a value != expected_micro. The
    # transfer is left unrecorded for support/sweeper handling.
    _mk(make_store, "ov", "0x" + "3" * 40)
    pay_to = "0x" + "3" * 40
    cid, exp = _order("ov")
    rpc = Rpc(
        head=200,
        receipts={TX1: _receipt([_log(pay_to, exp + 5_000, TX1, 0, 100)], 100)},
    )
    rpc.install()

    r = client.post(f"/api/checkout/{cid}/tx", json={"tx_hash": TX1})
    assert r.status_code == 400
    with SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(ProcessedTransfer)) == 0
        assert s.get(Order, cid).status == "pending"


@respx.mock
def test_txhash_below_created_block_is_rejected(make_store):
    # A transfer mined before the order's creation floor is historical, never this
    # order's payment — the fast path must refuse it without recording it.
    _mk(make_store, "floor", "0x" + "2" * 39 + "a")
    pay_to = "0x" + "2" * 39 + "a"
    cid, exp = _order("floor")
    with SessionLocal() as s:
        s.get(Order, cid).created_block = 500  # order created at chain head 500
        s.commit()
    rpc = Rpc(
        head=600,
        receipts={
            TX1: _receipt([_log(pay_to, exp, TX1, 0, 100)], 100),  # below the floor
            TX2: _receipt([_log(pay_to, exp, TX2, 0, 500)], 500),  # at the floor
        },
    )
    rpc.install()

    r1 = client.post(f"/api/checkout/{cid}/tx", json={"tx_hash": TX1})
    assert r1.status_code == 400
    with SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(ProcessedTransfer)) == 0
        assert s.get(Order, cid).status == "pending"

    r2 = client.post(f"/api/checkout/{cid}/tx", json={"tx_hash": TX2})
    assert r2.status_code == 200 and r2.json()["status"] == "paid"


@respx.mock
def test_fast_path_reorg_marks_reorged_not_delivered(make_store):
    # A depth-0 fast-path order must re-verify its receipt at maturation; a tx that
    # reorged to a different block is parked 'reorged', never delivered by block
    # arithmetic alone.
    _mk(make_store, "reorg", "0x" + "3" * 39 + "a")
    pay_to = "0x" + "3" * 39 + "a"
    cid, exp = _order("reorg")
    rpc = Rpc(head=100, receipts={TX1: _receipt([_log(pay_to, exp, TX1, 0, 99)], 99)})
    rpc.install()

    r = client.post(f"/api/checkout/{cid}/tx", json={"tx_hash": TX1})
    assert r.json()["status"] == "detected"  # depth 1 < CONFIRMATIONS

    rpc.head = 200  # past the confirmation window, but the tx re-mined elsewhere
    rpc.receipts[TX1] = _receipt([_log(pay_to, exp, TX1, 0, 105)], 105)
    assert _status(cid) == "reorged"
    with SessionLocal() as s:
        assert s.get(Order, cid).status == "reorged"


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


@respx.mock
def test_tx_against_canceled_order_rejected_and_transfer_stays_claimable(make_store):
    # A released (canceled) or reorged order must never record a transfer, or a
    # griefer could orphan a real buyer's payment (globally consuming the
    # tx_hash+log_index via ProcessedTransfer) while delivering nothing.
    # Regression for the M3 re-review money-safety hole.
    _mk(make_store, "grief", "0x" + "b" * 40, delivery="GOODS")
    pay_to = "0x" + "b" * 40
    cid_dead, exp = _order("grief")
    cid_real, _ = _order("grief")
    # Kill the first order the way release_expired does, and make the real
    # buyer's order share its amount (the collision a griefer engineers).
    with SessionLocal() as s:
        s.get(Order, cid_dead).status = "canceled"
        s.get(Order, cid_real).expected_micro = exp
        s.commit()
    rpc = Rpc(head=200, receipts={TX1: _receipt([_log(pay_to, exp, TX1, 0, 100)], 100)})
    rpc.install()

    # Submitting the buyer's tx against the dead order is rejected and consumes
    # nothing.
    assert (
        client.post(f"/api/checkout/{cid_dead}/tx", json={"tx_hash": TX1}).status_code
        == 400
    )
    with SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(ProcessedTransfer)) == 0
    # The true owner can still claim the same transfer.
    assert (
        client.post(f"/api/checkout/{cid_real}/tx", json={"tx_hash": TX1}).status_code
        == 200
    )


@respx.mock
def test_concurrent_tx_commit_conflict_reconciles_not_500(make_store, monkeypatch):
    # Two identical /tx submissions race: both pass the in-session ProcessedTransfer
    # pre-check, then the loser trips uq_processed_tx_log at commit. The handler must
    # reconcile to the winner's committed state (200), never surface a 500 — and
    # there is still exactly one transfer and one delivery. Reproduced single-thread
    # by committing the winner in the window between verify_txhash and the commit.
    _mk(make_store, "conc", "0x" + "9" * 39 + "a", delivery="CONC")
    pay_to = "0x" + "9" * 39 + "a"
    cid, exp = _order("conc")
    rpc = Rpc(head=200, receipts={TX1: _receipt([_log(pay_to, exp, TX1, 0, 100)], 100)})
    rpc.install()

    def racing_verify(session, order, tx_hash):
        # This request buffers its transfer (no flush, so it holds no write lock)…
        session.add(
            ProcessedTransfer(
                tx_hash=tx_hash,
                log_index=0,
                order_id=order.id,
                pay_to=order.pay_to,
                from_addr=FROM,
                amount_micro=exp,
                block_number=100,
                seen_at=checkout._now(),
            )
        )
        # …then the concurrent winner commits the same transfer first, so this
        # request's commit will trip uq_processed_tx_log.
        with SessionLocal() as other:
            checkout.apply_transfer(
                other, other.get(Order, order.id), exp, tx_hash, 0, 100, FROM, 200
            )
            other.commit()

    monkeypatch.setattr(checkout, "verify_txhash", racing_verify)

    r = client.post(f"/api/checkout/{cid}/tx", json={"tx_hash": TX1})
    assert r.status_code == 200  # reconciled to the winner, not a 500
    assert r.json()["status"] == "paid" and r.json()["delivery"] == "CONC"
    with SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(ProcessedTransfer)) == 1
        assert (
            s.scalar(
                select(func.count())
                .select_from(Delivery)
                .where(Delivery.order_id == cid)
            )
            == 1
        )
        assert s.get(Order, cid).status == "delivered"


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


@respx.mock
def test_expired_within_quarantine_credits_on_fast_path(make_store):
    _mk(make_store, "eqok", "0x" + "1" * 39 + "a", delivery="LATE-FAST")
    pay_to = "0x" + "1" * 39 + "a"
    cid, exp = _order("eqok")
    with SessionLocal() as s:
        o = s.get(Order, cid)
        o.status = "expired"
        o.expires_at = checkout._now() - timedelta(minutes=1)  # within quarantine
        s.commit()
    rpc = Rpc(head=200, receipts={TX1: _receipt([_log(pay_to, exp, TX1, 0, 150)], 150)})
    rpc.install()

    r = client.post(f"/api/checkout/{cid}/tx", json={"tx_hash": TX1})
    assert r.status_code == 200 and r.json()["status"] == "paid"
    assert r.json()["delivery"] == "LATE-FAST"


@respx.mock
def test_expired_past_quarantine_rejected_on_fast_path(make_store):
    _mk(make_store, "eqno", "0x" + "1" * 39 + "b")
    pay_to = "0x" + "1" * 39 + "b"
    cid, exp = _order("eqno")
    with SessionLocal() as s:
        o = s.get(Order, cid)
        o.status = "expired"
        o.expires_at = checkout._now() - timedelta(
            hours=config.QUARANTINE_HOURS, minutes=5
        )
        s.commit()
    rpc = Rpc(head=200, receipts={TX1: _receipt([_log(pay_to, exp, TX1, 0, 150)], 150)})
    rpc.install()

    r = client.post(f"/api/checkout/{cid}/tx", json={"tx_hash": TX1})
    assert r.status_code == 400  # past quarantine: the fast path refuses to revive it
    with SessionLocal() as s:
        assert s.get(Order, cid).status == "expired"
        assert s.scalar(select(func.count()).select_from(ProcessedTransfer)) == 0


def test_expired_amount_released_after_quarantine(make_store):
    # Past-quarantine expired orders leave the unique-amount index so the burned
    # offset is genuinely re-allocatable — the quarantine release is real, not
    # illusory (the amount was previously pinned by the index forever).
    store_id = make_store(slug="rel", pay_to="0x" + "a" * 38 + "cd")
    pay_to = "0x" + "a" * 38 + "cd"
    cid, exp = _order("rel")
    with SessionLocal() as s:
        o = s.get(Order, cid)
        o.status = "expired"
        o.expires_at = checkout._now() - timedelta(
            hours=config.QUARANTINE_HOURS, minutes=5
        )
        s.commit()

    with SessionLocal() as s:
        checkout.release_expired(s)
        s.commit()

    with SessionLocal() as s:
        assert s.get(Order, cid).status == "canceled"
        # the amount is now free: a fresh active order on the same (pay_to, amount)
        # no longer trips the partial unique index
        s.add(
            Order(
                id="reused0000000001",
                store_id=store_id,
                pay_to=pay_to,
                amount_micro=exp,
                expected_micro=exp,
                status="pending",
                expires_at=checkout._now() + timedelta(minutes=30),
            )
        )
        s.commit()  # must not raise IntegrityError
        assert s.get(Order, "reused0000000001").status == "pending"


def test_underpaid_order_expires_via_sweeper(make_store):
    # A stuck underpaid order (e.g. legacy data) must not pin its amount/address
    # forever: flip_expired expires it on expires_at like pending.
    make_store(slug="undr", pay_to="0x" + "b" * 39 + "a")
    cid, exp = _order("undr")
    with SessionLocal() as s:
        o = s.get(Order, cid)
        o.status = "underpaid"
        o.expires_at = checkout._now() - timedelta(minutes=1)
        s.commit()
    with SessionLocal() as s:
        checkout.flip_expired(s)
        s.commit()
    with SessionLocal() as s:
        assert s.get(Order, cid).status == "expired"


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


# ---------------------------------------- M4: deliver() with a deliverable
def _confirm_ready(cid, exp, **fields):
    with SessionLocal() as s:
        assert checkout.transition(
            s, cid, ("pending",), "confirmed", paid_micro=exp, **fields
        )
        s.commit()


def _run_deliver(cid):
    with SessionLocal() as s:
        checkout.deliver(s, s.get(Order, cid))
        s.commit()


def test_deliver_without_deliverable_uses_store_text_legacy(make_store):
    # LEGACY REGRESSION: a store with no deliverable row delivers store.delivery
    # text exactly as before, and writes no entitlement.
    _mk(make_store, "legfb", "0x" + "a" * 40, delivery="STORE-TEXT")
    cid, exp = _order("legfb")
    _confirm_ready(cid, exp)
    _run_deliver(cid)
    with SessionLocal() as s:
        d = s.scalar(select(Delivery).where(Delivery.order_id == cid))
        assert d.kind == "text" and d.payload == "STORE-TEXT"
        assert s.scalar(select(Entitlement).where(Entitlement.order_id == cid)) is None


def test_deliver_with_file_deliverable_creates_entitlement(make_store):
    _mk(make_store, "fdel", "0x" + "a" * 40, delivery="LEGACY")
    cid, exp = _order("fdel")
    with SessionLocal() as s:
        o = s.get(Order, cid)
        s.add(
            Deliverable(
                store_id=o.store_id,
                kind="file",
                file_sha256="ab" * 32,
                file_name="x.pdf",
                file_size=10,
                active=True,
            )
        )
        s.commit()
    _confirm_ready(cid, exp, from_addr="0x" + "c" * 40)
    _run_deliver(cid)
    with SessionLocal() as s:
        d = s.scalar(select(Delivery).where(Delivery.order_id == cid))
        assert d.kind == "file" and d.payload == delivery.FILE_READY_MESSAGE
        ent = s.scalar(select(Entitlement).where(Entitlement.order_id == cid))
        assert ent is not None
        assert ent.buyer_addr == "0x" + "c" * 40 and ent.license_key is None


def test_deliver_with_text_deliverable_delivers_secret_not_store_text(make_store):
    _mk(make_store, "txtd", "0x" + "a" * 40, delivery="IGNORED-STORE-TEXT")
    cid, exp = _order("txtd")
    with SessionLocal() as s:
        o = s.get(Order, cid)
        s.add(
            Deliverable(
                store_id=o.store_id, kind="text", payload="THE-SECRET", active=True
            )
        )
        s.commit()
    _confirm_ready(cid, exp)
    _run_deliver(cid)
    with SessionLocal() as s:
        d = s.scalar(select(Delivery).where(Delivery.order_id == cid))
        assert d.kind == "text" and d.payload == "THE-SECRET"


def test_deliver_with_license_deliverable_issues_unique_key(make_store):
    _mk(make_store, "licd", "0x" + "a" * 40)
    cid, exp = _order("licd")
    with SessionLocal() as s:
        o = s.get(Order, cid)
        s.add(
            Deliverable(
                store_id=o.store_id, kind="license", max_activations=3, active=True
            )
        )
        s.commit()
    _confirm_ready(cid, exp)
    _run_deliver(cid)
    with SessionLocal() as s:
        ent = s.scalar(select(Entitlement).where(Entitlement.order_id == cid))
        d = s.scalar(select(Delivery).where(Delivery.order_id == cid))
        assert ent.license_key.startswith("TILLA-")
        assert d.kind == "license" and d.payload == ent.license_key


def _add_product(store_id, name, price_micro):
    from app.models import Product

    with SessionLocal() as s:
        s.add(
            Product(store_id=store_id, name=name, price_micro=price_micro, active=True)
        )
        s.commit()


def test_checkout_product_index_selects_that_product(make_store):
    # A store with two products: primary "Thing" @ 9, index 1 "Deluxe" @ 20.
    sid = make_store(slug="multi", price_micro=9_000_000)
    _add_product(sid, "Deluxe", 20_000_000)
    r = client.post("/api/checkout/multi", json={"product_index": 1})
    assert r.status_code == 200, r.text
    assert r.json()["product_name"] == "Deluxe"
    with SessionLocal() as s:
        # amount_micro is the exact product price (offset only affects expected_micro)
        assert s.get(Order, r.json()["id"]).amount_micro == 20_000_000
    r0 = client.post("/api/checkout/multi", json={})
    assert r0.json()["product_name"] == "Thing"
    with SessionLocal() as s:
        assert s.get(Order, r0.json()["id"]).amount_micro == 9_000_000


def test_checkout_product_index_out_of_range_fails_closed(make_store):
    make_store(slug="oor", price_micro=9_000_000)
    assert (
        client.post("/api/checkout/oor", json={"product_index": 5}).status_code == 400
    )


def test_checkout_no_body_defaults_to_primary(make_store):
    make_store(slug="prim", price_micro=9_000_000)
    r = client.post("/api/checkout/prim")
    assert r.status_code == 200 and r.json()["product_name"] == "Thing"


def test_checkout_by_product_id_charges_that_product(make_store):
    from sqlalchemy import select

    from app.models import Product

    sid = make_store(slug="byid", price_micro=9_000_000)  # primary "Thing" @ 9
    _add_product(sid, "Deluxe", 20_000_000)
    with SessionLocal() as s:
        deluxe_id = s.scalars(
            select(Product.id).where(Product.store_id == sid).order_by(Product.id)
        ).all()[1]
    r = client.post("/api/checkout/byid", json={"product_id": deluxe_id})
    assert r.status_code == 200 and r.json()["product_name"] == "Deluxe"
    with SessionLocal() as s:
        assert s.get(Order, r.json()["id"]).amount_micro == 20_000_000


def test_checkout_foreign_or_unknown_product_id_fails_closed(make_store):
    from sqlalchemy import select

    from app.models import Product

    make_store(slug="sa", price_micro=9_000_000)
    b = make_store(slug="sb", price_micro=5_000_000)
    with SessionLocal() as s:
        b_pid = s.scalar(select(Product.id).where(Product.store_id == b))
    # store sa checkout with store sb's product id -> 404 (no IDOR, no wrong charge)
    assert (
        client.post("/api/checkout/sa", json={"product_id": b_pid}).status_code == 404
    )
    # a product id that doesn't exist -> 404
    assert (
        client.post("/api/checkout/sa", json={"product_id": 10_000_000}).status_code
        == 404
    )


# ------------------------------------------------ per-product deliverables (C)
def test_active_deliverable_prefers_product_then_store_default(make_store):
    from sqlalchemy import select

    import app.checkout as checkout
    from app.models import Deliverable, Product

    sid = make_store(slug="del", price_micro=9_000_000)  # primary "Thing"
    _add_product(sid, "Deluxe", 20_000_000)
    with SessionLocal() as s:
        ids = s.scalars(
            select(Product.id).where(Product.store_id == sid).order_by(Product.id)
        ).all()
        primary_id, deluxe_id = ids[0], ids[1]
        s.add(
            Deliverable(store_id=sid, kind="text", payload="STORE DEFAULT", active=True)
        )
        s.add(
            Deliverable(
                store_id=sid,
                product_id=deluxe_id,
                kind="text",
                payload="DELUXE ONLY",
                active=True,
            )
        )
        s.commit()
    with SessionLocal() as s:
        # a deluxe order gets the deluxe-specific deliverable
        assert checkout._active_deliverable(s, sid, deluxe_id).payload == "DELUXE ONLY"
        # the primary order (no primary-specific deliverable) falls back to the default
        assert (
            checkout._active_deliverable(s, sid, primary_id).payload == "STORE DEFAULT"
        )
        # no product context -> store default (legacy behaviour)
        assert checkout._active_deliverable(s, sid).payload == "STORE DEFAULT"


def test_deliverable_product_id_rejects_foreign_product(make_store):
    from sqlalchemy import select

    import pytest
    from fastapi import HTTPException

    import app.main as main
    from app.models import Product, Store

    sid = make_store(slug="dpv", price_micro=9_000_000)
    other = make_store(slug="dpv2", price_micro=5_000_000)
    with SessionLocal() as s:
        store = s.get(Store, sid)
        pid = s.scalar(select(Product.id).where(Product.store_id == sid))
        other_pid = s.scalar(select(Product.id).where(Product.store_id == other))
        assert main._deliverable_product_id(s, store, None) is None
        assert main._deliverable_product_id(s, store, str(pid)) == pid
        with pytest.raises(HTTPException) as e:
            main._deliverable_product_id(s, store, str(other_pid))
        assert e.value.status_code == 422


def test_norm_addr_lowercases_or_passes_none():
    """An EVM address is case-insensitive, so exactly one shape may be stored."""
    assert checkout.norm_addr("0x" + "A" * 40) == "0x" + "a" * 40
    assert checkout.norm_addr("0x" + "a" * 40) == "0x" + "a" * 40
    assert checkout.norm_addr(None) is None
    assert checkout.norm_addr("") is None


def test_payer_address_is_stored_lowercased(make_store):
    """The sweeper reads a payer off a log while the x402 paths recover it from a
    signature and get checksum casing. Both must land lowercased, or the same
    wallet exists twice and per-buyer grouping (the customer export) splits it."""
    make_store(slug="normaddr", price_micro=9_000_000)
    cid = client.post("/api/checkout/normaddr").json()["id"]
    with SessionLocal() as s:
        order = s.get(Order, cid)
        exp = order.expected_micro
        checkout.apply_transfer(
            s,
            order,
            exp,
            tx_hash="0x" + "e" * 64,
            log_index=0,
            block_number=100,
            from_addr="0x" + "A" * 40,  # checksum-cased on the way in
            head=200,
        )
        s.commit()
    with SessionLocal() as s:
        assert s.get(Order, cid).from_addr == "0x" + "a" * 40
        stored = s.scalar(
            select(ProcessedTransfer.from_addr).where(ProcessedTransfer.order_id == cid)
        )
        assert stored == "0x" + "a" * 40


# ------------------------------------------- matching offset is price-scaled
def test_cheap_product_surcharge_is_bounded(make_store):
    # Regression: the offset was a flat 1-4999 micro, so on a 0.01 product a buyer
    # told "0.01 USDT0" could be billed 0.0146. A merchant measured 10.6%, 32.1%
    # and 45.5% surcharges against 0.4% on a 1.0 product. The span now scales.
    base = 10_000  # 0.01 USDT0
    _mk(make_store, "cheap-surcharge", "0x" + "d" * 40, price=base)
    for i in range(12):
        _cid, expected = _order("cheap-surcharge")
        surcharge = expected - base
        assert 0 < surcharge <= base * config.AMOUNT_OFFSET_MAX_PCT / 100, (
            f"draw {i}: {surcharge} micro on a {base} base is more than "
            f"{config.AMOUNT_OFFSET_MAX_PCT}%"
        )


def test_expensive_product_keeps_the_original_span(make_store):
    # No regression for real-money products: at 1.0 USDT the 1% cap (10000) is
    # above AMOUNT_OFFSET_MAX, so the span is exactly what it always was.
    base = 1_000_000
    _mk(make_store, "dear-surcharge", "0x" + "e" * 40, price=base)
    for _ in range(8):
        _cid, expected = _order("dear-surcharge")
        surcharge = expected - base
        assert config.AMOUNT_OFFSET_MIN <= surcharge <= config.AMOUNT_OFFSET_MAX


def test_floor_keeps_the_allocator_viable_on_dust_prices(make_store):
    # 1% of a 0.001 product is 10 micro -- too few amounts for the retry loop, so
    # a floor applies. The surcharge is larger in percentage terms and that is the
    # accepted trade: a starved span can only ever yield "checkout busy", never a
    # wrong charge.
    base = 1_000  # 0.001 USDT0
    _mk(make_store, "dust-surcharge", "0x" + "f" * 40, price=base)
    seen = set()
    for _ in range(6):
        _cid, expected = _order("dust-surcharge")
        surcharge = expected - base
        assert 0 < surcharge <= config.AMOUNT_OFFSET_FLOOR
        seen.add(expected)
    assert len(seen) == 6  # distinct amounts still allocated

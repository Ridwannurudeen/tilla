"""M3 hardened checkout: state machine, unique-amount allocation, the polling
sweeper, the buyer-txhash fast path, and the delivery-race fix.

Payment matching is by EXACT ``expected_micro`` (price + unique offset), never a
balance delta — so concurrent buyers on one merchant address can't confirm off a
single credit. Every status change goes through :func:`transition` (a conditional
UPDATE), and every consumed transfer is recorded in ``processed_transfers`` in the
same transaction, so replays and races can't double-deliver.
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import chain, config, delivery
from app.db import SessionLocal
from app.models import (
    ChainCursor,
    Deliverable,
    Delivery,
    Entitlement,
    Order,
    ProcessedTransfer,
    Product,
    Store,
    log_event,
)

logger = logging.getLogger("tilla")

DEFAULT_DELIVERY = "Payment received — thank you!"

# In-flight reservations (unpaid or partially paid): always hold their amount.
IN_FLIGHT = ("pending", "detected", "underpaid")
# Received this order's funds but not yet delivered — still ready to deliver.
READY_TO_DELIVER = ("confirmed", "overpaid", "late_paid")
# Any order whose amount is definitively spoken for right now: never reassign it.
ALWAYS_HOLD = IN_FLIGHT + READY_TO_DELIVER
# Delivered/terminal: keep the amount reserved for a cooldown so a stranger's
# accidental duplicate payment can't confirm a fresh order on the same address.
COOLDOWN_HOLD = ("delivered", "paid", "refunded")
# Terminal happy states; GET surfaces both as "paid" for client compatibility.
TERMINAL_DELIVERED = ("delivered", "paid")


class AmountUnavailable(RuntimeError):
    """Offset space for this merchant address is momentarily exhausted (503)."""


class TxVerificationError(ValueError):
    """A submitted txhash failed on-chain verification (400)."""


class TxAlreadyUsed(RuntimeError):
    """A submitted transfer is already consumed by another order (409)."""


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _naive(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


# ---------------------------------------------------------------- transitions
def transition(
    session: Session, order_id: str, from_states, to_state, **fields
) -> bool:
    """Conditional status UPDATE. Returns True iff exactly one row moved — a lost
    race returns False, so no transition ever runs twice."""
    result = session.execute(
        update(Order)
        .where(Order.id == order_id, Order.status.in_(tuple(from_states)))
        .values(status=to_state, **fields)
    )
    return result.rowcount == 1


def _active_deliverable(session: Session, store_id: int) -> Deliverable | None:
    """The store's one active deliverable, if any. None → the legacy store.delivery
    text path, unchanged for every existing store."""
    return session.scalar(
        select(Deliverable)
        .where(Deliverable.store_id == store_id, Deliverable.active.is_(True))
        .order_by(Deliverable.id.desc())
        .limit(1)
    )


def deliver(session: Session, order: Order):
    """Flip a ready order to delivered AND write its Delivery row in one txn.
    Idempotent: a lost race (rowcount 0) or a duplicate Delivery returns the
    existing row instead of raising — this is the fix for the concurrent-poll
    delivery race that used to 500 the loser on UNIQUE(order_id).

    If the store has an active deliverable, an Entitlement is created in the same
    txn (begin_nested + UNIQUE(order_id), same idempotency as the Delivery row)
    and the Delivery payload is the text secret / license key / a short file-ready
    message. With no deliverable the behaviour is EXACTLY the legacy text path."""
    if not transition(session, order.id, READY_TO_DELIVER, "delivered", paid_at=_now()):
        return session.scalar(select(Delivery).where(Delivery.order_id == order.id))
    store = session.get(Store, order.store_id)
    deliverable = _active_deliverable(session, order.store_id)
    if deliverable is None:
        kind = "text"
        payload = store.delivery if store and store.delivery else DEFAULT_DELIVERY
    else:
        license_key = None
        if deliverable.kind == "license":
            kind = "license"
            license_key = delivery.mint_license_key()
            payload = license_key
        elif deliverable.kind == "file":
            kind = "file"
            payload = delivery.FILE_READY_MESSAGE
        else:
            kind = "text"
            payload = deliverable.payload or DEFAULT_DELIVERY
        try:
            with session.begin_nested():
                session.add(
                    Entitlement(
                        order_id=order.id,
                        deliverable_id=deliverable.id,
                        buyer_addr=order.from_addr,
                        license_key=license_key,
                    )
                )
        except IntegrityError:
            # Entitlement already issued for this order (idempotent replay).
            pass
    try:
        with session.begin_nested():
            session.add(Delivery(order_id=order.id, kind=kind, payload=payload))
    except IntegrityError:
        # belt-and-braces: a Delivery already existed (legacy 'paid' edge); the
        # savepoint rolled back only the insert, so re-read and return it.
        pass
    log_event(
        session, "engine", "order.delivered", store_id=order.store_id, order_id=order.id
    )
    return session.scalar(select(Delivery).where(Delivery.order_id == order.id))


# ------------------------------------------------------- amount allocation
def _amount_taken(session: Session, pay_to: str, candidate: int, now: datetime) -> bool:
    rows = session.execute(
        select(Order.status, Order.paid_at, Order.created_at, Order.expires_at).where(
            Order.pay_to == pay_to, Order.expected_micro == candidate
        )
    ).all()
    quarantine = timedelta(hours=config.QUARANTINE_HOURS)
    for status, paid_at, created_at, expires_at in rows:
        if status in ALWAYS_HOLD:
            return True
        if status == "expired":
            exp = _naive(expires_at) or _naive(created_at)
            if exp is not None and exp + quarantine > now:
                return True
        elif status in COOLDOWN_HOLD:
            reached = _naive(paid_at) or _naive(created_at)
            if reached is not None and reached + quarantine > now:
                return True
    return False


def _current_head() -> int | None:
    """Chain head at order creation, stored as a floor so a buyer-submitted txhash
    can't credit a historical transfer. Best-effort: with the sweeper disabled
    (tests) or on an RPC blip we store NULL and the floor is simply not enforced
    for that order — exact-amount matching still guards the concurrent case."""
    if not config.SWEEP_ENABLED:
        return None
    try:
        return chain.block_number()
    except (httpx.HTTPError, chain.ChainError, ValueError):
        return None


def create_order(session: Session, store: Store, product: Product) -> Order:
    """Insert a pending order with a unique per-address expected_micro. Redraws
    the offset on collision (app check) or IntegrityError (index backstop);
    raises AmountUnavailable after config.AMOUNT_ALLOC_RETRIES draws."""
    now = _now()
    created_block = _current_head()
    for _ in range(config.AMOUNT_ALLOC_RETRIES):
        offset = random.randint(config.AMOUNT_OFFSET_MIN, config.AMOUNT_OFFSET_MAX)
        expected = product.price_micro + offset
        if _amount_taken(session, store.pay_to, expected, now):
            continue
        order = Order(
            id=uuid.uuid4().hex[:16],
            store_id=store.id,
            product_id=product.id,
            pay_to=store.pay_to,
            amount_micro=product.price_micro,
            expected_micro=expected,
            status="pending",
            created_block=created_block,
            expires_at=now + timedelta(minutes=config.ORDER_TTL_MIN),
        )
        session.add(order)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            continue
        return order
    raise AmountUnavailable("checkout busy, retry")


# --------------------------------------------------------- payment matching
def _match_order(
    session: Session, pay_to: str, value: int, now: datetime
) -> Order | None:
    """Exact-amount match. Precedence: in-flight orders first, then a
    quarantined expired order (→ late_paid)."""
    order = session.scalar(
        select(Order)
        .where(
            Order.pay_to == pay_to,
            Order.expected_micro == value,
            Order.status.in_(IN_FLIGHT),
        )
        .order_by(Order.created_at)
        .limit(1)
    )
    if order is not None:
        return order
    quarantine = timedelta(hours=config.QUARANTINE_HOURS)
    for o in session.scalars(
        select(Order).where(
            Order.pay_to == pay_to,
            Order.expected_micro == value,
            Order.status == "expired",
        )
    ):
        exp = _naive(o.expires_at) or _naive(o.created_at)
        if exp is not None and exp + quarantine > now:
            return o
    return None


def _record_transfer(
    session, order, value, tx_hash, log_index, block_number, from_addr
):
    session.add(
        ProcessedTransfer(
            tx_hash=tx_hash,
            log_index=log_index,
            order_id=order.id,
            pay_to=order.pay_to,
            from_addr=(from_addr or ""),
            amount_micro=value,
            block_number=block_number,
            seen_at=_now(),
        )
    )


def _drive_state(session, order, added, block_number, from_addr, tx_hash, head):
    """Accumulate `added` onto the order and drive its state. `order` is read for
    its current status/paid_micro/expected_micro before any transition."""
    depth_ok = (head - block_number) >= config.CONFIRMATIONS
    cumulative = (order.paid_micro or 0) + added
    fields = dict(
        paid_micro=cumulative,
        block_number=block_number,
        from_addr=(from_addr or None),
        tx_hash=tx_hash,
    )

    if order.status == "expired":
        transition(session, order.id, ("expired",), "late_paid", **fields)
        log_event(
            session,
            "sweeper",
            "order.late_paid",
            store_id=order.store_id,
            order_id=order.id,
        )
        if depth_ok:
            deliver(session, order)
        return

    if cumulative < order.expected_micro:
        transition(session, order.id, ("pending", "underpaid"), "underpaid", **fields)
        log_event(
            session,
            "checkout",
            "order.underpaid",
            store_id=order.store_id,
            order_id=order.id,
        )
        return

    over = cumulative - order.expected_micro
    if not depth_ok:
        transition(
            session, order.id, IN_FLIGHT, "detected", overpaid_micro=over, **fields
        )
        log_event(
            session,
            "checkout",
            "order.detected",
            store_id=order.store_id,
            order_id=order.id,
        )
        return

    to_state = "overpaid" if over > 0 else "confirmed"
    transition(session, order.id, IN_FLIGHT, to_state, overpaid_micro=over, **fields)
    log_event(
        session,
        "checkout",
        "order.confirmed",
        store_id=order.store_id,
        order_id=order.id,
    )
    deliver(session, order)


def apply_transfer(
    session, order, value, tx_hash, log_index, block_number, from_addr, head
):
    """Record one verified transfer and drive the order's state, in one txn."""
    _record_transfer(session, order, value, tx_hash, log_index, block_number, from_addr)
    _drive_state(session, order, value, block_number, from_addr, tx_hash, head)


# ------------------------------------------------------------ fast path
def verify_txhash(session: Session, order: Order, tx_hash: str):
    """Verify a buyer-submitted txhash against the USDT0 contract and apply it.
    Credits the order ONLY when the tx's new USDT0 transfers to pay_to sum EXACTLY
    to expected_micro (mirrors _match_order — no >= accumulation, no underpay
    top-up), so an attacker can't farm a concurrent or historical inbound transfer
    for free goods. Rejects a mismatched contract/recipient/status, a not-found tx,
    a transfer mined below the order's creation floor, an out-of-quarantine expired
    order, a non-exact total (400), and a transfer already consumed by another
    order (409). A non-matching transfer is never recorded, so its true owner order
    can still claim it."""
    if order.status in TERMINAL_DELIVERED or order.status == "refunded":
        return
    if order.status in ("canceled", "reorged"):
        # A released/reorged order is dead: never record a transfer against it,
        # or a griefer could orphan a real buyer's payment (globally consuming
        # its tx_hash+log_index via ProcessedTransfer) while delivering nothing.
        raise TxVerificationError("order is no longer active")
    if order.status == "expired":
        # Same quarantine window the sweeper's _match_order enforces: a late tx can
        # only revive an expired order inside the window, never arbitrarily later.
        exp = _naive(order.expires_at) or _naive(order.created_at)
        quarantine = timedelta(hours=config.QUARANTINE_HOURS)
        if exp is None or exp + quarantine <= _now():
            raise TxVerificationError("order expired past the payment window")
    receipt = chain.get_transaction_receipt(tx_hash)
    if receipt is None:
        raise TxVerificationError("transaction not found")
    if str(receipt.get("status", "")).lower() != "0x1":
        raise TxVerificationError("transaction did not succeed on-chain")

    want_to = chain.pad_address(order.pay_to).lower()
    matched_any = False
    new_logs: list[dict] = []
    total = 0
    for lg in receipt.get("logs", []):
        if (lg.get("address", "") or "").lower() != config.USDT0:
            continue
        topics = lg.get("topics", [])
        if len(topics) < 3 or topics[0].lower() != config.TRANSFER_TOPIC:
            continue
        if topics[2].lower() != want_to:
            continue
        matched_any = True
        d = chain.decode_transfer_log(lg)
        if order.created_block is not None and d["block_number"] < order.created_block:
            # Mined before this order existed — a historical/M2-era transfer, never
            # this order's payment. Reject so it can't be farmed for free goods.
            raise TxVerificationError("transfer predates the order")
        pt = session.scalar(
            select(ProcessedTransfer).where(
                ProcessedTransfer.tx_hash == d["tx_hash"],
                ProcessedTransfer.log_index == d["log_index"],
            )
        )
        if pt is not None:
            if pt.order_id != order.id:
                raise TxAlreadyUsed()
            continue  # already counted for this order — idempotent
        total += d["value"]
        new_logs.append(d)

    if not matched_any:
        raise TxVerificationError("no USDT0 transfer to this store in that transaction")
    if not new_logs:
        return  # everything already applied to this order
    if total != order.expected_micro:
        # Not the exact payable amount. Reject WITHOUT recording any transfer, so
        # the transfer's true owner order can still consume it.
        raise TxVerificationError("transfer amount does not match the exact total due")

    head = chain.block_number()
    for d in new_logs:
        _record_transfer(
            session,
            order,
            d["value"],
            d["tx_hash"],
            d["log_index"],
            d["block_number"],
            d["from"],
        )
    _drive_state(
        session,
        order,
        total,
        new_logs[0]["block_number"],
        new_logs[0]["from"],
        tx_hash,
        head,
    )


# --------------------------------------------------------- reorg re-check
def _receipt_still_valid(order: Order) -> bool:
    """Re-fetch the receipt for a fast-path-ingested order and confirm the tx still
    succeeds at the SAME block with a USDT0 transfer to pay_to. detected/late_paid
    are only ever reached via the depth-0 fast path (the sweeper ingests at
    safe_head and delivers immediately), so an order maturing from either state
    must be re-checked against a reorg before it delivers."""
    receipt = chain.get_transaction_receipt(order.tx_hash)
    if receipt is None:
        return False
    if str(receipt.get("status", "")).lower() != "0x1":
        return False
    block_hex = receipt.get("blockNumber")
    if block_hex is None or int(block_hex, 16) != order.block_number:
        return False
    want_to = chain.pad_address(order.pay_to).lower()
    for lg in receipt.get("logs", []):
        if (lg.get("address", "") or "").lower() != config.USDT0:
            continue
        topics = lg.get("topics", [])
        if len(topics) < 3 or topics[0].lower() != config.TRANSFER_TOPIC:
            continue
        if topics[2].lower() == want_to:
            return True
    return False


def _mark_reorged(session: Session, order: Order):
    transition(session, order.id, ("detected", "late_paid"), "reorged")
    log_event(
        session, "checkout", "order.reorged", store_id=order.store_id, order_id=order.id
    )


# ---------------------------------------------------- on-demand GET progress
def refresh_order(session: Session, order: Order):
    """Cheap progress for a polling buyer between sweep ticks: flip expiry, and
    mature a detected/late_paid order to delivered once depth is reached. Makes
    one eth_blockNumber call only in the depth-sensitive states."""
    now = _now()
    if order.status == "pending":
        exp = _naive(order.expires_at)
        if exp is None:
            exp = _naive(order.created_at) + timedelta(minutes=config.ORDER_TTL_MIN)
        if now > exp:
            transition(session, order.id, ("pending",), "expired")
            log_event(
                session,
                "sweeper",
                "order.expired",
                store_id=order.store_id,
                order_id=order.id,
            )
            session.commit()
        return

    if order.status in ("detected", "late_paid") and order.block_number is not None:
        try:
            head = chain.block_number()
        except (httpx.HTTPError, chain.ChainError, ValueError):
            logger.warning("refresh_order: block_number unavailable for %s", order.id)
            return
        if order.block_number <= head - config.CONFIRMATIONS:
            if not _receipt_still_valid(order):
                _mark_reorged(session, order)
                session.commit()
                return
            if order.status == "detected":
                over = (order.paid_micro or 0) - order.expected_micro
                to_state = "overpaid" if over > 0 else "confirmed"
                transition(session, order.id, ("detected",), to_state)
                log_event(
                    session,
                    "checkout",
                    "order.confirmed",
                    store_id=order.store_id,
                    order_id=order.id,
                )
            deliver(session, order)
            session.commit()


# ----------------------------------------------------------- the sweeper
def _get_or_init_cursor(session: Session, safe_head: int) -> int:
    row = session.get(ChainCursor, 1)
    if row is None:
        # First boot: start at the current safe head — no historical backfill storm.
        session.add(ChainCursor(id=1, last_block=safe_head, updated_at=_now()))
        session.flush()
        return safe_head
    return row.last_block


def _set_cursor(session: Session, block: int):
    row = session.get(ChainCursor, 1)
    if row is None:
        session.add(ChainCursor(id=1, last_block=block, updated_at=_now()))
    else:
        row.last_block = block
        row.updated_at = _now()


def flip_expired(session: Session):
    now = _now()
    # 'underpaid' is included so a dust underpay can't pin the amount reservation
    # and the merchant address into every getLogs call forever: it expires on
    # expires_at like pending, then follows the normal quarantine/release path.
    for o in session.scalars(
        select(Order).where(Order.status.in_(("pending", "underpaid")))
    ):
        exp = _naive(o.expires_at)
        if exp is None:  # legacy pre-M3 order: created_at + TTL, not instant expiry
            exp = _naive(o.created_at) + timedelta(minutes=config.ORDER_TTL_MIN)
        if now > exp:
            transition(session, o.id, ("pending", "underpaid"), "expired")
            log_event(
                session, "sweeper", "order.expired", store_id=o.store_id, order_id=o.id
            )


def release_expired(session: Session):
    """Move expired orders past the quarantine window to a terminal status OUTSIDE
    the unique-amount index predicate, so the reserved offset is actually released
    for re-allocation. Without this the 24h quarantine is illusory: the expired row
    keeps holding the index, every redraw of that amount IntegrityErrors, and each
    abandoned checkout permanently burns one of the 4999 offsets per price point.
    _match_order already refuses past-quarantine expired orders, so a late tx that
    could still have matched is never lost."""
    now = _now()
    quarantine = timedelta(hours=config.QUARANTINE_HOURS)
    for o in session.scalars(select(Order).where(Order.status == "expired")):
        exp = _naive(o.expires_at) or _naive(o.created_at)
        if exp is not None and exp + quarantine <= now:
            transition(session, o.id, ("expired",), "canceled")
            log_event(
                session,
                "sweeper",
                "order.released",
                store_id=o.store_id,
                order_id=o.id,
            )


def _active_addresses(session: Session, now: datetime) -> list[str]:
    quarantine = timedelta(hours=config.QUARANTINE_HOURS)
    addrs: set[str] = set()
    for pay_to, status, expires_at, created_at in session.execute(
        select(Order.pay_to, Order.status, Order.expires_at, Order.created_at).where(
            Order.status.in_(("pending", "detected", "underpaid", "expired"))
        )
    ).all():
        if status != "expired":
            addrs.add(pay_to)
            continue
        exp = _naive(expires_at) or _naive(created_at)
        if exp is not None and exp + quarantine > now:
            addrs.add(pay_to)
    return sorted(addrs)


def _process_log(session: Session, log: dict, head: int, now: datetime):
    d = chain.decode_transfer_log(log)
    if session.scalar(
        select(ProcessedTransfer.id).where(
            ProcessedTransfer.tx_hash == d["tx_hash"],
            ProcessedTransfer.log_index == d["log_index"],
        )
    ):
        return  # replayed window — no-op
    order = _match_order(session, d["to"], d["value"], now)
    if order is None:
        # self-sends, dust, unrelated buyers, round-price payments missing the
        # offset — logged for support, never auto-confirms an order.
        log_event(
            session,
            "sweeper",
            "transfer.unmatched",
            data={
                "tx_hash": d["tx_hash"],
                "log_index": d["log_index"],
                "to": d["to"],
                "value": d["value"],
            },
        )
        return
    apply_transfer(
        session,
        order,
        d["value"],
        d["tx_hash"],
        d["log_index"],
        d["block_number"],
        d["from"],
        head,
    )


def _promote_matured(session: Session, head: int):
    """Deliver detected/late_paid orders whose depth has reached CONFIRMATIONS."""
    threshold = head - config.CONFIRMATIONS
    for o in session.scalars(
        select(Order).where(
            Order.status == "detected",
            Order.block_number.isnot(None),
            Order.block_number <= threshold,
        )
    ).all():
        if not _receipt_still_valid(o):
            _mark_reorged(session, o)
            continue
        over = (o.paid_micro or 0) - o.expected_micro
        transition(
            session, o.id, ("detected",), "overpaid" if over > 0 else "confirmed"
        )
        log_event(
            session, "checkout", "order.confirmed", store_id=o.store_id, order_id=o.id
        )
        deliver(session, o)
    for o in session.scalars(
        select(Order).where(
            Order.status == "late_paid",
            Order.block_number.isnot(None),
            Order.block_number <= threshold,
        )
    ).all():
        if not _receipt_still_valid(o):
            _mark_reorged(session, o)
            continue
        deliver(session, o)


def sweep_tick():
    """One synchronous sweep: flip expiries, read head, sweep new blocks in
    <=101-block windows (capped per tick), match transfers, promote matured
    orders. Every RPC call is time-boxed; the cursor persists per window so a
    crash mid-tick resumes gap-free."""
    now = _now()
    with SessionLocal() as session:
        flip_expired(session)
        release_expired(session)
        session.commit()

    head = chain.block_number()
    safe_head = head - config.CONFIRMATIONS
    if safe_head < 0:
        return

    with SessionLocal() as session:
        cursor = _get_or_init_cursor(session, safe_head)
        session.commit()

    if cursor < safe_head:
        with SessionLocal() as session:
            addresses = _active_addresses(session, now)
        if not addresses:
            with SessionLocal() as session:
                _set_cursor(session, safe_head)
                session.commit()
        else:
            from_block = cursor + 1
            windows = 0
            while from_block <= safe_head and windows < config.SWEEP_MAX_WINDOWS:
                to_block = min(from_block + config.GETLOGS_MAX_SPAN, safe_head)
                logs = chain.get_logs(from_block, to_block, addresses)
                with SessionLocal() as session:
                    for log in logs:
                        _process_log(session, log, head, now)
                    _set_cursor(session, to_block)
                    session.commit()
                from_block = to_block + 1
                windows += 1

    with SessionLocal() as session:
        _promote_matured(session, head)
        session.commit()


async def sweeper_loop():
    """Background loop for the lifespan task. Each tick runs off-thread so it
    never blocks the event loop; a bad tick is logged and the loop continues."""
    logger.info("tilla sweeper loop started (interval=%ss)", config.SWEEP_INTERVAL)
    while True:
        try:
            await asyncio.to_thread(sweep_tick)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("sweep tick failed")
        await asyncio.sleep(config.SWEEP_INTERVAL)

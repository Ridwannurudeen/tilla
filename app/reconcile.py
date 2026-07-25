"""M8 aggr_deferred settle reconciliation poller — DORMANT by default.

A deferred (async) x402 settle can serve a 200 whose aggregated on-chain tx is NOT
yet confirmed: ``agentic.record_settlement`` then holds the order in the provisional
'settling' status, records the pending aggregated reference on ``Order.settle_ref``,
and logs ``agent_order.settle_pending`` — never claiming settlement without evidence.
This worker finalizes those orders. Each tick queries the facilitator's
``get_settle_status(settle_ref)`` and:

  - confirmed  (success + status 'success' + a real tx hash) -> flip settling ->
    delivered via ``agentic._finalize_settled`` (releasing goods, recording the tx);
  - failed     (status 'failed', or an un-successful non-pending status) -> void via
    ``agentic._void_settling`` (settling -> canceled, entitlement revoked);
  - pending / success-without-tx / ambiguous -> leave 'settling' untouched.

It NEVER delivers without a confirmed tx hash. Idempotent: both transitions are
conditional ``settling -> *`` UPDATEs, so a double-tick (or a race with the reaper) is
a no-op once the order has left 'settling'.

Modeled on ``app.attest``: its own flag gate (started ONLY under SWEEP_ENABLED AND
AGGR_DEFERRED_ENABLED AND OKX creds — so tests, which disable SWEEP_ENABLED, never
start it and never touch the network), a time-boxed off-loop tick, a bounded per-tick
batch, and a client factory that returns None (idles, zero network) when creds are
absent. The facilitator client is the synchronous OKX client, built lazily so the SDK
is imported only when the rail is live.
"""

from __future__ import annotations

import logging
import os
import time

import httpx
from sqlalchemy import select

from app import agentic, chain, checkout, config, payment
from app.db import SessionLocal
from app.models import Order

logger = logging.getLogger("tilla")

# ``time.monotonic()`` of the last chain scan that COMPLETED — every candidate pair
# walked all the way to head with no RPC failure. This is the reaper's evidence that "no
# settlement transfer found" is a real answer rather than an RPC blackout or a scan that
# never reached back far enough. Without it a blind eth_getLogs failure looks exactly like
# "this order was never paid", which is how the 2026-07-23 reap voided facilitator-
# accepted orders. Read by ``agentic._reap_tick``; mirrors ``checkout.LAST_HEAD_MONO``.
LAST_CHAIN_SCAN_MONO = 0.0
# Order ids whose (buyer -> merchant) pair walked ALL THE WAY to head on the last tick.
# The reaper may only void these: the scan is capped at RECONCILE_MAX_PER_TICK
# candidates, so a global "scan completed" stamp licensed voiding orders the scan had
# never looked at (a 21st order, or one excluded by the from_addr/network filters).
LAST_SCANNED_ORDER_IDS: set[str] = set()

# X Layer mines ~1 block/second (measured across the 2026-07-23 settlement window). The
# backlog anchor turns a settling order's age into a block depth with a generous margin so
# the estimate always lands EARLIER than that order's real creation block: an over-early
# lower bound only costs a few extra windows, while a late one would step straight over
# the settlement transfer and lose it for good.
BLOCKS_PER_SEC = 1.0
ANCHOR_SAFETY_FACTOR = 1.25
ANCHOR_SAFETY_BLOCKS = 600

# Per-pair scan position, so a pair working through a backlog makes FORWARD progress
# instead of re-walking the same RECONCILE_MAX_WINDOWS slices every tick:
#   key in _pair_cursors  -> mid-walk, resume at that block;
#   key in _pairs_at_head -> the walk reached head, so the trailing window applies again;
#   in neither            -> first sight: anchor with ``_scan_start``.
# Both are process-local by design: after a restart the anchor is simply recomputed and
# the backlog re-walked, so an arbitrarily old settlement stays reachable. No DB state —
# ``chain_cursor`` belongs to the checkout sweeper (per chain, not per pair).
_pair_cursors: dict[tuple[str, str], int] = {}
_pairs_at_head: set[tuple[str, str]] = set()


def _reset_state() -> None:
    """Drop the per-pair scan positions and the completed-scan stamp (tests; mirrors
    ``attest._reset_state``)."""
    global LAST_CHAIN_SCAN_MONO
    LAST_CHAIN_SCAN_MONO = 0.0
    LAST_SCANNED_ORDER_IDS.clear()
    _pair_cursors.clear()
    _pairs_at_head.clear()


def _default_client_factory():
    """Build the synchronous OKX facilitator client from the process creds, or None
    when any credential is absent — the dormant case (no client => no RPC, the SDK is
    never imported). Mirrors the app.mpp lazy-creds pattern."""
    api_key = os.getenv("OKX_API_KEY", "")
    secret = os.getenv("OKX_SECRET_KEY", "")
    passphrase = os.getenv("OKX_PASSPHRASE", "")
    if not (api_key and secret and passphrase):
        return None
    from x402.http import (
        OKXAuthConfig,
        OKXFacilitatorClientSync,
        OKXFacilitatorConfig,
    )

    return OKXFacilitatorClientSync(
        OKXFacilitatorConfig(
            auth=OKXAuthConfig(
                api_key=api_key, secret_key=secret, passphrase=passphrase
            ),
            base_url=os.getenv("OKX_BASE_URL", payment.DEFAULT_FACILITATOR_URL),
            sync_settle=True,
        )
    )


# Indirection so tests inject a fake client without touching the network (the
# app.attest _attester_factory pattern).
_client_factory = _default_client_factory


def _reconcile_one(session, client, order: Order) -> bool:
    """Query the settle status for one 'settling' order and finalize/void/leave it.
    Returns True when the order reached a terminal state this tick (acted). A transient
    error or a non-terminal status leaves the order 'settling' for a later tick (or,
    after 15 min, the reaper)."""
    try:
        resp = client.get_settle_status(order.settle_ref)
    except Exception:
        # Transient facilitator/RPC error — leave it settling, retry next tick.
        logger.warning("reconcile: settle-status query failed for order %s", order.id)
        return False
    tx = resp.transaction or None
    if resp.success and resp.status == "success" and tx is not None:
        # Confirmed on-chain — release goods and record the aggregated tx. No agent id
        # is threaded through the async reconciliation, so the settled-event payload is
        # the byte-identical non-tiered path (the price was already locked at buy time).
        if agentic._finalize_settled(session, order, tx, None):
            # Stamp settle_ref too. _finalize_settled writes only tx_hash, and the
            # chain scan's consume set reads both columns — an unstamped settlement
            # left this tx eligible to finalize a second order on a later tick.
            order.settle_ref = tx
            session.commit()
            return True
        return False
    if resp.status == "failed":
        # DEFINITIVE failure only — void the provisional order. A missing/unknown
        # status (None) or success=False without an explicit "failed" is NOT
        # treated as definitive (a facilitator that omits status on a transient
        # error must never trigger a paid-order void): leave it settling, retry.
        if agentic._void_settling(
            session, order, "agent_order.settle_reconcile_failed"
        ):
            session.commit()
            return True
        return False
    # pending, success-without-tx, or ambiguous — never deliver without a confirmed tx.
    return False


def _facilitator_transfers(
    cfg, from_addr: str, pay_to: str, start: int, head: int
) -> tuple[list[tuple], int]:
    """Every CONFIRMED USDT0 Transfer from ``from_addr`` to ``pay_to`` at or after
    ``start`` whose transaction was submitted by the OKX facilitator relayer, as
    ``(block_number, log_index, value, tx_hash)`` sorted in chain order, plus the NEXT
    block to scan. This is the real settlement evidence: OKX's ``/settle`` returns no tx
    hash, but ~30s later its relayer submits a tx whose USDT0 Transfer log moves the funds
    buyer -> merchant (batched — one transfer of the SUM per group of orders). Pages
    eth_getLogs in <= GETLOGS_MAX_SPAN-block slices (the X Layer 101-block cap), bounded
    to RECONCILE_MAX_WINDOWS slices per call — a caller that resumes from the returned
    cursor therefore walks FORWARD through a long backlog instead of re-scanning the same
    slices every tick. A returned cursor past ``head`` means the walk covered the whole
    range. Raises on RPC error so the caller leaves the pair settling and retries the SAME
    slice next tick (fail-closed — never a false settlement, never a skipped block)."""
    want_from = from_addr.lower()
    transfers: list[tuple] = []
    receipts: dict[str, dict | None] = {}
    cursor = start
    windows = 0
    while cursor <= head and windows < config.RECONCILE_MAX_WINDOWS:
        to_block = min(cursor + config.GETLOGS_MAX_SPAN, head)
        for lg in chain.get_logs(cfg, cursor, to_block, [pay_to]):
            d = chain.decode_transfer_log(lg)
            if d["from"] != want_from:
                continue
            tx_hash = d["tx_hash"]
            if tx_hash not in receipts:
                receipts[tx_hash] = chain.get_transaction_receipt(cfg, tx_hash)
            receipt = receipts[tx_hash]
            if receipt is None:
                continue
            if str(receipt.get("status", "")).lower() != "0x1":
                continue
            # The callee must be a KNOWN settlement contract. Checked against a set,
            # not one address: OKX settles through several (aggregated vs period), and
            # pinning a single one made real settlements invisible — which is worse
            # than not checking, because the scan then reports "clean" and licenses the
            # reaper to void an order the buyer actually paid.
            if (
                receipt.get("to", "") or ""
            ).lower() not in config.AGGR_FACILITATOR_CALLEES:
                continue
            transfers.append((d["block_number"], d["log_index"], d["value"], tx_hash))
        cursor = to_block + 1
        windows += 1
    transfers.sort()
    return transfers, cursor


def _scan_start(orders, head: int) -> int:
    """Where a pair with no stored cursor begins its walk: the usual trailing
    ``head - AGGR_SETTLE_LOOKBACK_BLOCKS`` window, or — when the pair's OLDEST 'settling'
    order predates that window — an estimate of that order's creation block, so a
    settlement older than the window is still reachable (walked forward over the next few
    ticks). Head-anchored scanning alone left such an order settling forever: the reaper
    correctly refuses to void it, and the reconciler could never see far enough back."""
    trailing = max(head - config.AGGR_SETTLE_LOOKBACK_BLOCKS, 0)
    oldest = min(checkout._naive(o.created_at) for o in orders)
    elapsed = max((checkout._now() - oldest).total_seconds(), 0.0)
    depth = int(elapsed * BLOCKS_PER_SEC * ANCHOR_SAFETY_FACTOR) + ANCHOR_SAFETY_BLOCKS
    return min(trailing, max(head - depth, 0))


def _reconcile_chain_pair(
    session, cfg, from_addr, pay_to, orders, head
) -> tuple[int, bool]:
    """Finalize the oldest 'settling' aggr_deferred orders for one (from_addr ->
    pay_to) pair against the facilitator-relayed settlement transfers, ACCUMULATING —
    a batched settlement pays the SUM of several orders in one transfer, so each
    transfer's value is walked across the oldest orders it covers. Consumes each
    settlement tx once (a tx already recorded as a settle_ref on this pair is skipped)
    so a re-scanned transfer can never double-settle a later order. Records the settle
    tx on each finalized order (tx_hash via ``_finalize_settled`` + settle_ref as the
    consume cursor) and commits the pair atomically.

    Returns ``(finalized, reached_head)``. ``reached_head`` is False while the pair is
    still walking a backlog: a PARTIAL walk has not proven "no settlement transfer", so
    the reaper must not treat it as a clean scan. An RPC error PROPAGATES so the caller
    can tell "scanned, found nothing" apart from "could not scan" — the pair is left
    settling either way (fail-closed)."""
    key = (from_addr, pay_to)
    if key in _pair_cursors:
        start = _pair_cursors[key]  # mid-backlog: resume, don't re-walk
    elif key in _pairs_at_head:
        start = max(head - config.AGGR_SETTLE_LOOKBACK_BLOCKS, 0)
    else:
        start = _scan_start(orders, head)
    transfers, cursor = _facilitator_transfers(cfg, from_addr, pay_to, start, head)
    # A settlement tx is consumed if it appears on EITHER column: the three settle
    # paths do not agree on where they put it. _finalize_settled writes only
    # tx_hash; only this chain-scan path also stamps settle_ref; subscriptions put
    # the facilitator subId on settle_ref and the real tx on tx_hash. Reading just
    # settle_ref let an already-credited transfer finalize a SECOND order.
    consumed = {
        ref
        for ref in session.scalars(
            select(Order.settle_ref).where(
                Order.channel == "agent",
                Order.from_addr == from_addr,
                Order.pay_to == pay_to,
                Order.settle_ref.is_not(None),
            )
        ).all()
    } | {
        tx
        for tx in session.scalars(
            select(Order.tx_hash).where(
                Order.channel == "agent",
                Order.from_addr == from_addr,
                Order.pay_to == pay_to,
                Order.tx_hash.is_not(None),
            )
        ).all()
    }
    pending = sorted(orders, key=lambda o: checkout._naive(o.created_at))
    finalized = 0
    idx = 0
    for _block, _log_index, value, tx_hash in transfers:
        if tx_hash in consumed:
            continue
        consumed.add(tx_hash)
        remaining = value
        while idx < len(pending) and pending[idx].expected_micro <= remaining:
            order = pending[idx]
            idx += 1
            remaining -= order.expected_micro
            if agentic._finalize_settled(session, order, tx_hash, None):
                order.settle_ref = tx_hash
                finalized += 1
    if finalized:
        session.commit()
    reached_head = cursor > head
    if reached_head:
        _pair_cursors.pop(key, None)
        _pairs_at_head.add(key)
    else:
        _pair_cursors[key] = cursor
    return finalized, reached_head


def reconcile_chain_tick() -> int:
    """Chain-based settlement detection for 'settling' aggr_deferred orders whose
    facilitator settle returned NO tx hash (the live OKX case): find the on-chain
    USDT0 Transfer(s) the facilitator relayer submitted from the buyer to the merchant
    and finalize the orders they paid for (batched: one transfer -> the sum of several
    orders). DORMANT unless AGGR_DEFERRED_ENABLED. Bounded to RECONCILE_MAX_PER_TICK
    candidate orders per tick. Returns the number finalized. Fail-closed: any RPC error
    leaves the affected orders settling for a later tick.

    Stamps ``LAST_CHAIN_SCAN_MONO`` only when EVERY candidate pair walked all the way to
    head with no RPC failure — a pair still catching up through a backlog does NOT count.
    That stamp is the reaper's licence to treat "no transfer found" as a real answer (see
    ``agentic._reap_tick``)."""
    global LAST_CHAIN_SCAN_MONO
    if not config.AGGR_DEFERRED_ENABLED:
        return 0
    cfg = payment.CANONICAL_CHAIN
    with SessionLocal() as session:
        orders = session.scalars(
            select(Order)
            .where(
                Order.channel == "agent",
                Order.status == "settling",
                Order.settle_ref.is_(None),
                Order.from_addr.is_not(None),
                Order.network == cfg.caip2,
            )
            .order_by(Order.created_at)
            .limit(config.RECONCILE_MAX_PER_TICK)
        ).all()
        if not orders:
            # Nothing to look for: a scan with no candidates is a complete scan.
            _pair_cursors.clear()
            _pairs_at_head.clear()
            LAST_SCANNED_ORDER_IDS.clear()
            LAST_CHAIN_SCAN_MONO = time.monotonic()
            return 0
        try:
            head = chain.block_number(cfg)
        except (chain.ChainError, httpx.HTTPError, ValueError):
            logger.warning("reconcile: chain head unavailable")
            return 0
        pairs: dict[tuple[str, str], list] = {}
        for o in orders:
            pairs.setdefault((o.from_addr, o.pay_to), []).append(o)
        # Pairs with nothing settling any more can't be scanned; drop their positions so
        # the maps stay bounded over a long-running process.
        for stale in set(_pair_cursors) - set(pairs):
            del _pair_cursors[stale]
        _pairs_at_head.intersection_update(pairs)
        acted = 0
        scanned_all = True
        scanned_ids: set[str] = set()
        for (from_addr, pay_to), pair_orders in pairs.items():
            try:
                finalized, reached_head = _reconcile_chain_pair(
                    session, cfg, from_addr, pay_to, pair_orders, head
                )
                acted += finalized
                scanned_all = scanned_all and reached_head
                if reached_head:
                    # Only THIS pair's orders were proven unpaid by a completed walk.
                    scanned_ids.update(o.id for o in pair_orders)
            except (chain.ChainError, httpx.HTTPError, ValueError):
                logger.warning(
                    "reconcile: chain scan failed for %s -> %s", from_addr, pay_to
                )
                scanned_all = False
        LAST_SCANNED_ORDER_IDS.clear()
        LAST_SCANNED_ORDER_IDS.update(scanned_ids)
        if scanned_all:
            LAST_CHAIN_SCAN_MONO = time.monotonic()
    return acted


def chain_reachable() -> bool:
    """True iff the canonical chain RPC answers a head query. The reaper uses this to
    fail-closed: a live aggr_deferred settle carries NO tx hash, so those orders sit
    'settling' (settle_ref NULL) until this module finds the facilitator transfer on
    chain — during an RPC outage the reconciler is blind, and the time-based reaper
    (which needs no RPC) must NOT void a genuinely-paid-but-slow order."""
    try:
        chain.block_number(payment.CANONICAL_CHAIN)
        return True
    except (chain.ChainError, httpx.HTTPError, ValueError):
        return False


def reconcile_tick() -> int:
    """One reconciliation pass. Runs the CHAIN settlement detection (the real
    aggr_deferred path — the facilitator gives no tx ref, so settlement is found
    on-chain) then, when OKX creds are configured, the get_settle_status path for any
    order that DID capture a pending aggregated ref. Reconciles up to
    RECONCILE_MAX_PER_TICK settling aggr_deferred orders per path. Returns the number
    of orders that reached a terminal state."""
    acted = reconcile_chain_tick()
    client = _client_factory()
    if client is None:
        return acted  # dormant / no creds — no facilitator RPC
    with SessionLocal() as session:
        orders = session.scalars(
            select(Order)
            .where(
                Order.channel == "agent",
                Order.status == "settling",
                Order.settle_ref.is_not(None),
            )
            .limit(config.RECONCILE_MAX_PER_TICK)
        ).all()
        for order in orders:
            if _reconcile_one(session, client, order):
                acted += 1
    return acted


async def reconcile_loop() -> None:
    """Background loop (lifespan task) mirroring attest_loop/sweeper_loop: each tick
    runs off-thread; a bad tick is logged and the loop continues; idle when nothing is
    pending. Started ONLY when SWEEP_ENABLED AND AGGR_DEFERRED_ENABLED AND OKX creds are
    all set."""
    import asyncio

    logger.info(
        "tilla reconcile loop started (interval=%ss)", config.RECONCILE_INTERVAL
    )
    while True:
        try:
            await asyncio.to_thread(reconcile_tick)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("reconcile tick failed")
        await asyncio.sleep(config.RECONCILE_INTERVAL)

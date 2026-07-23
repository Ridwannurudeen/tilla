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

import httpx
from sqlalchemy import select

from app import agentic, chain, checkout, config, payment
from app.db import SessionLocal
from app.models import Order

logger = logging.getLogger("tilla")


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


def _facilitator_transfers(cfg, from_addr: str, pay_to: str, head: int) -> list[tuple]:
    """Every CONFIRMED USDT0 Transfer from ``from_addr`` to ``pay_to`` in the recent
    lookback window whose transaction was submitted by the OKX facilitator relayer,
    as ``(block_number, log_index, value, tx_hash)`` sorted in chain order. This is
    the real settlement evidence: OKX's ``/settle`` returns no tx hash, but ~30s later
    its relayer submits a tx whose USDT0 Transfer log moves the funds buyer -> merchant
    (batched — one transfer of the SUM per group of orders). Pages the eth_getLogs
    window in <= GETLOGS_MAX_SPAN-block slices (the X Layer 101-block cap), bounded to
    RECONCILE_MAX_WINDOWS slices. Raises on RPC error so the caller leaves the pair
    settling and retries next tick (fail-closed — never a false settlement)."""
    want_from = from_addr.lower()
    transfers: list[tuple] = []
    receipts: dict[str, dict | None] = {}
    cursor = max(head - config.AGGR_SETTLE_LOOKBACK_BLOCKS, 0)
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
            if (receipt.get("to", "") or "").lower() != config.AGGR_FACILITATOR_RELAYER:
                continue
            transfers.append((d["block_number"], d["log_index"], d["value"], tx_hash))
        cursor = to_block + 1
        windows += 1
    transfers.sort()
    return transfers


def _reconcile_chain_pair(session, cfg, from_addr, pay_to, orders, head) -> int:
    """Finalize the oldest 'settling' aggr_deferred orders for one (from_addr ->
    pay_to) pair against the facilitator-relayed settlement transfers, ACCUMULATING —
    a batched settlement pays the SUM of several orders in one transfer, so each
    transfer's value is walked across the oldest orders it covers. Consumes each
    settlement tx once (a tx already recorded as a settle_ref on this pair is skipped)
    so a re-scanned transfer can never double-settle a later order. Records the settle
    tx on each finalized order (tx_hash via ``_finalize_settled`` + settle_ref as the
    consume cursor) and commits the pair atomically. Returns the number finalized;
    fail-closed (0, left settling) on an RPC error."""
    try:
        transfers = _facilitator_transfers(cfg, from_addr, pay_to, head)
    except (chain.ChainError, httpx.HTTPError, ValueError):
        logger.warning("reconcile: chain scan failed for %s -> %s", from_addr, pay_to)
        return 0
    consumed = set(
        session.scalars(
            select(Order.settle_ref).where(
                Order.channel == "agent",
                Order.from_addr == from_addr,
                Order.pay_to == pay_to,
                Order.settle_ref.is_not(None),
            )
        ).all()
    )
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
    return finalized


def reconcile_chain_tick() -> int:
    """Chain-based settlement detection for 'settling' aggr_deferred orders whose
    facilitator settle returned NO tx hash (the live OKX case): find the on-chain
    USDT0 Transfer(s) the facilitator relayer submitted from the buyer to the merchant
    and finalize the orders they paid for (batched: one transfer -> the sum of several
    orders). DORMANT unless AGGR_DEFERRED_ENABLED. Bounded to RECONCILE_MAX_PER_TICK
    candidate orders per tick. Returns the number finalized. Fail-closed: any RPC error
    leaves the affected orders settling for a later tick."""
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
            return 0
        try:
            head = chain.block_number(cfg)
        except (chain.ChainError, httpx.HTTPError, ValueError):
            logger.warning("reconcile: chain head unavailable")
            return 0
        pairs: dict[tuple[str, str], list] = {}
        for o in orders:
            pairs.setdefault((o.from_addr, o.pay_to), []).append(o)
        acted = 0
        for (from_addr, pay_to), pair_orders in pairs.items():
            acted += _reconcile_chain_pair(
                session, cfg, from_addr, pay_to, pair_orders, head
            )
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

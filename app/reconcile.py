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

from sqlalchemy import select

from app import agentic, config, payment
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
    if resp.status == "failed" or (not resp.success and resp.status != "pending"):
        # Definitive failure — void the provisional order (never on a pending status).
        if agentic._void_settling(
            session, order, "agent_order.settle_reconcile_failed"
        ):
            session.commit()
            return True
        return False
    # pending, success-without-tx, or ambiguous — never deliver without a confirmed tx.
    return False


def reconcile_tick() -> int:
    """One reconciliation pass. Idles (zero network) when no client is configured.
    Reconciles up to RECONCILE_MAX_PER_TICK settling aggr_deferred orders. Returns the
    number of orders that reached a terminal state."""
    client = _client_factory()
    if client is None:
        return 0  # dormant / no creds — no RPC
    acted = 0
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

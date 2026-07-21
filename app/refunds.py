"""M9 non-custodial refunds: verify a merchant-sent USDT0 refund on X Layer and
apply it, mirroring :func:`app.checkout.verify_txhash`.

Tilla never holds or moves funds. The merchant sends USDT0 back to the buyer from
their OWN wallet and submits the tx hash; this module is verify-only. A refund is
credited to exactly one order ever (UNIQUE(tx_hash, log_index) on ``refunds``,
the ProcessedTransfer pattern), the amount is server-computed and matched exactly,
and a refund transfer that predates the payment block is rejected.
"""

from __future__ import annotations

import logging

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import affiliates, chain, config, delivery, payment, webhooks
from app.checkout import transition
from app.models import Order, Refund, Store, log_event

logger = logging.getLogger("tilla")

# Full-refund preconditions: any state where the buyer paid (or partly paid) and
# is owed their money back. 'detected' is excluded — the payment is not depth-final
# yet, so the merchant waits. 'canceled' with paid_micro==0 self-rejects on the
# amount-due check (nothing to refund). This is the M3-deferred underpaid/overpaid
# resolution, layered on via the existing conditional-UPDATE transition().
REFUNDABLE = (
    "underpaid",
    "expired",
    "canceled",
    "confirmed",
    "overpaid",
    "late_paid",
    "delivered",
    "paid",
)
# Overage-only refunds return the surplus while the buyer keeps the goods, so the
# order must currently be delivered.
OVERAGE_STATES = ("delivered", "paid")


class RefundError(Exception):
    """A refund could not be verified/applied. ``status_code`` maps to the HTTP
    response (400 verification failure, 409 conflict/idempotency)."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _amount_due(order: Order, kind: str) -> int:
    """Server-computed amount the refund tx must match EXACTLY — never client
    supplied. full → paid_micro − refunded_micro; overage → overpaid_micro not yet
    refunded (only on a delivered order)."""
    if kind == "overage":
        if order.status not in OVERAGE_STATES:
            raise RefundError(409, "overage refund is only valid on a delivered order")
        due = (order.overpaid_micro or 0) - (order.refunded_micro or 0)
        if (order.overpaid_micro or 0) <= 0 or due <= 0:
            raise RefundError(409, "no overage left to refund")
        return due
    # full
    if order.status not in REFUNDABLE:
        raise RefundError(409, f"order is not refundable in status {order.status}")
    due = (order.paid_micro or 0) - (order.refunded_micro or 0)
    if due <= 0:
        raise RefundError(409, "order has nothing left to refund")
    return due


def apply_refund(
    session: Session, order: Order, store: Store, tx_hash: str, kind: str
) -> dict:
    """Verify `tx_hash` as a USDT0 refund from ``store.pay_to`` to
    ``order.from_addr`` and apply it, all in the caller's transaction (the route
    commits). Returns a small result dict. Raises :class:`RefundError` (400/409) or
    lets chain errors propagate (route → 502). Idempotent: resubmitting the same
    refund tx for the same order is a no-op returning current state; a tx already
    claimed by another order 409s."""
    if kind not in ("full", "overage"):
        raise RefundError(400, "kind must be 'full' or 'overage'")
    if not order.from_addr:
        raise RefundError(409, "order has no on-chain buyer address to refund")

    # Pin refund verification to the order's own chain (its RPC + asset only),
    # mirroring checkout.verify_txhash: a refund tx on any other chain is never
    # fetched and a non-``cfg.asset`` transfer is skipped.
    cfg = payment.chain_for(order.network)
    receipt = chain.get_transaction_receipt(cfg, tx_hash)
    if receipt is None:
        raise RefundError(400, "transaction not found")
    if str(receipt.get("status", "")).lower() != "0x1":
        raise RefundError(400, "transaction did not succeed on-chain")

    want_from = chain.pad_address(store.pay_to).lower()
    want_to = chain.pad_address(order.from_addr).lower()
    matched_any = False
    new_logs: list[dict] = []
    total = 0
    for lg in receipt.get("logs", []):
        if (lg.get("address", "") or "").lower() != cfg.asset:
            continue
        topics = lg.get("topics", [])
        if len(topics) < 3 or topics[0].lower() != config.TRANSFER_TOPIC:
            continue
        # sender must be the store's receive wallet; recipient must be the buyer
        if topics[1].lower() != want_from or topics[2].lower() != want_to:
            continue
        matched_any = True
        d = chain.decode_transfer_log(lg)
        if order.block_number is not None and d["block_number"] < order.block_number:
            # A refund cannot predate the payment — the analogue of M3's
            # created_block floor. Reject so a historical transfer can't be farmed.
            raise RefundError(400, "refund transfer predates the payment")
        existing = session.scalar(
            select(Refund).where(
                Refund.tx_hash == d["tx_hash"], Refund.log_index == d["log_index"]
            )
        )
        if existing is not None:
            if existing.order_id != order.id:
                raise RefundError(409, "refund tx already claimed by another order")
            continue  # already applied to this order — idempotent
        total += d["value"]
        new_logs.append(d)

    if not matched_any:
        raise RefundError(
            400, "no USDT0 refund from the store wallet to the buyer in that tx"
        )
    if not new_logs:
        # everything already applied to this order — idempotent no-op
        return _result(order, applied=0)

    due = _amount_due(order, kind)
    if total != due:
        raise RefundError(400, "refund amount does not match the amount due")

    for d in new_logs:
        try:
            with session.begin_nested():
                session.add(
                    Refund(
                        order_id=order.id,
                        kind=kind,
                        tx_hash=d["tx_hash"],
                        log_index=d["log_index"],
                        amount_micro=d["value"],
                        block_number=d["block_number"],
                    )
                )
        except IntegrityError as exc:
            # a concurrent submission claimed this exact transfer first
            raise RefundError(409, "refund tx already claimed") from exc

    new_refunded = (order.refunded_micro or 0) + total
    if kind == "full":
        # race-proof conditional UPDATE layered on the M3 machine; a lost race
        # (double-refund) returns False and we reconcile to current state.
        moved = transition(
            session, order.id, REFUNDABLE, "refunded", refunded_micro=new_refunded
        )
        if not moved:
            raise RefundError(409, "order is no longer refundable")
        delivery.revoke_entitlement(session, order)
        # M13: a full refund voids the affiliate accrual (accrued -> void) in the same
        # transaction — no rev-share is owed on a reversed sale. A payout already made
        # on-chain (status 'paid') is left untouched (the money already left the
        # operator's wallet); void_accrual only touches a still-'accrued' row.
        affiliates.void_accrual(session, order)
        log_event(
            session,
            "refunds",
            "order.refunded",
            store_id=order.store_id,
            order_id=order.id,
            data={"kind": "full", "amount_micro": total},
        )
    else:  # overage: keep delivered status, just record the surplus returned
        # Race-proof the read-modify-write: the compare-and-set on refunded_micro
        # (mirroring the full-refund transition()) means two concurrent overage
        # submissions with different tx hashes can't both land — the loser's stale
        # base fails the WHERE and 409s, so the refunds ledger and refunded_micro
        # never diverge.
        moved = session.execute(
            update(Order)
            .where(
                Order.id == order.id,
                Order.status.in_(OVERAGE_STATES),
                Order.refunded_micro == (order.refunded_micro or 0),
            )
            .values(refunded_micro=new_refunded)
        ).rowcount
        if moved != 1:
            raise RefundError(409, "order is no longer eligible for an overage refund")
        log_event(
            session,
            "refunds",
            "order.overage_refunded",
            store_id=order.store_id,
            order_id=order.id,
            data={"kind": "overage", "amount_micro": total},
        )

    session.refresh(order)
    webhooks.enqueue(
        session,
        store.merchant_id,
        "order.refunded" if kind == "full" else "order.overage_refunded",
        order,
        extra={"kind": kind, "refund_amount_micro": total},
    )
    return _result(order, applied=total)


def _result(order: Order, applied: int) -> dict:
    return {
        "order_id": order.id,
        "status": order.status,
        "applied_micro": applied,
        "refunded_micro": order.refunded_micro,
    }

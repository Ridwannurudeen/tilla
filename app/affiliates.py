"""M13 affiliate rev-share: attribution capture, the accrual ledger, and the
verify-and-record payout path.

The affiliate identity is a bare EVM address — the referring agent's payout wallet
(no signup friction). Attribution is captured first-write-wins on the Order at
creation and is immutable afterwards. Accrual is a PURE DB NUMBER written exactly at
the settled/delivered seam: no code here (or anywhere) can move funds. A payout is a
manual on-chain USDT0 send the merchant/operator makes from their OWN wallet, which
this module then verifies and records — byte-identical philosophy to the M9
non-custodial refunds (:mod:`app.refunds`). There is NO signer, NO private key, and
NO fund-moving call anywhere in this file.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import chain, config, payment
from app.models import (
    AffiliateAccrual,
    AffiliatePayout,
    Order,
    Store,
    log_event,
)

logger = logging.getLogger("tilla")

_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_ZERO_ADDRESS = "0x" + "0" * 40


class RefRejected(ValueError):
    """A supplied affiliate ref failed validation (bad shape / zero address)."""


class PayoutError(Exception):
    """A payout could not be verified/recorded. ``status_code`` maps to the HTTP
    response (400 verification failure, 409 conflict/idempotency/nothing-owed)."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# ------------------------------------------------------------ capture validation
def normalize_ref(value: str | None) -> str | None:
    """Validate + normalize a capture-path ref to a lowercased EVM address, or None
    when absent/empty. Raises :class:`RefRejected` on a malformed or zero address —
    every capture path (web body, agent query, MCP arg, ACP affiliate object) runs
    through this one gate so attribution can never be a non-address or 0x0…0."""
    if value is None:
        return None
    v = value.strip()
    if not v:
        return None
    if not _EVM_ADDRESS.fullmatch(v):
        raise RefRejected("ref must be a 0x-prefixed 20-byte EVM address")
    low = v.lower()
    if low == _ZERO_ADDRESS:
        raise RefRejected("ref must not be the zero address")
    return low


# ------------------------------------------------------------------ accrual
def accrue(session: Session, order: Order, store: Store) -> int | None:
    """Write the rev-share ledger row for a referred, settled/delivered order. Called
    inside the caller's transaction at the delivered seam — ``checkout.deliver`` for
    web orders and ``agentic.record_settlement`` for agent orders (so a voided/reaped
    agent order NEVER accrues). Idempotent via UNIQUE(order_id) (the Entitlement
    begin_nested pattern). Returns the accrued micro-amount, or None when there is no
    ref, the sale self-refers, or the computed accrual rounds to zero.

    SELF-REFERRAL GUARD (from_addr is known at this seam): a referrer equal to the
    buyer's on-chain payer OR the store's receive wallet earns NO accrual and logs
    'affiliate.self_referral_rejected' — wash-trade farming inflates nothing."""
    referrer = order.referrer_addr
    if not referrer:
        return None
    buyer = (order.from_addr or "").lower()
    pay_to = (store.pay_to or "").lower()
    if referrer == buyer or referrer == pay_to:
        log_event(
            session,
            "affiliates",
            "affiliate.self_referral_rejected",
            store_id=order.store_id,
            order_id=order.id,
            data={"referrer": referrer},
        )
        return None
    basis = int(order.expected_micro or 0)
    rate = int(config.TILLA_AFFILIATE_BPS)
    accrued = basis * rate // 10000
    if accrued <= 0:
        return None
    try:
        with session.begin_nested():
            session.add(
                AffiliateAccrual(
                    order_id=order.id,
                    store_id=store.id,
                    merchant_id=store.merchant_id,
                    referrer_addr=referrer,
                    basis_micro=basis,
                    rate_bps=rate,
                    accrued_micro=accrued,
                    status="accrued",
                )
            )
    except IntegrityError:
        # already accrued for this order (idempotent replay)
        return None
    log_event(
        session,
        "affiliates",
        "affiliate.accrued",
        store_id=order.store_id,
        order_id=order.id,
        data={"referrer": referrer, "accrued_micro": accrued},
    )
    return accrued


def void_accrual(session: Session, order: Order) -> bool:
    """Void the still-accrued ledger row for `order` (a full M9 refund calls this in
    its transaction). Only an 'accrued' row is voided — a 'paid' accrual (the operator
    already sent that USDT0 on-chain) is left alone, since the money has left the
    wallet and a refund cannot claw it back. Idempotent."""
    row = session.scalar(
        select(AffiliateAccrual).where(
            AffiliateAccrual.order_id == order.id,
            AffiliateAccrual.status == "accrued",
        )
    )
    if row is None:
        return False
    row.status = "void"
    log_event(
        session,
        "affiliates",
        "affiliate.voided",
        store_id=order.store_id,
        order_id=order.id,
        data={"accrued_micro": row.accrued_micro},
    )
    return True


# ----------------------------------------------------------- payout verify+record
def owed_micro(session: Session, merchant_id: int, referrer: str) -> int:
    """The outstanding (status='accrued') micro-amount this merchant owes `referrer`."""
    return int(
        session.scalar(
            select(func.coalesce(func.sum(AffiliateAccrual.accrued_micro), 0)).where(
                AffiliateAccrual.merchant_id == merchant_id,
                AffiliateAccrual.referrer_addr == referrer,
                AffiliateAccrual.status == "accrued",
            )
        )
        or 0
    )


def verify_and_record_payout(
    session: Session, merchant_id: int, referrer: str, tx_hash: str
) -> dict:
    """Verify `tx_hash` as a USDT0 transfer to `referrer` and record it as a payout,
    then flip covered accruals to 'paid' — all in the caller's transaction (the route
    commits). VERIFY-AND-RECORD ONLY: Tilla constructs no signer and moves no funds;
    the operator already sent the transfer from their own wallet. Pins the USDT0
    contract, the exact recipient (`referrer`), status 1, and caps the applied total
    at the outstanding owed balance. A transfer already consumed by another payout row
    409s (UNIQUE(tx_hash, log_index) — the Refund idempotency pattern)."""
    owed = owed_micro(session, merchant_id, referrer)
    if owed <= 0:
        raise PayoutError(409, "nothing is currently owed to this referrer")

    # Payouts settle on the canonical USDT0 ledger (INV-2); pin the receipt lookup
    # and asset filter to it via the registry.
    cfg = payment.CANONICAL_CHAIN
    receipt = chain.get_transaction_receipt(cfg, tx_hash)
    if receipt is None:
        raise PayoutError(400, "transaction not found")
    if str(receipt.get("status", "")).lower() != "0x1":
        raise PayoutError(400, "transaction did not succeed on-chain")

    want_to = chain.pad_address(referrer).lower()
    matched_any = False
    new_logs: list[dict] = []
    total = 0
    for lg in receipt.get("logs", []):
        if (lg.get("address", "") or "").lower() != cfg.asset:
            continue
        topics = lg.get("topics", [])
        if len(topics) < 3 or topics[0].lower() != config.TRANSFER_TOPIC:
            continue
        if topics[2].lower() != want_to:
            continue
        matched_any = True
        d = chain.decode_transfer_log(lg)
        existing = session.scalar(
            select(AffiliatePayout).where(
                AffiliatePayout.tx_hash == d["tx_hash"],
                AffiliatePayout.log_index == d["log_index"],
            )
        )
        if existing is not None:
            raise PayoutError(409, "payout transfer already recorded")
        total += d["value"]
        new_logs.append(d)

    if not matched_any:
        raise PayoutError(400, "no USDT0 transfer to the referrer in that transaction")
    if not new_logs or total <= 0:
        raise PayoutError(400, "payout transfers sum to zero")
    if total > owed:
        raise PayoutError(400, "payout exceeds the outstanding owed balance")

    payout_rows: list[AffiliatePayout] = []
    for d in new_logs:
        try:
            with session.begin_nested():
                row = AffiliatePayout(
                    merchant_id=merchant_id,
                    referrer_addr=referrer,
                    tx_hash=d["tx_hash"],
                    log_index=d["log_index"],
                    amount_micro=d["value"],
                    block_number=d["block_number"],
                )
                session.add(row)
                session.flush()
                payout_rows.append(row)
        except IntegrityError as exc:
            raise PayoutError(409, "payout transfer already recorded") from exc

    primary_payout_id = payout_rows[0].id
    remaining = total
    covered = 0
    for acc in session.scalars(
        select(AffiliateAccrual)
        .where(
            AffiliateAccrual.merchant_id == merchant_id,
            AffiliateAccrual.referrer_addr == referrer,
            AffiliateAccrual.status == "accrued",
        )
        .order_by(AffiliateAccrual.id)
    ):
        if acc.accrued_micro <= remaining:
            acc.status = "paid"
            acc.payout_id = primary_payout_id
            remaining -= acc.accrued_micro
            covered += acc.accrued_micro
    log_event(
        session,
        "affiliates",
        "affiliate.payout_recorded",
        store_id=None,
        data={
            "referrer": referrer,
            "merchant_id": merchant_id,
            "recorded_micro": total,
            "covered_micro": covered,
        },
    )
    return {
        "referrer": referrer,
        "recorded_micro": total,
        "covered_micro": covered,
        "owed_before_micro": owed,
        "owed_after_micro": owed - covered,
    }


# ---------------------------------------------------------------- read surfaces
def _tx_url(tx_hash: str | None) -> str | None:
    return config.OKLINK_TX_BASE + tx_hash if tx_hash else None


def merchant_summary(session: Session, merchant_id: int) -> dict:
    """Per-referrer accrued/void/paid totals + per-order rows for the merchant's
    dashboard. Order-level OKLink links point at the sale tx and, once paid, the
    on-chain payout tx. No buyer email / merchant wallet is emitted."""
    accruals = session.scalars(
        select(AffiliateAccrual)
        .where(AffiliateAccrual.merchant_id == merchant_id)
        .order_by(AffiliateAccrual.id.desc())
    ).all()
    order_ids = [a.order_id for a in accruals]
    payout_ids = [a.payout_id for a in accruals if a.payout_id is not None]
    tx_by_order: dict[str, str | None] = {}
    slug_by_store: dict[int, str] = {}
    if order_ids:
        for oid, tx, sid in session.execute(
            select(Order.id, Order.tx_hash, Order.store_id).where(
                Order.id.in_(order_ids)
            )
        ).all():
            tx_by_order[oid] = tx
        store_ids = {a.store_id for a in accruals}
        for sid, slug in session.execute(
            select(Store.id, Store.slug).where(Store.id.in_(store_ids))
        ).all():
            slug_by_store[sid] = slug
    payout_tx: dict[int, str] = {}
    if payout_ids:
        for pid, tx in session.execute(
            select(AffiliatePayout.id, AffiliatePayout.tx_hash).where(
                AffiliatePayout.id.in_(payout_ids)
            )
        ).all():
            payout_tx[pid] = tx

    referrers: dict[str, dict] = {}
    orders_out: list[dict] = []
    for a in accruals:
        agg = referrers.setdefault(
            a.referrer_addr,
            {
                "referrer": a.referrer_addr,
                "accrued_micro": 0,
                "void_micro": 0,
                "paid_micro": 0,
            },
        )
        agg[a.status + "_micro"] += a.accrued_micro
        p_tx = payout_tx.get(a.payout_id) if a.payout_id else None
        orders_out.append(
            {
                "order_id": a.order_id,
                "store_slug": slug_by_store.get(a.store_id),
                "referrer": a.referrer_addr,
                "status": a.status,
                "basis_micro": a.basis_micro,
                "rate_bps": a.rate_bps,
                "accrued_micro": a.accrued_micro,
                "sale_tx_url": _tx_url(tx_by_order.get(a.order_id)),
                "payout_tx_url": _tx_url(p_tx),
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
        )
    return {
        "referrers": list(referrers.values()),
        "orders": orders_out,
        "owed_micro": sum(r["accrued_micro"] for r in referrers.values()),
    }


def merchant_owed_micro(session: Session, merchant_id: int) -> int:
    """Total outstanding (status='accrued') the merchant owes across all referrers —
    the 'affiliate_owed_micro' aggregate the merchant summary surfaces."""
    return int(
        session.scalar(
            select(func.coalesce(func.sum(AffiliateAccrual.accrued_micro), 0)).where(
                AffiliateAccrual.merchant_id == merchant_id,
                AffiliateAccrual.status == "accrued",
            )
        )
        or 0
    )


def referrer_summary(session: Session, referrer: str) -> dict:
    """A referring agent's own balance across every store that credited them —
    per-store accrued/paid/void totals + overall totals. Gated to the wallet holder
    (they sign as `referrer`); no merchant wallet, buyer, or order tx is exposed, so
    there is no public amount oracle."""
    rows = session.execute(
        select(
            AffiliateAccrual.store_id,
            AffiliateAccrual.status,
            func.count(),
            func.coalesce(func.sum(AffiliateAccrual.accrued_micro), 0),
        )
        .where(AffiliateAccrual.referrer_addr == referrer)
        .group_by(AffiliateAccrual.store_id, AffiliateAccrual.status)
    ).all()
    store_ids = {sid for sid, _, _, _ in rows}
    slug_by_store: dict[int, str] = {}
    if store_ids:
        for sid, slug in session.execute(
            select(Store.id, Store.slug).where(Store.id.in_(store_ids))
        ).all():
            slug_by_store[sid] = slug
    per_store: dict[int, dict] = {}
    totals = {"accrued_micro": 0, "void_micro": 0, "paid_micro": 0}
    for sid, status, count, micro in rows:
        st = per_store.setdefault(
            sid,
            {
                "store_slug": slug_by_store.get(sid),
                "accrued_micro": 0,
                "void_micro": 0,
                "paid_micro": 0,
                "orders": 0,
            },
        )
        st[status + "_micro"] += int(micro)
        st["orders"] += int(count)
        totals[status + "_micro"] += int(micro)
    return {
        "referrer": referrer,
        "totals": totals,
        "stores": list(per_store.values()),
    }

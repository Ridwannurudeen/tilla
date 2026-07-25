#!/usr/bin/env python3
"""One-off remediation for pre-fix orders left with paid_micro=0 (audit finding;
committed so the correction is reviewable).

Each affected order was delivered against a real, confirmed settlement — the USDT0
reached the merchant on chain — but its row records paid_micro=0 because at the time
only the checkout sweeper wrote that column. Two live consequences:

  * refunds._amount_due computes ``paid_micro - refunded_micro`` and raises
    "order has nothing left to refund" at 0, so a legitimate refund is refused.
  * dashboard._store_stats sums ``paid_micro - refunded_micro`` for revenue, so the
    merchant's reported takings are understated by the full amount of each sale.

The ROOT CAUSE IS ALREADY CLOSED — agentic._finalize_settled sets
``paid_micro=order.expected_micro`` on the settling->delivered flip and subscriptions
writes it at order creation (covered by tests/test_agent_buy.py). The rows this script
corrects are stale residue from before those fixes, including the six reaper-voided
orders restored earlier by a _finalize_settled that predated the paid_micro line. No
code changes here: this is a data correction only.

AFFECTED ROWS ARE DISCOVERED, NOT HARDCODED. A hand-written id list written from one
query missed eight rows of the identical shape; the query below is the definition of
the defect, so nothing in the class can hide from it.

VERIFICATION IS PER SETTLEMENT TX, NOT PER ORDER. The aggr_deferred rail settles a
BATCH: one relayer transfer pays the SUM of several orders. Verifying each order
independently against a shared tx would let a single 1.0 USDT0 transfer "cover" two
1.0 USDT0 orders. So orders are grouped by tx_hash and the on-chain total into the
merchant must cover the sum of EVERY order that claims that tx — including orders
whose paid_micro is already set, since the same transfer paid for those too.

Any shortfall, failed receipt, or mixed pay_to within a group aborts the whole run with
a rollback. Idempotent: rows already carrying paid_micro are outside the query.

Run on the VPS from /opt/tilla with the service env loaded (PYTHONPATH is required —
python puts the SCRIPT's directory on sys.path, not the working directory):
    set -a; . /opt/tilla/.env; set +a
    PYTHONPATH=/opt/tilla .venv/bin/python scripts/remediate_paid_micro.py
"""

from sqlalchemy import func, select

from app import chain, config, payment
from app.db import SessionLocal
from app.models import Order

TERMINAL_PAID = ("delivered", "paid")


def _pad(addr: str) -> str:
    return "0x" + "0" * 24 + addr.lower()[2:]


def _moved_to(cfg, tx_hash: str, pay_to: str) -> int:
    """Micro-USDT0 this tx moved to pay_to. Aborts unless the receipt succeeded."""
    receipt = chain.get_transaction_receipt(cfg, tx_hash, 30)
    if receipt is None or str(receipt.get("status", "")).lower() != "0x1":
        raise SystemExit(f"ABORT: {tx_hash} receipt missing or not status 1")
    want_to = _pad(pay_to)
    total = 0
    for lg in receipt.get("logs", []):
        if (lg.get("address", "") or "").lower() != config.USDT0:
            continue
        topics = lg.get("topics", [])
        if len(topics) < 3 or topics[0].lower() != config.TRANSFER_TOPIC:
            continue
        if topics[2].lower() == want_to:
            total += int(lg.get("data", "0x0"), 16)
    return total


def main() -> None:
    cfg = payment.CANONICAL_CHAIN
    session = SessionLocal()
    try:
        unpaid = session.scalars(
            select(Order)
            .where(
                Order.status.in_(TERMINAL_PAID),
                Order.tx_hash.is_not(None),
                func.coalesce(Order.paid_micro, 0) == 0,
            )
            .order_by(Order.created_at)
        ).all()
        if not unpaid:
            print("nothing to remediate")
            return
        print(f"{len(unpaid)} order(s) delivered against a settlement but recorded 0\n")

        by_tx: dict[str, list[Order]] = {}
        for order in unpaid:
            by_tx.setdefault(order.tx_hash, []).append(order)

        for tx_hash, orders in by_tx.items():
            # Every order this tx settled, not just the unpaid ones — a batch transfer
            # must cover the whole batch, so siblings already corrected still count.
            claimants = session.scalars(
                select(Order).where(Order.tx_hash == tx_hash)
            ).all()
            pay_tos = {o.pay_to.lower() for o in claimants}
            if len(pay_tos) != 1:
                raise SystemExit(f"ABORT: {tx_hash[:14]} claimed across {pay_tos}")
            pay_to = claimants[0].pay_to
            owed = sum(o.expected_micro for o in claimants)
            moved = _moved_to(cfg, tx_hash, pay_to)
            if moved < owed:
                raise SystemExit(
                    f"ABORT: {tx_hash[:14]} moved {moved} micro to {pay_to}, but "
                    f"{len(claimants)} order(s) claim it for {owed}"
                )
            print(
                f"{tx_hash[:14]}… moved {moved} micro -> {pay_to[:12]}…, "
                f"covers {len(claimants)} order(s) owed {owed}"
            )
            for order in orders:
                order.paid_micro = order.expected_micro
                print(f"    {order.id}: paid_micro 0 -> {order.expected_micro}")
        session.commit()
        print("\nDONE")
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()

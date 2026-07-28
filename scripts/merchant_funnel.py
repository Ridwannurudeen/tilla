#!/usr/bin/env python3
"""Merchant-activation funnel: does a Tilla store become a working business?

READ-ONLY. Answers the question `soldCount` cannot: created stores are not traction,
so this counts how far each merchant walks along

    store created -> product published -> first sale -> repeat buyer

and splits store GMV into external vs self-funded, because a self-funded order proves
the machinery works and proves nothing about demand.

WHY THIS EXISTS. Tilla's headline numbers describe the CREATE-STORE service (an ASP
selling storefronts). They say nothing about whether those storefronts then sell, and
the two are easy to conflate: a wallet that pays Tilla is a Tilla customer, not a
customer of the store it created. This script reports the second market only.

CLASSIFICATION IS SHOWN, NEVER HIDDEN. `_SELF_WALLETS` below is a documented constant
that WILL go stale, and a stale constant that silently reclassifies revenue is worse
than none — the payer census in the repo has been mislabelled in both directions
before. So every buyer wallet is printed with its raw totals alongside the split: if
the constant is wrong, the raw table shows it immediately.

Source of truth for wallet roles: docs/PROOF-onchain.md (per-transaction roles) and
the payer census in README.md. Re-derive via the OKX indexer, never a chain sweep.

Usage (on the VPS, where the DB lives):
    set -a; . /opt/tilla/.env; set +a
    /opt/tilla/.venv/bin/python scripts/merchant_funnel.py
"""

from __future__ import annotations

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import Merchant, Order, Product, Store

# Wallets Tilla itself controls, so their purchases are self-funded tests rather
# than demand. Roles per docs/PROOF-onchain.md; re-check against README.md's payer
# census before trusting the external/self split below.
_SELF_WALLETS = {
    "0x03d134c36425f312aefe28ab08bf471a61cf4ebb",  # Tilla's original buyer wallet
    "0xf4c9fa07f3bb852547fdc4df7c1d9fd9991cfa51",  # Account 1: agents 6961/4844/3808
}


def _fmt(micro: int | None) -> str:
    return f"{(micro or 0) / 1e6:.6f}".rstrip("0").rstrip(".") or "0"


def main() -> None:
    with SessionLocal() as s:
        merchants = s.scalar(select(func.count()).select_from(Merchant)) or 0
        live_stores = s.scalars(select(Store).where(Store.status == "live")).all()
        live_ids = [st.id for st in live_stores]

        with_product = {
            r
            for r in s.scalars(
                select(Product.store_id).where(
                    Product.store_id.in_(live_ids), Product.active.is_(True)
                )
            ).all()
        }
        delivered = s.scalars(select(Order).where(Order.status == "delivered")).all()

        sold_ids = {o.store_id for o in delivered}
        buyers: dict[str, list[Order]] = {}
        for o in delivered:
            buyers.setdefault((o.from_addr or "(none)").lower(), []).append(o)

        ext_orders = [
            o for o in delivered if (o.from_addr or "").lower() not in _SELF_WALLETS
        ]
        self_orders = [
            o for o in delivered if (o.from_addr or "").lower() in _SELF_WALLETS
        ]
        ext_buyers = {(o.from_addr or "").lower() for o in ext_orders}

        print("== FUNNEL ==")
        print(f"  merchants                     : {merchants}")
        print(f"  live stores                   : {len(live_stores)}")
        print(f"  ... with an active product    : {len(with_product)}")
        print(f"  ... that ever delivered a sale: {len(sold_ids)}")
        print(f"  ... never sold anything       : {len(live_ids) - len(sold_ids)}")

        print("\n== STORE GMV (delivered orders only) ==")
        print(
            f"  external : {len(ext_orders)} order(s), "
            f"{_fmt(sum(o.paid_micro for o in ext_orders))} USDT0, "
            f"{len(ext_buyers)} distinct buyer(s)"
        )
        print(
            f"  self     : {len(self_orders)} order(s), "
            f"{_fmt(sum(o.paid_micro for o in self_orders))} USDT0"
        )

        print("\n== EVERY BUYER, RAW (check the split above against this) ==")
        for addr, rows in sorted(buyers.items(), key=lambda kv: -len(kv[1])):
            tag = "self" if addr in _SELF_WALLETS else "external"
            print(
                f"  {addr}  n={len(rows):<3} "
                f"{_fmt(sum(r.paid_micro for r in rows)):>12} USDT0  [{tag}]"
            )

        # Counting our own wallet as a "repeat buyer" would flatter the number into
        # meaninglessness — the metric is whether an INDEPENDENT buyer came back.
        print("\n== REPEAT BUYERS (>1 delivered order) ==")
        repeat_ext = {
            a: r for a, r in buyers.items() if len(r) > 1 and a not in _SELF_WALLETS
        }
        repeat_self = {
            a: r for a, r in buyers.items() if len(r) > 1 and a in _SELF_WALLETS
        }
        print(f"  external : {len(repeat_ext)}")
        print(f"  self     : {len(repeat_self)}  (not demand; shown for completeness)")

        print("\n== RECENCY ==")
        paid = [o.paid_at for o in delivered if o.paid_at]
        if paid:
            print(f"  first sale : {min(paid)}")
            print(f"  last  sale : {max(paid)}")
        else:
            print("  no delivered sale on record")

        by_store: dict[int, list[Order]] = {}
        for o in delivered:
            by_store.setdefault(o.store_id, []).append(o)
        if by_store:
            print("\n== STORES THAT SOLD ==")
            slugs = {st.id: st.slug for st in live_stores}
            for sid, rows in sorted(by_store.items(), key=lambda kv: -len(kv[1])):
                print(
                    f"  {slugs.get(sid, f'store#{sid}'):<20} n={len(rows):<3} "
                    f"{_fmt(sum(r.paid_micro for r in rows)):>12} USDT0"
                )


if __name__ == "__main__":
    main()

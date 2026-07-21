#!/usr/bin/env python3
"""Browse Tilla and open a human checkout — ZERO funds, ZERO keys required.

This talks only to read-only + checkout-create endpoints. It never signs anything
and never moves money: it prints the pay-to address and amount so a HUMAN can
choose to pay from their own wallet (or just walk away — the order expires
server-side).

    python examples/browse_and_checkout.py [--slug SLUG]

With no --slug it discovers a store from the public index and uses the first one.
"""

from __future__ import annotations

import argparse

from tilla_sdk import TillaClient


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="https://tilla.gudman.xyz")
    parser.add_argument("--slug", default=None, help="store slug (default: discover)")
    args = parser.parse_args()

    with TillaClient(base_url=args.base_url) as client:
        slug = args.slug
        if slug is None:
            disc = client.discovery(limit=5)
            print(f"discovered {disc.total} live store(s):")
            for r in disc.resources:
                print(f"  - {r.slug}: {r.name} ({r.sold_count} sold)")
            if not disc.resources:
                print("no live stores to browse; pass --slug once one exists.")
                return
            slug = disc.resources[0].slug

        feed = client.feed(slug)
        print(f"\nstore: {feed.name} — {feed.description}")
        for p in feed.products:
            print(f"  product {p.id}: {p.title} — {p.price_amount} {p.currency}")

        checkout = client.create_checkout(slug)
        print("\n--- human checkout opened (nothing paid yet) ---")
        print(f"  order id : {checkout.id}")
        print(f"  pay to   : {checkout.pay_to}")
        print(f"  amount   : {checkout.amount_micro} micro-USDT (X Layer)")
        print(f"  expires  : {checkout.expires_at}")
        print(
            "\nTo complete: send exactly that amount of USDT0 to the pay-to address "
            "from your own wallet, then call client.submit_tx(order_id, tx_hash) or "
            "wait_for_paid(order_id). To abandon: do nothing — it expires."
        )

        status = client.checkout_status(checkout.id)
        print(f"\ncurrent status: {status.status}")


if __name__ == "__main__":
    main()

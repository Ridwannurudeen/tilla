#!/usr/bin/env python3
"""Agent x402 buy — THIS MOVES REAL FUNDS. Never run in CI.

You supply and control the key (TILLA_BUYER_KEY); the SDK enforces sign-once + an
amount cap and surfaces the full challenge (including the merchant pay-to) before
signing. A wrong asset/network/scheme, or an amount over your --max-usdt cap, is
refused BEFORE any signature is produced.

    export TILLA_BUYER_KEY=0x<your funded X Layer key>
    python examples/agent_buy.py --slug SLUG --max-usdt 1.0

The script prints the decoded challenge and requires you to type "YES" to proceed.
"""

from __future__ import annotations

import argparse
import os
import sys

from tilla_sdk import LocalEip3009Signer, PaymentRefused, SettlementUnknown, TillaClient


class ConfirmingSigner:
    """Wraps LocalEip3009Signer with an interactive confirmation that shows the
    real pay-to + amount before a signature (real funds) is ever produced."""

    def __init__(self, inner: LocalEip3009Signer):
        self._inner = inner

    def sign(self, challenge):
        print("\n--- x402 challenge (about to spend REAL USDT) ---")
        print(f"  pay to : {challenge.pay_to}  (the merchant wallet)")
        print(f"  amount : {challenge.amount_micro} micro-USDT")
        print(f"  asset  : {challenge.asset}")
        print(f"  network: {challenge.network}")
        answer = input('Type "YES" to sign and spend, anything else to abort: ')
        if answer.strip() != "YES":
            raise RuntimeError("aborted by operator")
        return self._inner.sign(challenge)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="https://tilla.gudman.xyz")
    parser.add_argument("--slug", required=True)
    parser.add_argument(
        "--max-usdt", type=float, required=True, help="hard spend cap in USDT"
    )
    args = parser.parse_args()

    key = os.environ.get("TILLA_BUYER_KEY")
    if not key:
        print("set TILLA_BUYER_KEY to a funded X Layer key first", file=sys.stderr)
        sys.exit(2)

    max_amount_micro = int(round(args.max_usdt * 1_000_000))
    signer = ConfirmingSigner(LocalEip3009Signer(key))

    with TillaClient(base_url=args.base_url) as client:
        try:
            purchase = client.buy(
                args.slug, signer=signer, max_amount_micro=max_amount_micro
            )
        except PaymentRefused as exc:
            print(f"refused before signing (no funds moved): {exc}", file=sys.stderr)
            sys.exit(1)
        except SettlementUnknown as exc:
            print(
                f"transport failed AFTER signing — do NOT re-run; reconcile the "
                f"order/store first: {exc}",
                file=sys.stderr,
            )
            sys.exit(1)

    print("\n--- purchase complete ---")
    print(f"  order id : {purchase.order_id}")
    print(f"  settle tx: {purchase.settle_tx}")
    print(f"  delivery : {purchase.delivery}")
    if purchase.download_url:
        print(f"  download : {purchase.download_url}")
    if purchase.license_key:
        print(f"  license  : {purchase.license_key}")


if __name__ == "__main__":
    main()

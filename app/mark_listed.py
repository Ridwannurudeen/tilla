#!/usr/bin/env python3
"""M10 marketplace listing commands (runbook steps B and E).

    cd /opt/tilla && .venv/bin/python -m app.mark_listed describe <slug>
    cd /opt/tilla && .venv/bin/python -m app.mark_listed <slug> <status>

``describe`` is READ-ONLY: it builds one store's OKX service listing — name,
description and fee — from the SAME live numbers ``/discovery/resources`` publishes
(trust tier, sales, clean-delivery rate, buyer rating), and prints the exact
``onchainos agent update`` command line for the owner to run by hand. It never calls
``onchainos``, never touches the chain, and writes nothing.

The status form is the ONLY writer of ``stores.marketplace_status``. Run it on the
VPS AFTER the user-approved on-chain listing lands, to record the state the read-only
dashboard panel surfaces. ``status`` is one of unlisted / prepared / submitted /
listed / rejected. Marking a store ``listed`` stamps ``marketplace_listed_at``; every
transition writes an append-only event_log row. There is no HTTP route that mutates
listing state — the on-chain listing is out-of-band and approval-gated, so this stays
a manual command.
"""

import json
import shlex
import sys

from sqlalchemy import select

from app import config
from app.agentic import (
    AGENT_ID,
    _active_product,
    _discovery_rows,
    _store_name,
    _usdt_str,
)
from app.db import SessionLocal
from app.models import Store, log_event

ALLOWED = ("unlisted", "prepared", "submitted", "listed", "rejected")


def _stats_sentence(row: dict) -> str:
    """The reputation half of the listing text, in buyer vocabulary, built ONLY from
    the fields that are actually populated. A store with no sales yet gets no stats
    sentence at all — a new store must read as new, never as ``None``."""
    if not row["sold_count"]:
        return ""
    clauses = [f"Sold {row['sold_count']} times"]
    if row["success_rate"] is not None:
        pct = round(row["success_rate"] * 100)
        clauses.append(f"{pct}% of sales delivered with no dispute or refund")
    if row["review_avg"] is not None:
        clauses.append(f"buyer rating {row['review_avg']} out of 5")
    clauses.append(f"seller trust tier {row['trust_tier']}")
    return ", ".join(clauses) + "."


# OKX registry cap on serviceName, learned from the live Wallet API (code 81001).
SERVICE_NAME_MAX = 30


def describe(slug: str) -> dict:
    """Build `slug`'s OKX service listing from live data, plus the exact
    ``onchainos agent update`` command line the owner runs by hand.

    The reputation numbers come from ``agentic._discovery_rows`` — the same call
    ``/discovery/resources`` serves — so the listing can never drift from what an
    agent buyer already sees. The fee is the primary product's exact price, i.e. what
    ``POST /s/<slug>/buy`` actually charges. Read-only; raises LookupError when there
    is nothing listable (unknown slug, store not publicly live, no active product).
    """
    with SessionLocal() as session:
        store = session.scalar(select(Store).where(Store.slug == slug))
        if store is None:
            raise LookupError(f"store not found: {slug}")
        # Check the product FIRST. Discovery now excludes stores with no active
        # product, so an emptiness test alone reports a paused store as "not
        # publicly live" — false, and it made this dedicated branch unreachable.
        product = _active_product(session, store.id)
        if product is None:
            raise LookupError(f"store has no active product: {slug}")
        rows = _discovery_rows(session, [Store.slug == slug], 1, 0)
        if not rows:
            raise LookupError(f"store is not publicly live, nothing to list: {slug}")
        row = rows[0]
        store_name = _store_name(store)
        fee = _usdt_str(product.price_micro)
        # Content rules (M10 runbook, learned on #3808): plain buyer vocabulary, no
        # links, no prompt-like text, no tech-stack terms. Only merchant-authored
        # strings here are the store and product names, so `validate-listing` still
        # runs on the output before submit.
        description = (
            f"{product.name} from {store_name}, delivered as soon as the payment "
            f"clears. Price {fee} USDT."
        )
        stats = _stats_sentence(row)
        if stats:
            description = f"{description} {stats}"
        # Rule D1 (blocking, checked by the CLI's `validate-listing`): a service
        # description must carry TWO parts on SEPARATE lines — what the service does,
        # then what the buyer supplies. The platform listings already had this shape;
        # the storefront listings did not, so every one of them fails validation as
        # written and would be blocked on the next review. The inputs named here are
        # exactly the ones the feed's per-product `input_schema` advertises, so a
        # buying agent reads the same contract in both places.
        description = (
            f"{description}\n"
            f"Provide: payment of {fee} USDT from your wallet. Optionally an agent "
            "id to be priced at a wholesale tier, and a referral wallet to credit "
            "the sale."
        )
        # The registry rejects serviceName over 30 chars (Wallet API code 81001,
        # hit on the first live B1.3 submit). Prefer "Buy <product> from <store>",
        # fall back to "Buy <product>", then truncate the product name — the store
        # identity always survives in the description.
        name = f"Buy {product.name} from {store_name}"
        if len(name) > SERVICE_NAME_MAX:
            name = f"Buy {product.name}"
        if len(name) > SERVICE_NAME_MAX:
            name = name[:SERVICE_NAME_MAX].rstrip()
        service = [
            {
                "operation": "create",
                "serviceName": name,
                "serviceDescription": description,
                "serviceType": "A2MCP",
                "fee": fee,
                "endpoint": f"{config.PUBLIC_BASE_URL}/s/{slug}/buy",
            }
        ]
        return {
            "slug": slug,
            "service": service[0],
            "command": (
                f"onchainos agent update --agent-id {AGENT_ID} --service "
                + shlex.quote(json.dumps(service, separators=(",", ":")))
            ),
        }


def mark_listed(slug: str, status: str) -> dict:
    """Set `slug`'s marketplace_status to `status`, stamping marketplace_listed_at
    when it becomes 'listed', plus an event_log row. Raises ValueError on a bad
    status and LookupError on an unknown slug."""
    if status not in ALLOWED:
        raise ValueError(f"status must be one of {', '.join(ALLOWED)}")
    with SessionLocal() as session:
        store = session.scalar(select(Store).where(Store.slug == slug))
        if store is None:
            raise LookupError(f"store not found: {slug}")
        store.marketplace_status = status
        if status == "listed" and store.marketplace_listed_at is None:
            from app.models import _utcnow

            store.marketplace_listed_at = _utcnow()
        log_event(
            session,
            "runbook",
            "store.marketplace_status",
            store_id=store.id,
            data={"slug": slug, "status": status},
        )
        session.commit()
        return {
            "slug": slug,
            "marketplace_status": status,
            "marketplace_listed_at": (
                store.marketplace_listed_at.isoformat()
                if store.marketplace_listed_at
                else None
            ),
        }


def main() -> None:
    if len(sys.argv) != 3:
        print("usage: python -m app.mark_listed describe <slug>")
        print("       python -m app.mark_listed <slug> <status>")
        print(f"status: {' | '.join(ALLOWED)}")
        sys.exit(1)
    if sys.argv[1] == "describe":
        try:
            listing = describe(sys.argv[2])
        except LookupError as exc:
            print(f"ERROR: {exc}")
            sys.exit(1)
        service = listing["service"]
        print(f"serviceName:        {service['serviceName']}")
        print(f"serviceDescription: {service['serviceDescription']}")
        print(f"fee:                {service['fee']}")
        print(f"endpoint:           {service['endpoint']}")
        print()
        print("Run this yourself (on-chain, approval-gated):")
        print(listing["command"])
        return
    slug, status = sys.argv[1], sys.argv[2]
    try:
        result = mark_listed(slug, status)
    except (ValueError, LookupError) as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)
    print(f"OK: {result}")


if __name__ == "__main__":
    main()

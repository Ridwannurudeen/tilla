#!/usr/bin/env python3
"""M10 marketplace listing-state command (runbook step E).

The ONLY writer of ``stores.marketplace_status``. Run on the VPS AFTER the
user-approved on-chain listing lands, to record the state the read-only dashboard
panel surfaces:

    cd /opt/tilla && .venv/bin/python -m app.mark_listed <slug> <status>

``status`` is one of unlisted / prepared / submitted / listed / rejected. Marking a
store ``listed`` stamps ``marketplace_listed_at``; every transition writes an
append-only event_log row. There is no HTTP route that mutates listing state — the
on-chain listing is out-of-band and approval-gated, so this stays a manual command.
"""

import sys

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Store, log_event

ALLOWED = ("unlisted", "prepared", "submitted", "listed", "rejected")


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
        print("usage: python -m app.mark_listed <slug> <status>")
        print(f"status: {' | '.join(ALLOWED)}")
        sys.exit(1)
    slug, status = sys.argv[1], sys.argv[2]
    try:
        result = mark_listed(slug, status)
    except (ValueError, LookupError) as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)
    print(f"OK: {result}")


if __name__ == "__main__":
    main()

"""Bring every Tilla-generated delivery message up to the current default.

`create-store` used to default `stores.delivery` to a fabricated
`https://tilla.gudman.xyz/files/<slug>` link. That path is not a route, so a buyer
who paid received a link that 404s — labelled "(demo delivery link)", but only
after settling. Fixing the default on 2026-07-28 changed what NEW stores get and
left every row already persisted exactly as it was; this is what repairs them.

It also re-runs cleanly when the wording changes again. A store repaired under an
older default would otherwise be frozen at whatever the text said the day it ran,
so the fleet would fragment into one cohort per edit.

Rewrites to `engine.default_delivery_text` — the same function the create path
calls, so the two cannot drift — and repairs the on-disk `stores/<slug>/store.json`
to match, since that file is what `app.import_stores` re-reads on a fresh host.

Deliberately narrow — a store is rewritten only when BOTH hold:

  * its `delivery` is text TILLA generated (the exact fabricated fallback, or an
    exact render of a superseded default), and
  * it has no active deliverable.

The second condition is what keeps this safe. A merchant who attached real goods
has working fulfilment regardless of what the text says, and their `delivery` is
theirs to word; silently rewriting it would be the same class of overreach as the
price floor that started all of this. A merchant who wrote their own message is
untouched too: matching an exact render of a default we shipped is what proves we
wrote it, and prose a merchant typed cannot collide with one.

Dry run by default: prints every change and writes nothing. Pass --apply to commit.
"""

from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import select

from app import config, engine
from app.db import SessionLocal
from app.models import Deliverable, Store


def _product_name(store: Store) -> str:
    content = store.content if isinstance(store.content, dict) else {}
    return str(content.get("product_name") or "your order")


def _is_tilla_authored(store: Store) -> bool:
    """True iff this store's delivery text is one WE generated, not the merchant's.

    Either it is the exact historical fabricated-link fallback for this store, or
    it is an exact render of a superseded default. Exact match is the point: it is
    proof of authorship, so a merchant's own URL containing ``/files/`` cannot be
    caught by a broad substring match."""
    text = store.delivery or ""
    historical = (
        f"✅ Thank you! Your {_product_name(store)} is ready: "
        f"https://tilla.gudman.xyz/files/{store.slug} (demo delivery link)"
    )
    if text == historical:
        return True
    return text in engine.superseded_delivery_texts(store.slug, _product_name(store))


def _repairable(session) -> list[Store]:
    """Tilla-authored delivery text on a store that cannot fulfil, already stale.

    Status is deliberately NOT filtered. A blocked store 404s before any 402
    (`agentic._guard_store_status`), so its text is unreachable today — but
    leaving stale text in the row means unblocking one silently restores it.
    Reachability is not a reason to keep bad data."""
    stores = session.scalars(select(Store).order_by(Store.id)).all()
    out = []
    for store in stores:
        if not _is_tilla_authored(store):
            continue
        # Already current — nothing to do, and this is what makes re-runs no-ops.
        if store.delivery == engine.default_delivery_text(
            store.slug, _product_name(store)
        ):
            continue
        active = session.scalar(
            select(Deliverable).where(
                Deliverable.store_id == store.id, Deliverable.active.is_(True)
            )
        )
        if active is None:
            out.append(store)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--apply", action="store_true", help="write the changes (default: dry run)"
    )
    args = ap.parse_args()

    with SessionLocal() as session:
        targets = _repairable(session)
        if not targets:
            print("nothing to repair")
            return 0

        print(f"{len(targets)} store(s) to repair\n")
        for store in targets:
            new_text = engine.default_delivery_text(store.slug, _product_name(store))
            print(f"--- {store.slug} (id={store.id})")
            print(f"    BEFORE: {(store.delivery or '')[:120]}")
            print(f"    AFTER : {new_text[:120]}")
            if not args.apply:
                continue
            store.delivery = new_text
            # store.json is the on-disk record app.import_stores reads; leaving the
            # fabricated link there would resurrect it on a fresh-host import.
            path = config.STORES_DIR / store.slug / "store.json"
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                data["delivery"] = new_text
                path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print("    store.json updated")
            else:
                print("    store.json absent — DB only")

        if args.apply:
            session.commit()
            print(f"\napplied to {len(targets)} store(s)")
        else:
            print(
                f"\ndry run — nothing written. Re-run with --apply for {len(targets)}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())

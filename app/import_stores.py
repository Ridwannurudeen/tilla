#!/usr/bin/env python3
"""One-shot, idempotent importer for stores that already exist on disk.

Run on the VPS during the M2 deploy — AFTER `alembic upgrade head` and BEFORE
`systemctl restart tilla-api` — so invoice-flow and billable exist as DB rows
before the new (DB-reading) code serves a single request:

    cd /opt/tilla && python3 -m app.import_stores

Idempotent: a store whose slug already has a row is skipped, so re-running
inserts nothing. Parses defensively (a broken store.json is logged and skipped,
never fatal). Never touches index.html and never deletes anything.
"""

import json
import logging

from sqlalchemy import select

from app import config
from app.db import SessionLocal
from app.engine import DEFAULT_THEME
from app.models import Product, Store, get_or_create_merchant, log_event

logger = logging.getLogger("tilla")
STORES_DIR = config.STORES_DIR


def import_stores() -> dict:
    imported = skipped = broken = 0
    if not STORES_DIR.exists():
        logger.warning("import_stores: %s does not exist", STORES_DIR)
        print("import_stores: imported=0 skipped=0 broken=0")
        return {"imported": 0, "skipped": 0, "broken": 0}

    with SessionLocal() as session:
        for d in sorted(STORES_DIR.iterdir()):
            meta_path = d / "store.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                logger.warning(
                    "import_stores: broken store.json in %s, skipping", d.name
                )
                broken += 1
                continue

            slug = meta.get("slug", d.name)
            if session.scalar(select(Store).where(Store.slug == slug)) is not None:
                logger.info("import_stores: %s already imported, skipping", slug)
                skipped += 1
                continue

            try:
                pay_to = meta["pay_to"]
                price_micro = int(round(float(meta["amount_usdt"]) * 1e6))
            except (KeyError, TypeError, ValueError):
                logger.warning(
                    "import_stores: %s missing/invalid pay_to or amount_usdt, skipping",
                    slug,
                )
                broken += 1
                continue
            if price_micro <= 0:
                logger.warning(
                    "import_stores: %s has non-positive price, skipping", slug
                )
                broken += 1
                continue

            merchant = get_or_create_merchant(session, pay_to)
            store = Store(
                slug=slug,
                merchant_id=merchant.id,
                status=meta.get("status", "live"),
                pay_to=pay_to,
                delivery=meta.get("delivery"),
                description=meta.get("description"),
                content=meta.get("content"),
                theme=meta.get("theme", DEFAULT_THEME),
            )
            session.add(store)
            session.flush()
            session.add(
                Product(
                    store_id=store.id,
                    name=meta.get("product_name", ""),
                    price_micro=price_micro,
                    active=True,
                )
            )
            log_event(
                session,
                "import",
                "store.imported",
                store_id=store.id,
                data={"source": "store.json"},
            )
            imported += 1

        session.commit()

    print(f"import_stores: imported={imported} skipped={skipped} broken={broken}")
    return {"imported": imported, "skipped": skipped, "broken": broken}


if __name__ == "__main__":
    import_stores()

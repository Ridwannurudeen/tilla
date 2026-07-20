import json

from sqlalchemy import func, select

import app.import_stores as importer
from app.db import SessionLocal
from app.models import Merchant, Product, Store


def _write_store(root, slug, meta):
    d = root / slug
    d.mkdir()
    (d / "store.json").write_text(json.dumps(meta), encoding="utf-8")


def test_import_old_and_new_format_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(importer, "STORES_DIR", tmp_path)

    # (a) old-format live store, exactly the shape invoice-flow/billable ship:
    # no status/content, amount as an int.
    _write_store(
        tmp_path,
        "invoice-flow",
        {
            "slug": "invoice-flow",
            "product_name": "Invoice PDF",
            "amount_usdt": 9,
            "pay_to": "0x" + "1" * 40,
            "delivery": "Here is your invoice.",
        },
    )
    # (b) new-format pending_screening store with content (has no index.html yet)
    _write_store(
        tmp_path,
        "pending-brand",
        {
            "slug": "pending-brand",
            "status": "pending_screening",
            "product_name": "Course",
            "amount_usdt": 19.5,
            "pay_to": "0x" + "2" * 40,
            "delivery": "Course link",
            "description": "a course",
            "content": {"store_name": "Brand", "price_usdt": 19.5},
            "theme": "original.html",
        },
    )

    assert importer.import_stores() == {"imported": 2, "skipped": 0, "broken": 0}

    with SessionLocal() as s:
        inv = s.scalar(select(Store).where(Store.slug == "invoice-flow"))
        assert inv.status == "live"  # missing status defaults to live
        assert inv.pay_to == "0x" + "1" * 40
        inv_product = s.scalar(select(Product).where(Product.store_id == inv.id))
        assert inv_product.price_micro == 9_000_000  # exact 6dp conversion

        pend = s.scalar(select(Store).where(Store.slug == "pending-brand"))
        assert pend.status == "pending_screening"  # preserved, not forced live
        assert pend.content == {"store_name": "Brand", "price_usdt": 19.5}
        pend_product = s.scalar(select(Product).where(Product.store_id == pend.id))
        assert pend_product.price_micro == 19_500_000

        assert s.scalar(select(func.count()).select_from(Merchant)) == 2

    # idempotent: a second run inserts nothing and touches no counts
    assert importer.import_stores() == {"imported": 0, "skipped": 2, "broken": 0}
    with SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(Store)) == 2
        assert s.scalar(select(func.count()).select_from(Product)) == 2
        assert s.scalar(select(func.count()).select_from(Merchant)) == 2


def test_import_skips_broken_json(tmp_path, monkeypatch):
    monkeypatch.setattr(importer, "STORES_DIR", tmp_path)
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "store.json").write_text("{ not valid json", encoding="utf-8")
    # a dir with no store.json at all is ignored, not counted
    (tmp_path / "empty").mkdir()

    assert importer.import_stores() == {"imported": 0, "skipped": 0, "broken": 1}
    with SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(Store)) == 0

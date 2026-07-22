import os
import pathlib
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.exc import IntegrityError

import app.main as main
from app import checkout
from app.db import SessionLocal, engine
from app.models import (
    Delivery,
    EventLog,
    Merchant,
    Order,
    Product,
    Store,
    get_or_create_merchant,
)

REPO = pathlib.Path(__file__).resolve().parent.parent
TABLES = {"merchants", "stores", "products", "orders", "deliveries", "event_log"}
M3_TABLES = {"processed_transfers", "chain_cursor"}


def _confirm(cid, tx_hash="0x" + "1" * 64):
    """Apply an exact, fully-confirmed on-chain payment to an order via the real
    state machine — no network. Confirms and delivers in one shot."""
    with SessionLocal() as s:
        order = s.get(Order, cid)
        checkout.apply_transfer(
            s,
            order,
            order.expected_micro - (order.paid_micro or 0),
            tx_hash=tx_hash,
            log_index=0,
            block_number=1,
            from_addr="0x" + "2" * 40,
            head=10**9,
        )
        s.commit()


# ---------- alembic migration up/down (BUILD.md acceptance) ----------
def _alembic(db_path, *args):
    env = dict(os.environ)
    env["TILLA_DB_PATH"] = str(db_path)
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
    )


def _table_names(db_path):
    eng = create_engine(f"sqlite:///{db_path}")
    try:
        return set(inspect(eng).get_table_names())
    finally:
        eng.dispose()


def test_migration_up_down_reentrant(tmp_path):
    db = tmp_path / "mig.db"

    r = _alembic(db, "upgrade", "head")
    assert r.returncode == 0, r.stderr
    assert TABLES <= _table_names(db)

    r = _alembic(db, "downgrade", "base")
    assert r.returncode == 0, r.stderr
    assert not (TABLES & _table_names(db))

    # re-entrant: upgrading again from base re-creates everything
    r = _alembic(db, "upgrade", "head")
    assert r.returncode == 0, r.stderr
    assert TABLES <= _table_names(db)


def test_migration_0002_backfills_and_reverses(tmp_path):
    import sqlite3

    db = tmp_path / "m3.db"
    r = _alembic(db, "upgrade", "0001_persistence_core")
    assert r.returncode == 0, r.stderr

    # seed legacy rows against the 0001 schema (no expected_micro yet)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO merchants (id, wallet_address, created_at) VALUES (1,'0xabc','2026')"
    )
    con.execute(
        "INSERT INTO stores (id, slug, merchant_id, status, pay_to, theme, created_at,"
        " updated_at) VALUES (1,'s',1,'live','0xabc','original.html','2026','2026')"
    )
    con.execute(
        "INSERT INTO orders (id, store_id, pay_to, amount_micro, baseline_micro, status,"
        " created_at) VALUES ('leg_pending',1,'0xabc',9000000,0,'pending','2026')"
    )
    con.execute(
        "INSERT INTO orders (id, store_id, pay_to, amount_micro, baseline_micro, status,"
        " created_at) VALUES ('leg_paid',1,'0xabc',5000000,0,'paid','2026')"
    )
    con.commit()
    con.close()

    r = _alembic(db, "upgrade", "head")
    assert r.returncode == 0, r.stderr
    assert M3_TABLES <= _table_names(db)

    con = sqlite3.connect(db)
    cols = {c[1] for c in con.execute("PRAGMA table_info(orders)")}
    assert {
        "expected_micro",
        "paid_micro",
        "overpaid_micro",
        "from_addr",
        "block_number",
    } <= cols
    # expected_micro backfilled to the exact price; legacy 'paid' status untouched
    assert (
        con.execute(
            "SELECT expected_micro FROM orders WHERE id='leg_pending'"
        ).fetchone()[0]
        == 9000000
    )
    assert (
        con.execute("SELECT status FROM orders WHERE id='leg_paid'").fetchone()[0]
        == "paid"
    )
    idx = {
        i[0] for i in con.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert "ux_orders_active_amount" in idx
    con.close()

    r = _alembic(db, "downgrade", "0001_persistence_core")
    assert r.returncode == 0, r.stderr
    assert not (M3_TABLES & _table_names(db))
    con = sqlite3.connect(db)
    cols = {c[1] for c in con.execute("PRAGMA table_info(orders)")}
    assert "expected_micro" not in cols
    assert (
        con.execute("SELECT status FROM orders WHERE id='leg_paid'").fetchone()[0]
        == "paid"
    )
    con.close()


def test_migration_0002_dedupes_duplicate_pendings(tmp_path):
    import sqlite3

    db = tmp_path / "dup.db"
    r = _alembic(db, "upgrade", "0001_persistence_core")
    assert r.returncode == 0, r.stderr

    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO merchants (id, wallet_address, created_at) VALUES (1,'0xabc','2026')"
    )
    con.execute(
        "INSERT INTO stores (id, slug, merchant_id, status, pay_to, theme, created_at,"
        " updated_at) VALUES (1,'s',1,'live','0xabc','original.html','2026','2026')"
    )
    # M2 had no unique-amount offset: two pending orders share (pay_to, amount) and
    # would collide on the partial unique index the migration builds.
    con.execute(
        "INSERT INTO orders (id, store_id, pay_to, amount_micro, baseline_micro, status,"
        " created_at) VALUES ('dup1',1,'0xabc',9000000,0,'pending','2026')"
    )
    con.execute(
        "INSERT INTO orders (id, store_id, pay_to, amount_micro, baseline_micro, status,"
        " created_at) VALUES ('dup2',1,'0xabc',9000000,0,'pending','2026')"
    )
    con.commit()
    con.close()

    r = _alembic(db, "upgrade", "head")
    assert r.returncode == 0, r.stderr  # index build must not fail on the duplicate

    con = sqlite3.connect(db)
    statuses = sorted(row[0] for row in con.execute("SELECT status FROM orders"))
    # exactly one kept pending, the other parked outside the index predicate
    assert statuses == ["canceled", "pending"]
    con.close()


def test_migration_0006_additive_and_index_survives(tmp_path):
    import sqlite3

    db = tmp_path / "m9.db"
    r = _alembic(db, "upgrade", "0005_payment_methods")
    assert r.returncode == 0, r.stderr

    # seed a live order against the 0005 schema (no refunded_micro yet)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO merchants (id, wallet_address, created_at) VALUES (1,'0xabc','2026')"
    )
    con.execute(
        "INSERT INTO stores (id, slug, merchant_id, status, pay_to, theme, created_at,"
        " updated_at) VALUES (1,'s',1,'live','0xabc','original.html','2026','2026')"
    )
    con.execute(
        "INSERT INTO orders (id, store_id, pay_to, amount_micro, expected_micro,"
        " paid_micro, overpaid_micro, status, created_at)"
        " VALUES ('m9ord',1,'0xabc',9000000,9000123,9000123,0,'delivered','2026')"
    )
    con.commit()
    con.close()

    r = _alembic(db, "upgrade", "head")
    assert r.returncode == 0, r.stderr
    assert {"refunds", "webhook_deliveries"} <= _table_names(db)

    con = sqlite3.connect(db)
    ocols = {c[1] for c in con.execute("PRAGMA table_info(orders)")}
    assert "refunded_micro" in ocols
    # additive column backfills to 0 for the existing order (no behaviour change)
    assert (
        con.execute("SELECT refunded_micro FROM orders WHERE id='m9ord'").fetchone()[0]
        == 0
    )
    mcols = {c[1] for c in con.execute("PRAGMA table_info(merchants)")}
    assert {"webhook_url", "webhook_secret"} <= mcols
    idx = {
        i[0] for i in con.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    # native ADD COLUMN (no batch rebuild) — the M3 partial unique index survives
    assert "ux_orders_active_amount" in idx
    refunds_ddl = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='refunds'"
    ).fetchone()[0]
    assert "uq_refunds_tx_log" in refunds_ddl  # one transfer -> one order, ever
    con.close()

    r = _alembic(db, "downgrade", "0005_payment_methods")
    assert r.returncode == 0, r.stderr
    assert not ({"refunds", "webhook_deliveries"} & _table_names(db))
    con = sqlite3.connect(db)
    ocols = {c[1] for c in con.execute("PRAGMA table_info(orders)")}
    assert "refunded_micro" not in ocols
    idx = {
        i[0] for i in con.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert "ux_orders_active_amount" in idx  # untouched by the downgrade too
    con.close()


def test_migration_0007_additive_and_index_survives(tmp_path):
    import sqlite3

    db = tmp_path / "m10.db"
    r = _alembic(db, "upgrade", "0006_merchant_platform")
    assert r.returncode == 0, r.stderr

    # seed a live store against the 0006 schema (no marketplace_* columns yet)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO merchants (id, wallet_address, created_at) VALUES (1,'0xabc','2026')"
    )
    con.execute(
        "INSERT INTO stores (id, slug, merchant_id, status, pay_to, theme, created_at,"
        " updated_at) VALUES (1,'s',1,'live','0xabc','original.html','2026','2026')"
    )
    con.commit()
    con.close()

    r = _alembic(db, "upgrade", "head")
    assert r.returncode == 0, r.stderr
    assert "screening_receipts" in _table_names(db)

    con = sqlite3.connect(db)
    scols = {c[1] for c in con.execute("PRAGMA table_info(stores)")}
    assert {"marketplace_status", "marketplace_listed_at"} <= scols
    # additive column backfills to 'unlisted' for the existing store
    assert (
        con.execute("SELECT marketplace_status FROM stores WHERE id=1").fetchone()[0]
        == "unlisted"
    )
    sddl = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='stores'"
    ).fetchone()[0]
    assert "ck_stores_marketplace_status" in sddl  # CHECK survives the batch rebuild
    assert "UNIQUE" in sddl.upper()  # slug uniqueness preserved
    rddl = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='screening_receipts'"
    ).fetchone()[0]
    assert "ck_screening_receipts_mode" in rddl
    idx = {
        i[0] for i in con.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    # stores rebuild must not touch the M3 partial unique index on orders
    assert "ux_orders_active_amount" in idx
    con.close()

    r = _alembic(db, "downgrade", "0006_merchant_platform")
    assert r.returncode == 0, r.stderr
    assert "screening_receipts" not in _table_names(db)
    con = sqlite3.connect(db)
    scols = {c[1] for c in con.execute("PRAGMA table_info(stores)")}
    assert "marketplace_status" not in scols
    idx = {
        i[0] for i in con.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert "ux_orders_active_amount" in idx  # untouched by the downgrade too
    con.close()


def test_migration_0008_additive_and_index_survives(tmp_path):
    import sqlite3

    db = tmp_path / "m11.db"
    r = _alembic(db, "upgrade", "0007_marketplace_citizenship")
    assert r.returncode == 0, r.stderr

    # seed a delivered order against the 0007 schema (no attest_* columns yet)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO merchants (id, wallet_address, created_at) VALUES (1,'0xabc','2026')"
    )
    con.execute(
        "INSERT INTO stores (id, slug, merchant_id, status, pay_to, theme, created_at,"
        " updated_at) VALUES (1,'s',1,'live','0xabc','original.html','2026','2026')"
    )
    con.execute(
        "INSERT INTO orders (id, store_id, pay_to, amount_micro, expected_micro,"
        " paid_micro, overpaid_micro, status, created_at)"
        " VALUES ('m11ord',1,'0xabc',9000000,9000123,9000123,0,'delivered','2026')"
    )
    con.commit()
    con.close()

    r = _alembic(db, "upgrade", "head")
    assert r.returncode == 0, r.stderr

    con = sqlite3.connect(db)
    ocols = {c[1] for c in con.execute("PRAGMA table_info(orders)")}
    assert {"attestation_uid", "attest_tx", "attest_status"} <= ocols
    # additive column backfills to 'none' for the existing order (never queued)
    assert (
        con.execute("SELECT attest_status FROM orders WHERE id='m11ord'").fetchone()[0]
        == "none"
    )
    idx = {
        i[0] for i in con.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert "ix_orders_attest_status" in idx  # the worker's pending-query index
    # native ADD COLUMN (no batch rebuild) — the M3 partial unique index survives
    assert "ux_orders_active_amount" in idx
    con.close()

    # up-down-up: the partial index must survive the downgrade rebuild + re-upgrade
    r = _alembic(db, "downgrade", "0007_marketplace_citizenship")
    assert r.returncode == 0, r.stderr
    con = sqlite3.connect(db)
    ocols = {c[1] for c in con.execute("PRAGMA table_info(orders)")}
    assert "attest_status" not in ocols
    idx = {
        i[0] for i in con.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert "ux_orders_active_amount" in idx  # untouched by the downgrade too
    con.close()

    r = _alembic(db, "upgrade", "head")
    assert r.returncode == 0, r.stderr
    con = sqlite3.connect(db)
    idx = {
        i[0] for i in con.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert "ux_orders_active_amount" in idx  # survives up-down-up
    assert "ix_orders_attest_status" in idx
    con.close()


def test_migration_0009_additive_and_index_survives(tmp_path):
    import sqlite3

    db = tmp_path / "m13.db"
    r = _alembic(db, "upgrade", "0008_onchain_receipts")
    assert r.returncode == 0, r.stderr

    # seed a delivered order against the 0008 schema (no referrer_addr yet)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO merchants (id, wallet_address, created_at) VALUES (1,'0xabc','2026')"
    )
    con.execute(
        "INSERT INTO stores (id, slug, merchant_id, status, pay_to, theme, created_at,"
        " updated_at) VALUES (1,'s',1,'live','0xabc','original.html','2026','2026')"
    )
    con.execute(
        "INSERT INTO orders (id, store_id, pay_to, amount_micro, expected_micro,"
        " paid_micro, overpaid_micro, status, created_at)"
        " VALUES ('m13ord',1,'0xabc',9000000,9000123,9000123,0,'delivered','2026')"
    )
    con.commit()
    con.close()

    r = _alembic(db, "upgrade", "head")
    assert r.returncode == 0, r.stderr
    tables = _table_names(db)
    assert {
        "affiliate_accruals",
        "affiliate_payouts",
        "email_subscribers",
        "acp_sessions",
    } <= tables

    con = sqlite3.connect(db)
    ocols = {c[1] for c in con.execute("PRAGMA table_info(orders)")}
    assert "referrer_addr" in ocols
    # additive column backfills to NULL for the existing order (unreferred)
    assert (
        con.execute("SELECT referrer_addr FROM orders WHERE id='m13ord'").fetchone()[0]
        is None
    )
    accr_ddl = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='affiliate_accruals'"
    ).fetchone()[0]
    assert "uq_affiliate_accruals_order_id" in accr_ddl
    assert "ck_affiliate_accruals_status" in accr_ddl
    pay_ddl = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='affiliate_payouts'"
    ).fetchone()[0]
    assert "uq_affiliate_payouts_tx_log" in pay_ddl  # one transfer -> one payout, ever
    idx = {
        i[0] for i in con.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    # native ADD COLUMN (no batch rebuild) — the M3 partial unique index survives
    assert "ux_orders_active_amount" in idx
    assert "ix_orders_referrer_addr" in idx
    con.close()

    # up-down-up: the partial index must survive the downgrade rebuild + re-upgrade
    r = _alembic(db, "downgrade", "0008_onchain_receipts")
    assert r.returncode == 0, r.stderr
    con = sqlite3.connect(db)
    ocols = {c[1] for c in con.execute("PRAGMA table_info(orders)")}
    assert "referrer_addr" not in ocols
    idx = {
        i[0] for i in con.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert "ux_orders_active_amount" in idx
    con.close()

    r = _alembic(db, "upgrade", "head")
    assert r.returncode == 0, r.stderr
    con = sqlite3.connect(db)
    idx = {
        i[0] for i in con.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert "ux_orders_active_amount" in idx  # survives up-down-up
    assert "ix_orders_referrer_addr" in idx
    con.close()


def test_migration_0014_additive_and_index_survives(tmp_path):
    import sqlite3

    db = tmp_path / "m18.db"
    r = _alembic(db, "upgrade", "0009_growth")
    assert r.returncode == 0, r.stderr

    # seed a delivered order against the 0009 schema (no network column yet)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO merchants (id, wallet_address, created_at) VALUES (1,'0xabc','2026')"
    )
    con.execute(
        "INSERT INTO stores (id, slug, merchant_id, status, pay_to, theme, created_at,"
        " updated_at) VALUES (1,'s',1,'live','0xabc','original.html','2026','2026')"
    )
    con.execute(
        "INSERT INTO orders (id, store_id, pay_to, amount_micro, expected_micro,"
        " paid_micro, overpaid_micro, status, created_at)"
        " VALUES ('m18ord',1,'0xabc',9000000,9000123,9000123,0,'delivered','2026')"
    )
    con.commit()
    con.close()

    r = _alembic(db, "upgrade", "head")
    assert r.returncode == 0, r.stderr

    con = sqlite3.connect(db)
    ocols = {c[1] for c in con.execute("PRAGMA table_info(orders)")}
    assert "network" in ocols
    # additive NOT NULL column backfills every existing order to the canonical chain
    assert (
        con.execute("SELECT network FROM orders WHERE id='m18ord'").fetchone()[0]
        == "eip155:196"
    )
    idx = {
        i[0] for i in con.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    # native ADD COLUMN (no batch rebuild) — the M3 partial unique index survives
    assert "ux_orders_active_amount" in idx
    con.close()

    # up-down-up: the partial index must survive the downgrade rebuild + re-upgrade
    r = _alembic(db, "downgrade", "0009_growth")
    assert r.returncode == 0, r.stderr
    con = sqlite3.connect(db)
    ocols = {c[1] for c in con.execute("PRAGMA table_info(orders)")}
    assert "network" not in ocols
    idx = {
        i[0] for i in con.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert "ux_orders_active_amount" in idx
    con.close()

    r = _alembic(db, "upgrade", "head")
    assert r.returncode == 0, r.stderr
    con = sqlite3.connect(db)
    idx = {
        i[0] for i in con.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert "ux_orders_active_amount" in idx  # survives up-down-up
    con.close()


def test_migration_0011_additive_and_index_survives(tmp_path):
    import sqlite3

    db = tmp_path / "m14.db"
    r = _alembic(db, "upgrade", "0021_self_serve_create_store")
    assert r.returncode == 0, r.stderr

    # seed a product against the 0021 schema (no sla_minutes yet)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO merchants (id, wallet_address, created_at) VALUES (1,'0xabc','2026')"
    )
    con.execute(
        "INSERT INTO stores (id, slug, merchant_id, status, pay_to, theme, created_at,"
        " updated_at) VALUES (1,'s',1,'live','0xabc','original.html','2026','2026')"
    )
    con.execute(
        "INSERT INTO products (id, store_id, name, price_micro, active, pricing_model,"
        " created_at) VALUES (1,1,'Thing',9000000,1,'one_time','2026')"
    )
    con.commit()
    con.close()

    r = _alembic(db, "upgrade", "head")
    assert r.returncode == 0, r.stderr
    con = sqlite3.connect(db)
    pcols = {c[1] for c in con.execute("PRAGMA table_info(products)")}
    assert "sla_minutes" in pcols
    # additive column backfills to NULL for the existing product (no per-product SLA)
    assert (
        con.execute("SELECT sla_minutes FROM products WHERE id=1").fetchone()[0] is None
    )
    idx = {
        i[0] for i in con.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    # products add_column never touches orders — the M3 partial unique index is safe
    assert "ux_orders_active_amount" in idx
    con.close()

    # up-down-up: the column drops and re-adds, the partial index is untouched
    r = _alembic(db, "downgrade", "0021_self_serve_create_store")
    assert r.returncode == 0, r.stderr
    con = sqlite3.connect(db)
    pcols = {c[1] for c in con.execute("PRAGMA table_info(products)")}
    assert "sla_minutes" not in pcols
    idx = {
        i[0] for i in con.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert "ux_orders_active_amount" in idx
    con.close()

    r = _alembic(db, "upgrade", "head")
    assert r.returncode == 0, r.stderr


def test_migration_0012_additive_and_index_survives(tmp_path):
    import sqlite3

    db = tmp_path / "m15.db"
    r = _alembic(db, "upgrade", "0022_product_sla")
    assert r.returncode == 0, r.stderr

    # seed a delivered order against the 0022 schema (no eval_status yet)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO merchants (id, wallet_address, created_at) VALUES (1,'0xabc','2026')"
    )
    con.execute(
        "INSERT INTO stores (id, slug, merchant_id, status, pay_to, theme, created_at,"
        " updated_at) VALUES (1,'s',1,'live','0xabc','original.html','2026','2026')"
    )
    con.execute(
        "INSERT INTO orders (id, store_id, pay_to, amount_micro, expected_micro,"
        " paid_micro, overpaid_micro, status, created_at)"
        " VALUES ('m15ord',1,'0xabc',9000000,9000123,9000123,0,'delivered','2026')"
    )
    con.commit()
    con.close()

    r = _alembic(db, "upgrade", "head")
    assert r.returncode == 0, r.stderr
    con = sqlite3.connect(db)
    ocols = {c[1] for c in con.execute("PRAGMA table_info(orders)")}
    assert "eval_status" in ocols
    # server_default backfills the existing delivered order to 'none' (counted as good)
    assert (
        con.execute("SELECT eval_status FROM orders WHERE id='m15ord'").fetchone()[0]
        == "none"
    )
    idx = {
        i[0] for i in con.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    # native ADD COLUMN on orders leaves the M3 partial unique index intact
    assert "ux_orders_active_amount" in idx
    con.close()

    r = _alembic(db, "downgrade", "0022_product_sla")
    assert r.returncode == 0, r.stderr
    con = sqlite3.connect(db)
    ocols = {c[1] for c in con.execute("PRAGMA table_info(orders)")}
    assert "eval_status" not in ocols
    idx = {
        i[0] for i in con.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert "ux_orders_active_amount" in idx  # survives the downgrade rebuild too
    con.close()

    r = _alembic(db, "upgrade", "head")
    assert r.returncode == 0, r.stderr


def test_migration_0013_additive_and_index_survives(tmp_path):
    import sqlite3

    db = tmp_path / "m16.db"
    r = _alembic(db, "upgrade", "0023_order_eval_status")
    assert r.returncode == 0, r.stderr

    # seed a store against the 0023 schema (no visibility yet)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO merchants (id, wallet_address, created_at) VALUES (1,'0xabc','2026')"
    )
    con.execute(
        "INSERT INTO stores (id, slug, merchant_id, status, pay_to, theme, created_at,"
        " updated_at) VALUES (1,'s',1,'live','0xabc','original.html','2026','2026')"
    )
    con.commit()
    con.close()

    r = _alembic(db, "upgrade", "head")
    assert r.returncode == 0, r.stderr
    con = sqlite3.connect(db)
    scols = {c[1] for c in con.execute("PRAGMA table_info(stores)")}
    assert "visibility" in scols
    # server_default backfills the existing store to 'public' (unchanged discovery)
    assert (
        con.execute("SELECT visibility FROM stores WHERE id=1").fetchone()[0]
        == "public"
    )
    idx = {
        i[0] for i in con.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    # a stores column never touches orders — the M3 partial unique index is intact
    assert "ux_orders_active_amount" in idx
    con.close()

    r = _alembic(db, "downgrade", "0023_order_eval_status")
    assert r.returncode == 0, r.stderr
    con = sqlite3.connect(db)
    scols = {c[1] for c in con.execute("PRAGMA table_info(stores)")}
    assert "visibility" not in scols
    con.close()

    r = _alembic(db, "upgrade", "head")
    assert r.returncode == 0, r.stderr


def test_migration_0025_additive_and_index_survives(tmp_path):
    import sqlite3

    db = tmp_path / "m25.db"
    r = _alembic(db, "upgrade", "0024_store_visibility")
    assert r.returncode == 0, r.stderr

    # seed a delivered order against the 0024 schema (no reviews table yet)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO merchants (id, wallet_address, created_at) VALUES (1,'0xabc','2026')"
    )
    con.execute(
        "INSERT INTO stores (id, slug, merchant_id, status, pay_to, theme, created_at,"
        " updated_at) VALUES (1,'s',1,'live','0xabc','original.html','2026','2026')"
    )
    con.execute(
        "INSERT INTO orders (id, store_id, pay_to, amount_micro, expected_micro,"
        " paid_micro, overpaid_micro, status, created_at)"
        " VALUES ('m25ord',1,'0xabc',9000000,9000123,9000123,0,'delivered','2026')"
    )
    con.commit()
    con.close()

    r = _alembic(db, "upgrade", "head")
    assert r.returncode == 0, r.stderr
    assert "reviews" in _table_names(db)

    con = sqlite3.connect(db)
    rddl = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='reviews'"
    ).fetchone()[0]
    assert "uq_reviews_order_id" in rddl  # one review per completed order, ever
    assert "ck_reviews_rating" in rddl  # rating bounded 1..5
    idx = {
        i[0] for i in con.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert "ix_reviews_store_id" in idx
    # a new table never touches orders — the M3 partial unique index is intact
    assert "ux_orders_active_amount" in idx
    con.close()

    # up-down-up: the partial index must survive the downgrade + re-upgrade
    r = _alembic(db, "downgrade", "0024_store_visibility")
    assert r.returncode == 0, r.stderr
    assert "reviews" not in _table_names(db)
    con = sqlite3.connect(db)
    idx = {
        i[0] for i in con.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert "ux_orders_active_amount" in idx  # untouched by the downgrade too
    con.close()

    r = _alembic(db, "upgrade", "head")
    assert r.returncode == 0, r.stderr
    assert "reviews" in _table_names(db)


def test_fresh_schema_has_marketplace_and_receipts():
    # Fresh create_all (conftest) must match the migrated schema: the new column +
    # table exist on the in-process engine used by the app/tests.
    from sqlalchemy import inspect as _inspect

    insp = _inspect(engine)
    scols = {c["name"] for c in insp.get_columns("stores")}
    assert {"marketplace_status", "marketplace_listed_at"} <= scols
    assert "screening_receipts" in insp.get_table_names()
    # M11 attestation columns + worker index present on the fresh schema too
    ocols = {c["name"] for c in insp.get_columns("orders")}
    assert {"attestation_uid", "attest_tx", "attest_status"} <= ocols
    oidx = {i["name"] for i in insp.get_indexes("orders")}
    assert "ix_orders_attest_status" in oidx
    # M13 growth tables + referrer_addr column present on the fresh schema too
    assert {
        "affiliate_accruals",
        "affiliate_payouts",
        "email_subscribers",
        "acp_sessions",
    } <= set(insp.get_table_names())
    assert "referrer_addr" in ocols
    assert "ix_orders_referrer_addr" in oidx


def test_migration_0002_downgrade_backfills_null_baseline(tmp_path):
    import sqlite3

    db = tmp_path / "nullbase.db"
    r = _alembic(db, "upgrade", "head")
    assert r.returncode == 0, r.stderr

    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO merchants (id, wallet_address, created_at) VALUES (1,'0xabc','2026')"
    )
    con.execute(
        "INSERT INTO stores (id, slug, merchant_id, status, pay_to, theme, created_at,"
        " updated_at) VALUES (1,'s',1,'live','0xabc','original.html','2026','2026')"
    )
    # an M3 order leaves baseline_micro NULL, as the live app now does
    con.execute(
        "INSERT INTO orders (id, store_id, pay_to, amount_micro, expected_micro,"
        " paid_micro, overpaid_micro, baseline_micro, status, created_at)"
        " VALUES ('m3ord',1,'0xabc',9000000,9000123,0,0,NULL,'pending','2026')"
    )
    con.commit()
    con.close()

    # downgrade restores baseline_micro NOT NULL; without the backfill it aborts
    r = _alembic(db, "downgrade", "0001_persistence_core")
    assert r.returncode == 0, r.stderr

    con = sqlite3.connect(db)
    val = con.execute("SELECT baseline_micro FROM orders WHERE id='m3ord'").fetchone()[
        0
    ]
    assert val == 0  # NULL backfilled before the NOT NULL restore
    con.close()


# ---------- model CRUD ----------
def test_model_crud_roundtrip():
    addr_mixed = "0x" + "Ab" * 20
    with SessionLocal() as s:
        merchant = get_or_create_merchant(s, addr_mixed)
        # get-or-create is idempotent and normalizes case: a differently-cased
        # form of the same address returns the same row, not a second merchant.
        again = get_or_create_merchant(s, addr_mixed.lower())
        assert again.id == merchant.id
        assert merchant.wallet_address == addr_mixed.lower()

        store = Store(
            slug="crud",
            merchant_id=merchant.id,
            status="live",
            pay_to=merchant.wallet_address,
            theme="original.html",
        )
        s.add(store)
        s.flush()
        product = Product(
            store_id=store.id, name="Widget", price_micro=1_000_000, active=True
        )
        s.add(product)
        s.flush()
        s.add(
            Order(
                id="orderid00000001",
                store_id=store.id,
                product_id=product.id,
                pay_to=store.pay_to,
                amount_micro=1_000_000,
                baseline_micro=0,
                status="pending",
            )
        )
        s.flush()
        s.add(Delivery(order_id="orderid00000001", payload="thanks"))
        s.commit()

    with SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(Merchant)) == 1
        order = s.get(Order, "orderid00000001")
        assert order.amount_micro == 1_000_000
        assert order.status == "pending"
        delivery = s.scalar(select(Delivery).where(Delivery.order_id == order.id))
        assert delivery.payload == "thanks"
        assert delivery.kind == "text"


def test_product_price_micro_must_be_positive():
    with SessionLocal() as s:
        merchant = get_or_create_merchant(s, "0x" + "c" * 40)
        store = Store(
            slug="badprice",
            merchant_id=merchant.id,
            status="live",
            pay_to=merchant.wallet_address,
            theme="original.html",
        )
        s.add(store)
        s.flush()
        s.add(Product(store_id=store.id, name="X", price_micro=0, active=True))
        with pytest.raises(IntegrityError):
            s.commit()


# ---------- order survives a simulated process restart ----------
def test_order_survives_simulated_restart(make_store):
    make_store(
        slug="persist-store",
        pay_to="0x" + "d" * 40,
        price_micro=5_000_000,
        delivery="download here",
    )
    client = TestClient(main.app)

    cid = client.post("/api/checkout/persist-store").json()["id"]

    # simulated restart: drop the connection pool — in-memory state is gone but
    # the data is on disk, so a fresh connection reads the same order.
    engine.dispose()
    poll = client.get(f"/api/checkout/{cid}")
    assert poll.status_code == 200
    assert poll.json()["status"] == "pending"

    # payment lands (confirmed on-chain, delivered)
    _confirm(cid)
    paid = client.get(f"/api/checkout/{cid}").json()
    assert paid["status"] == "paid"  # delivered, surfaced as "paid" to clients
    assert paid["delivery"] == "download here"

    # restart again: still terminal, delivery still recorded (read from deliveries)
    engine.dispose()
    after = client.get(f"/api/checkout/{cid}").json()
    assert after["status"] == "paid"
    assert after["delivery"] == "download here"


# ---------- delivery transition is idempotent ----------
def test_paid_flow_is_idempotent(make_store):
    make_store(
        slug="idem-store",
        pay_to="0x" + "e" * 40,
        price_micro=3_000_000,
        delivery="thanks!",
    )
    client = TestClient(main.app)
    cid = client.post("/api/checkout/idem-store").json()["id"]

    _confirm(cid)
    first = client.get(f"/api/checkout/{cid}").json()
    second = client.get(f"/api/checkout/{cid}").json()
    assert first["status"] == second["status"] == "paid"
    assert first["delivery"] == second["delivery"] == "thanks!"

    with SessionLocal() as s:
        n = s.scalar(
            select(func.count()).select_from(Delivery).where(Delivery.order_id == cid)
        )
        assert n == 1  # UNIQUE(order_id): no double delivery on repeat polls
        assert s.get(Order, cid).paid_at is not None


# ---------- event_log audit spine ----------
def test_event_log_records_order_lifecycle(make_store):
    make_store(slug="events-store", pay_to="0x" + "f" * 40, price_micro=2_000_000)
    client = TestClient(main.app)
    cid = client.post("/api/checkout/events-store").json()["id"]
    _confirm(cid)

    with SessionLocal() as s:
        events = [
            e.event for e in s.scalars(select(EventLog).where(EventLog.order_id == cid))
        ]
    assert "order.created" in events
    assert "order.confirmed" in events
    assert "order.delivered" in events


def test_event_log_is_insert_only_in_source():
    # The audit spine is append-only by construction: nothing in the app writes
    # an UPDATE or DELETE against event_log / EventLog.
    app_dir = REPO / "app"
    for py in app_dir.glob("*.py"):
        for line in py.read_text(encoding="utf-8").splitlines():
            if "EventLog" in line:
                low = line.lower()
                assert "delete" not in low and "update" not in low, f"{py.name}: {line}"

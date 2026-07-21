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

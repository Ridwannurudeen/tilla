"""M12 ops tests: the /ready readiness probe, the RPC concurrency cap + request-
path timeout, the Anthropic-outage 503, LLM usage logging, and shell-script
hygiene. All RPC/LLM/network is mocked — no real network, no real sleeps.
"""

import json
import logging
import pathlib
import shutil
import subprocess
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

import app.main as main
from app import chain, checkout, config, engine
from app.db import SessionLocal
from app.db import engine as db_engine
from app.db import expected_migration_head
from app.models import EventLog, Order, Product, Store

client = TestClient(main.app)

TX1 = "0x" + "1" * 64
TX2 = "0x" + "2" * 64
FROM = "0x" + "7" * 40


# ------------------------------------------------------------- helpers
def _set_head(value: str) -> None:
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS alembic_version "
                "(version_num VARCHAR(32) NOT NULL)"
            )
        )
        conn.execute(text("DELETE FROM alembic_version"))
        conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:v)"),
            {"v": value},
        )


def _drop_head() -> None:
    with db_engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))


@pytest.fixture(autouse=True)
def _reset_alembic_version():
    """alembic_version lives outside Base.metadata, so the conftest _clean_db
    fixture never touches it — reset it around every test so /ready starts clean."""
    _drop_head()
    yield
    _drop_head()


def _log(to_addr, value, tx_hash, log_index, block, from_addr=FROM):
    return {
        "address": config.USDT0,
        "topics": [
            config.TRANSFER_TOPIC,
            chain.pad_address(from_addr),
            chain.pad_address(to_addr),
        ],
        "data": hex(value),
        "transactionHash": tx_hash,
        "logIndex": hex(log_index),
        "blockNumber": hex(block),
    }


def _receipt(logs, block, status="0x1"):
    return {"status": status, "blockNumber": hex(block), "logs": logs}


def _detected_order(store_id, pay_to, expected, block):
    """Insert an order in 'detected' status (block_number set) so refresh_order
    takes its depth-sensitive branch and makes an eth_blockNumber call."""
    with SessionLocal() as s:
        product_id = s.scalar(select(Product.id).where(Product.store_id == store_id))
        oid = "det" + str(block).rjust(13, "0")
        s.add(
            Order(
                id=oid,
                store_id=store_id,
                product_id=product_id,
                pay_to=pay_to,
                amount_micro=expected,
                expected_micro=expected,
                paid_micro=expected,
                status="detected",
                block_number=block,
                tx_hash=TX1,
                expires_at=checkout._now(),
            )
        )
        s.commit()
        return oid


# ================================ /ready ================================
def test_ready_ok():
    _set_head(expected_migration_head())
    r = client.get("/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is True
    assert body["checks"]["db"] == "ok"
    assert body["checks"]["migrations"] == expected_migration_head()
    # conftest disables the sweeper, so both heartbeat checks report disabled.
    assert body["checks"]["sweeper"] == "disabled"
    assert body["checks"]["rpc"] == "disabled"


def test_ready_migration_table_missing_is_503():
    _drop_head()  # explicit: no alembic_version at all
    r = client.get("/ready")
    assert r.status_code == 503
    assert r.json()["ready"] is False
    assert r.json()["checks"]["migrations"].startswith("expected ")


def test_ready_migration_wrong_revision_is_503():
    _set_head("0001_persistence_core")
    r = client.get("/ready")
    assert r.status_code == 503
    migrations = r.json()["checks"]["migrations"]
    assert "0001_persistence_core" in migrations
    assert expected_migration_head() in migrations


def test_ready_db_down_is_clean_503(monkeypatch):
    """A DB failure yields a clean 503 JSON — /ready never raises."""
    _set_head(expected_migration_head())

    class _BoomSession:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **k):
            from sqlalchemy.exc import OperationalError

            raise OperationalError("SELECT 1", {}, Exception("database is locked"))

    monkeypatch.setattr(main, "SessionLocal", lambda: _BoomSession())
    r = client.get("/ready")
    assert r.status_code == 503
    assert r.json()["ready"] is False
    assert r.json()["checks"]["db"] == "error"


def test_ready_sweeper_and_rpc_stale(monkeypatch):
    _set_head(expected_migration_head())
    monkeypatch.setattr(config, "SWEEP_ENABLED", True)

    # aged heartbeats -> both stale -> 503 naming them
    monkeypatch.setattr(checkout, "LAST_TICK_MONO", time.monotonic() - 100_000)
    monkeypatch.setattr(checkout, "LAST_HEAD_MONO", time.monotonic() - 100_000)
    r = client.get("/ready")
    assert r.status_code == 503
    assert r.json()["checks"]["sweeper"] == "stale"
    assert r.json()["checks"]["rpc"] == "stale"

    # fresh heartbeats -> both ok -> 200
    monkeypatch.setattr(checkout, "LAST_TICK_MONO", time.monotonic())
    monkeypatch.setattr(checkout, "LAST_HEAD_MONO", time.monotonic())
    r = client.get("/ready")
    assert r.status_code == 200
    assert r.json()["checks"]["sweeper"] == "ok"
    assert r.json()["checks"]["rpc"] == "ok"


# ====================== RPC concurrency cap + timeout ======================
def test_rpc_concurrency_cap(make_store, monkeypatch):
    """Saturate the RPC semaphore: chain.block_number fails fast with ChainBusy,
    the /tx route maps it to 502, and the status GET degrades to DB state (200)."""
    pay_to = "0x" + "a" * 40
    make_store(slug="cap", pay_to=pay_to, price_micro=9_000_000)
    cid = client.post("/api/checkout/cap").json()["id"]
    with SessionLocal() as s:
        store_id = s.scalar(select(Store.id).where(Store.slug == "cap"))
        expected = s.get(Order, cid).expected_micro
    did = _detected_order(store_id, pay_to, expected + 1, 100)

    monkeypatch.setattr(config, "RPC_ACQUIRE_TIMEOUT", 0.05)
    n = config.RPC_MAX_CONCURRENT
    for _ in range(n):
        assert chain._RPC_SEMAPHORE.acquire(blocking=False)
    try:
        with pytest.raises(chain.ChainBusy):
            chain.block_number()
        # /tx: ChainBusy is a ChainError, mapped to 502 by the existing handler.
        assert (
            client.post(f"/api/checkout/{cid}/tx", json={"tx_hash": TX1}).status_code
            == 502
        )
        # status GET: refresh_order swallows the ChainBusy and serves DB state.
        r = client.get(f"/api/checkout/{did}")
        assert r.status_code == 200
        assert r.json()["status"] == "detected"
    finally:
        for _ in range(n):
            chain._RPC_SEMAPHORE.release()


def test_request_path_rpc_timeout(make_store, monkeypatch):
    """refresh_order and verify_txhash use RPC_TIMEOUT_REQUEST (5s); a bare
    block_number() (the sweeper's call site) passes None -> RPC_TIMEOUT (10s)."""
    pay_to = "0x" + "b" * 40
    make_store(slug="to", pay_to=pay_to, price_micro=9_000_000)

    recorded: list[tuple[str, float | None]] = []
    head = 200

    def fake_rpc(method, params, timeout=None):
        recorded.append((method, timeout))
        if method == "eth_blockNumber":
            return hex(head)
        if method == "eth_getTransactionReceipt":
            return _receipt([_log(pay_to, EXPECTED[0], TX2, 0, 100)], 100)
        return None

    EXPECTED = [0]
    monkeypatch.setattr(chain, "_rpc", fake_rpc)

    # (A) refresh_order on a detected order below depth -> one block_number(5s) call
    did = _detected_order(
        s_id := _store_id("to"),
        pay_to,
        9_000_100,
        head,  # block==head -> depth 0
    )
    with SessionLocal() as s:
        checkout.refresh_order(s, s.get(Order, did))
    assert ("eth_blockNumber", config.RPC_TIMEOUT_REQUEST) in recorded

    # (B) verify_txhash -> receipt(5s) + block_number(5s)
    recorded.clear()
    with SessionLocal() as s:
        store = s.get(Store, s_id)
        product = s.scalar(select(Product).where(Product.store_id == s_id))
        order = checkout.create_order(s, store, product)
        EXPECTED[0] = order.expected_micro
        s.commit()
        checkout.verify_txhash(s, order, TX2)
    assert ("eth_getTransactionReceipt", config.RPC_TIMEOUT_REQUEST) in recorded
    assert ("eth_blockNumber", config.RPC_TIMEOUT_REQUEST) in recorded

    # (C) the sweeper's bare call passes None (-> RPC_TIMEOUT inside _rpc)
    recorded.clear()
    chain.block_number()
    assert ("eth_blockNumber", None) in recorded
    assert config.RPC_TIMEOUT == 10.0
    assert config.RPC_TIMEOUT_REQUEST == 5.0


def _store_id(slug):
    with SessionLocal() as s:
        return s.scalar(select(Store.id).where(Store.slug == slug))


# ====================== Anthropic outage -> 503 (no fake store) ==========
def _mock_post(monkeypatch, fn):
    monkeypatch.setattr(engine.requests, "post", fn)


def test_generation_503_on_connection_error(monkeypatch):
    monkeypatch.setenv("TILLA_LLM_KEY", "test-key")
    monkeypatch.setattr(engine, "LLM_RETRY_SLEEP_SEC", 0)
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise engine.requests.ConnectionError("anthropic down")

    _mock_post(monkeypatch, boom)
    r = client.post("/create-store", json={"description": "I sell socks"})
    assert r.status_code == 503
    assert r.headers["Retry-After"] == "60"
    assert calls["n"] == 2  # initial + one retry
    with SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(Store)) == 0  # no fake store


def test_generation_503_on_529_retries(monkeypatch):
    monkeypatch.setenv("TILLA_LLM_KEY", "test-key")
    monkeypatch.setattr(engine, "LLM_RETRY_SLEEP_SEC", 0)
    calls = {"n": 0}

    class Resp529:
        status_code = 529

        def raise_for_status(self):
            raise engine.requests.HTTPError("overloaded")

        def json(self):
            return {}

    def overloaded(*a, **k):
        calls["n"] += 1
        return Resp529()

    _mock_post(monkeypatch, overloaded)
    r = client.post("/create-store", json={"description": "I sell socks"})
    assert r.status_code == 503
    assert calls["n"] == 2  # 529 is transient -> one retry


def test_generation_503_on_401_no_retry(monkeypatch):
    monkeypatch.setenv("TILLA_LLM_KEY", "test-key")
    monkeypatch.setattr(engine, "LLM_RETRY_SLEEP_SEC", 0)
    calls = {"n": 0}

    class Resp401:
        status_code = 401

        def raise_for_status(self):
            raise engine.requests.HTTPError("unauthorized")

        def json(self):
            return {}

    def bad_key(*a, **k):
        calls["n"] += 1
        return Resp401()

    _mock_post(monkeypatch, bad_key)
    r = client.post("/create-store", json={"description": "I sell socks"})
    assert r.status_code == 503
    assert calls["n"] == 1  # non-transient 4xx -> single attempt, no retry


def test_generation_usage_logged(make_store, monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("TILLA_LLM_KEY", "test-key")
    monkeypatch.setattr(engine, "STORES_DIR", tmp_path)

    class Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "content": [
                    {"text": json.dumps({"store_name": "Usage Co", "price_usdt": 5})}
                ],
                "usage": {"input_tokens": 123, "output_tokens": 45},
            }

    _mock_post(monkeypatch, lambda *a, **k: Resp())
    # screening must ALLOW so the store goes live and store.created fires.
    import respx

    with respx.mock:
        import httpx

        respx.post(config.WARDEN_SCREEN_URL).mock(
            return_value=httpx.Response(200, json={"verdict": "ALLOW"})
        )
        with caplog.at_level(logging.INFO, logger="tilla"):
            r = client.post("/create-store", json={"description": "i sell a thing"})
    assert r.status_code == 200
    assert "llm usage:" in caplog.text
    with SessionLocal() as s:
        ev = s.scalar(select(EventLog).where(EventLog.event == "store.created"))
        assert ev is not None
        assert ev.data["llm_in"] == 123
        assert ev.data["llm_out"] == 45
    # the usage sidecar keys never leak into persisted store content
    with SessionLocal() as s:
        store = s.scalar(select(Store).where(Store.slug != ""))
        assert "_llm_in" not in (store.content or {})


# ============================ shell-script hygiene ========================
_SHELL_SCRIPTS = (
    "watchdog.sh",
    "backup_db.sh",
    "backup_offsite.sh",
    "restore_drill.sh",
)


def _scripts_dir():
    return pathlib.Path(__file__).resolve().parent.parent / "scripts"


def test_shell_scripts_are_lf_only():
    for name in _SHELL_SCRIPTS:
        data = (_scripts_dir() / name).read_bytes()
        assert b"\r\n" not in data, f"{name} has CRLF line endings"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not on PATH")
def test_shell_scripts_pass_bash_syntax_check():
    for name in _SHELL_SCRIPTS:
        path = _scripts_dir() / name
        res = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
        assert res.returncode == 0, f"{name}: {res.stderr}"

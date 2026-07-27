import os
import pathlib
import tempfile

import pytest

# Point the app at throwaway paths and disable the paywall + LLM generation
# BEFORE any test module imports app.config / app.db / app.main. The DB path in
# particular must be set here, or app.db would build its engine against the
# default /opt/tilla/tilla.db. Mirrors the OKX/LLM key pops below.
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="tilla-tests-"))
os.environ["TILLA_DB_PATH"] = str(_TMP / "test.db")
os.environ["TILLA_STORES_DIR"] = str(_TMP / "stores")
# M4 gated delivery: a throwaway files dir + a signing key so the gated endpoints
# are enabled by default (a fail-closed test monkeypatches config.SIGNING_KEY="").
os.environ["TILLA_FILES_DIR"] = str(_TMP / "deliverables")
os.environ["TILLA_SIGNING_KEY"] = "test-signing-key-0123456789abcdef0123456789abcdef"
# Never start the background sweeper / hit the RPC network from tests; the M3
# tests drive chain.py directly through respx-mocked httpx instead.
os.environ["TILLA_SWEEP_ENABLED"] = "0"
os.environ.pop("OKX_API_KEY", None)
os.environ.pop("TILLA_LLM_KEY", None)

import app.models  # noqa: E402,F401 — registers every table on Base.metadata
from app.db import Base, engine  # noqa: E402

Base.metadata.create_all(engine)


@pytest.fixture(autouse=True)
def _clean_db():
    """Start each test against empty tables so rows never leak between tests."""
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
    yield


@pytest.fixture(autouse=True)
def _sync_imagery(monkeypatch):
    """Photography resolves inline during tests. In production it runs in a
    background thread AFTER create_store returns (IMAGERY_ASYNC) — a thread that
    outlives a test would run against torn-down monkeypatches and could reach the
    real network. Tests that exercise the async path flip this back deliberately
    with their own controlled worker."""
    monkeypatch.setattr("app.engine.IMAGERY_ASYNC", False)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """slowapi's in-memory limiter is a module-level singleton keyed by
    client IP; every TestClient call shares one IP, so without a reset each
    test would inherit rate-limit state left over by the previous one."""
    import app.main as main

    main.limiter.reset()
    yield
    main.limiter.reset()


@pytest.fixture
def make_store():
    """Factory that inserts a store (+ merchant + one active product) straight
    into the DB, the way an import or a resumed create leaves it."""
    from app.db import SessionLocal
    from app.models import Product, Store, get_or_create_merchant

    def _make(
        slug="test-store",
        pay_to="0x" + "a" * 40,
        price_micro=9_000_000,
        delivery="Here is your download link.",
        status="live",
    ):
        with SessionLocal() as session:
            merchant = get_or_create_merchant(session, pay_to)
            store = Store(
                slug=slug,
                merchant_id=merchant.id,
                status=status,
                pay_to=pay_to,
                delivery=delivery,
                theme="original.html",
            )
            session.add(store)
            session.flush()
            session.add(
                Product(
                    store_id=store.id,
                    name="Thing",
                    price_micro=price_micro,
                    active=True,
                )
            )
            session.commit()
            return store.id

    return _make

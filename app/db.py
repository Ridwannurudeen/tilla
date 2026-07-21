"""SQLAlchemy engine + session for Tilla's sqlite store.

Sync engine (endpoints are plain `def`, so FastAPI runs them in its threadpool);
`check_same_thread=False` lets the pooled connection cross threads. A connect
listener puts every new connection into WAL with foreign keys enforced and a
bounded busy timeout, so concurrent polling serializes instead of erroring.
"""

import pathlib
from collections.abc import Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app import config

engine = create_engine(
    f"sqlite:///{config.DB_PATH}",
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


# ---------- M12 migration-head readiness helpers ----------
_EXPECTED_HEAD: str | None = None


def expected_migration_head() -> str:
    """The single migration head Alembic expects, computed once from the
    ScriptDirectory and cached module-level. Returns ``"unknown"`` (non-fatal:
    /ready reports it but does not fail readiness) if alembic.ini is absent or the
    revisions can't be read. Uses an absolute script_location so the result never
    depends on the process CWD."""
    global _EXPECTED_HEAD
    if _EXPECTED_HEAD is not None:
        return _EXPECTED_HEAD
    root = pathlib.Path(__file__).resolve().parent.parent
    if not (root / "alembic.ini").exists():
        _EXPECTED_HEAD = "unknown"
        return _EXPECTED_HEAD
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config(str(root / "alembic.ini"))
        cfg.set_main_option("script_location", str(root / "alembic"))
        cfg.set_main_option("path_separator", "os")  # silence 1.18 split-legacy warn
        _EXPECTED_HEAD = (
            ScriptDirectory.from_config(cfg).get_current_head() or "unknown"
        )
    except Exception:
        _EXPECTED_HEAD = "unknown"
    return _EXPECTED_HEAD


def current_migration_head(session: Session) -> str | None:
    """The revision recorded in ``alembic_version``, or ``None`` when the table is
    absent (never migrated) or empty — which /ready reads as a migration mismatch."""
    try:
        row = session.execute(text("SELECT version_num FROM alembic_version")).first()
    except Exception:
        return None
    return row[0] if row else None

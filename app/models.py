"""SQLAlchemy ORM models — the M2 persistence core.

One row per merchant (wallet identity), store, product, order, and delivery,
plus an append-only ``event_log`` audit spine. Money is stored as integer USDT0
base units (6 decimals) so payment matching stays exact — no float drift.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
    text,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Merchant(Base):
    __tablename__ = "merchants"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    wallet_address: Mapped[str] = mapped_column(String(42), nullable=False, unique=True)
    api_key_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )


class Store(Base):
    __tablename__ = "stores"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="live")
    pay_to: Mapped[str] = mapped_column(String(42), nullable=False)
    delivery: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    theme: Mapped[str] = mapped_column(
        String(40), nullable=False, default="original.html"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("price_micro > 0", name="ck_products_price_micro_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    price_micro: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_store_status", "store_id", "status"),
        Index("ix_orders_created_at", "created_at"),
        # HARD BACKSTOP against the check-then-insert race in amount allocation:
        # at most one order per (pay_to, expected_micro) may hold a reservation.
        # An expired order stays inside the index (via status='expired') so a
        # late tx for that amount can only ever match the original order.
        Index(
            "ux_orders_active_amount",
            "pay_to",
            "expected_micro",
            unique=True,
            sqlite_where=text(
                "status IN ('pending','detected','underpaid','expired','late_paid')"
            ),
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id"), nullable=True
    )
    pay_to: Mapped[str] = mapped_column(String(42), nullable=False)
    amount_micro: Mapped[int] = mapped_column(Integer, nullable=False)
    # The exact micro-USDT a buyer must send = price + unique offset; payment
    # matching is on this, not amount_micro. Backfilled to amount_micro for
    # pre-M3 orders (offset 0 -> exact-price matching).
    expected_micro: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    # Cumulative verified inbound (underpay accumulates here); overage recorded.
    paid_micro: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    overpaid_micro: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    # baseline_micro is legacy (balance-delta): kept nullable for rollback
    # insurance, no longer populated by M3 code.
    baseline_micro: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    tx_hash: Mapped[str | None] = mapped_column(String(66), nullable=True)
    from_addr: Mapped[str | None] = mapped_column(String(42), nullable=True)
    block_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Delivery(Base):
    __tablename__ = "deliveries"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(
        ForeignKey("orders.id"), nullable=False, unique=True
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="text")
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    delivered_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )


class ProcessedTransfer(Base):
    """One row per consumed on-chain Transfer. UNIQUE(tx_hash, log_index) makes a
    replayed sweep window or a re-submitted txhash a no-op — one transfer pays
    exactly one order."""

    __tablename__ = "processed_transfers"
    __table_args__ = (
        UniqueConstraint("tx_hash", "log_index", name="uq_processed_tx_log"),
        Index("ix_processed_transfers_order_id", "order_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tx_hash: Mapped[str] = mapped_column(String(66), nullable=False)
    log_index: Mapped[int] = mapped_column(Integer, nullable=False)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), nullable=False)
    pay_to: Mapped[str] = mapped_column(String(42), nullable=False)
    from_addr: Mapped[str] = mapped_column(String(42), nullable=False)
    amount_micro: Mapped[int] = mapped_column(Integer, nullable=False)
    block_number: Mapped[int] = mapped_column(Integer, nullable=False)
    seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


class ChainCursor(Base):
    """Single-row sweep cursor: the last block the sweeper has fully processed.
    Persisting it makes restarts resume gap-free."""

    __tablename__ = "chain_cursor"
    __table_args__ = (CheckConstraint("id = 1", name="ck_chain_cursor_singleton"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    last_block: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )


class EventLog(Base):
    __tablename__ = "event_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    event: Mapped[str] = mapped_column(String(60), nullable=False)
    store_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    order_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True)


def get_or_create_merchant(session: Session, wallet_address: str) -> Merchant:
    """Return the merchant for `wallet_address` (lowercased), creating it once.
    Flushes so the caller can use `merchant.id` as a foreign key immediately."""
    addr = wallet_address.lower()
    merchant = session.scalar(select(Merchant).where(Merchant.wallet_address == addr))
    if merchant is None:
        merchant = Merchant(wallet_address=addr)
        session.add(merchant)
        session.flush()
    return merchant


def log_event(
    session: Session,
    source: str,
    event: str,
    *,
    store_id: int | None = None,
    order_id: str | None = None,
    data: dict | None = None,
) -> None:
    """Append one audit row. The only write path for event_log — insert-only,
    never updated or deleted, so the log stays an accurate history."""
    session.add(
        EventLog(
            source=source,
            event=event,
            store_id=store_id,
            order_id=order_id,
            data=data,
        )
    )

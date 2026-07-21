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
    # M9 outbound webhook (one per merchant). webhook_secret is plaintext because
    # Tilla signs each delivery with it (HMAC); the DB is server-owned like .env
    # and it is never logged or exported. NULL until the merchant registers a URL.
    webhook_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    webhook_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )


class Store(Base):
    __tablename__ = "stores"
    __table_args__ = (
        # M10 marketplace listing lifecycle. The runbook's mark_listed command is
        # the only writer; the CHECK is defense-in-depth against a bad value.
        CheckConstraint(
            "marketplace_status IN "
            "('unlisted','prepared','submitted','listed','rejected')",
            name="ck_stores_marketplace_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="live")
    pay_to: Mapped[str] = mapped_column(String(42), nullable=False)
    # M10 marketplace citizenship: where this store stands on the #6961 listing.
    # 'unlisted' (default, every existing store) until the user-gated runbook lists
    # it and the mark_listed command records the transition. Read-only in the app —
    # no HTTP route mutates it (the on-chain listing is out-of-band + approval-gated).
    marketplace_status: Mapped[str] = mapped_column(
        String(12), nullable=False, default="unlisted", server_default="unlisted"
    )
    marketplace_listed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    # sha256 hex of the per-store manage key (capability secret handed to the
    # paid create-store caller once). NULL for legacy stores until minted.
    manage_key_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
        CheckConstraint(
            "pricing_model IN ('one_time','batch','metered','subscription')",
            name="ck_products_pricing_model",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    price_micro: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # M8 payment methods: which x402/MPP rail this product declares. 'one_time'
    # (the default, every existing product) is byte-identical exact checkout;
    # 'batch' unlocks the aggr_deferred accepts-entry; 'metered' unlocks MPP
    # session channels; 'subscription' unlocks the period sidecar. pricing_params
    # holds the per-model config, validated by pydantic at the declaration seam.
    pricing_model: Mapped[str] = mapped_column(
        String(12), nullable=False, default="one_time", server_default="one_time"
    )
    pricing_params: Mapped[dict | None] = mapped_column(JSON, nullable=True)
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
        # Buyer library lookup: orders for a given on-chain payer wallet.
        Index("ix_orders_from_addr", "from_addr"),
        # x402 agent buys: the EIP-3009 authorization nonce is the idempotency /
        # replay key. UNIQUE dedupes order creation; SQLite allows unlimited NULLs
        # here, so human (web) orders — which never set it — are unaffected.
        Index("ux_orders_x402_nonce", "x402_nonce", unique=True),
        # M11 EAS attester worker query: pending orders awaiting attestation.
        Index("ix_orders_attest_status", "attest_status"),
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
    # M9 refunds: cumulative verified refund micro sent back by the merchant from
    # their own wallet (non-custodial). A full refund records paid_micro here and
    # flips status='refunded'; an overage refund records overpaid_micro and keeps
    # the delivered status. Server-computed, never client-supplied.
    refunded_micro: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    # baseline_micro is legacy (balance-delta): kept nullable for rollback
    # insurance, no longer populated by M3 code.
    baseline_micro: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # 'web' = human unique-amount checkout; 'agent' = x402 per-store buy. Marks
    # the reaper scope + analytics; defaulted so pre-0004 code inserts stay valid.
    channel: Mapped[str] = mapped_column(
        String(10), nullable=False, default="web", server_default="web"
    )
    # The EIP-3009 authorization nonce of an x402 agent buy (0x + 32 bytes), the
    # idempotency/replay key. NULL for human orders (see the unique index above).
    x402_nonce: Mapped[str | None] = mapped_column(String(66), nullable=True)
    tx_hash: Mapped[str | None] = mapped_column(String(66), nullable=True)
    from_addr: Mapped[str | None] = mapped_column(String(42), nullable=True)
    block_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Chain head captured at order creation. A buyer-submitted txhash whose
    # transfer is mined below this floor is a historical transfer, never this
    # order's payment; NULL when the head was unavailable at creation time.
    created_block: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Optional exchange-custody fallback: buyer supplies an email so delivery can
    # be re-sent via a magic link when the on-chain payer isn't reachable.
    buyer_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # M11 on-chain depth: the EAS receipt attestation for this order. attest_status
    # walks none -> pending -> sent -> attested | failed, driven ONLY by the dormant
    # attester worker (app.attest) — a single writer, so no CHECK constraint (which
    # would force an orders-table rebuild and endanger ux_orders_active_amount). Every
    # existing row stays 'none' (never queued), so enabling later attests only
    # post-enable sales. attest_tx is the on-chain attestation tx; attestation_uid is
    # the EAS UID parsed from its Attested log.
    attestation_uid: Mapped[str | None] = mapped_column(String(66), nullable=True)
    attest_tx: Mapped[str | None] = mapped_column(String(66), nullable=True)
    attest_status: Mapped[str] = mapped_column(
        String(12), nullable=False, default="none", server_default="none"
    )


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


class Deliverable(Base):
    """The merchant-supplied thing a buyer receives. One ACTIVE row per store;
    replacing inserts a new row and flips the old ``active=false`` so already-sold
    orders keep the version they bought (their Entitlement points at the old row).
    A store with no deliverable row falls back to the ``store.delivery`` text —
    that is the exact legacy behaviour."""

    __tablename__ = "deliverables"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('file','text','license')", name="ck_deliverables_kind"
        ),
        Index("ix_deliverables_store_id", "store_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    # Dormant M9 multi-product hook: stays NULL for the single-product store.
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    # The secret for kind='text'. NULL for file/license.
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Content-addressed storage: on-disk name is the sha256 hex, no user path.
    file_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Sanitized original name — Content-Disposition only, never a path component.
    file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mime: Mapped[str | None] = mapped_column(String(100), nullable=True)
    max_downloads: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="5"
    )
    link_ttl_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="86400"
    )
    # kind='license' only (default 3); NULL for file/text.
    max_activations: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )


class Entitlement(Base):
    """Binds one sale (order) to the deliverable it bought. UNIQUE(order_id) makes
    issuance idempotent under the same begin_nested pattern as the Delivery row.
    Counters are advanced by race-proof conditional UPDATEs, never a read-modify-
    write, so concurrent requests can't exceed a cap."""

    __tablename__ = "entitlements"
    __table_args__ = (
        UniqueConstraint("order_id", name="uq_entitlements_order_id"),
        UniqueConstraint("license_key", name="uq_entitlements_license_key"),
        Index("ix_entitlements_deliverable_id", "deliverable_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), nullable=False)
    deliverable_id: Mapped[int] = mapped_column(
        ForeignKey("deliverables.id"), nullable=False
    )
    # Snapshot of the on-chain payer at deliver time (order.from_addr).
    buyer_addr: Mapped[str | None] = mapped_column(String(42), nullable=True)
    download_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    activations_used: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    license_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )


class AuthNonce(Base):
    """Single-use buyer sign-in nonce. Consumed by a conditional UPDATE
    (used_at IS NULL), so a captured signature is worthless after first use.
    Expired rows are opportunistically deleted when new nonces are issued."""

    __tablename__ = "auth_nonces"
    __table_args__ = (Index("ix_auth_nonces_address", "address"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    address: Mapped[str] = mapped_column(String(42), nullable=False)
    nonce: Mapped[str] = mapped_column(String(32), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LicenseActivation(Base):
    """Audit + idempotency row for one device holding a license activation.
    UNIQUE(entitlement_id, device_id) makes re-activating the same device a
    no-op (no double count) and lets deactivate free exactly one slot."""

    __tablename__ = "license_activations"
    __table_args__ = (
        UniqueConstraint(
            "entitlement_id", "device_id", name="uq_license_activation_device"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    entitlement_id: Mapped[int] = mapped_column(
        ForeignKey("entitlements.id"), nullable=False
    )
    device_id: Mapped[str] = mapped_column(String(128), nullable=False)
    activated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


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


class Refund(Base):
    """One verified on-chain refund transfer credited to exactly one order. The
    merchant sends USDT0 back from their own wallet (non-custodial); Tilla only
    verifies the tx and records it here. UNIQUE(tx_hash, log_index) makes one
    on-chain transfer creditable to a single order ever (the ProcessedTransfer
    pattern), so a resubmit is a no-op and a tx claimed by another order 409s."""

    __tablename__ = "refunds"
    __table_args__ = (
        UniqueConstraint("tx_hash", "log_index", name="uq_refunds_tx_log"),
        CheckConstraint("kind IN ('full','overage')", name="ck_refunds_kind"),
        Index("ix_refunds_order_id", "order_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    tx_hash: Mapped[str] = mapped_column(String(66), nullable=False)
    log_index: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_micro: Mapped[int] = mapped_column(Integer, nullable=False)
    block_number: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )


class ScreeningReceipt(Base):
    """One content-screening event for a store — the typed, queryable money-record
    surface for M10 (the Refund-table precedent). EventLog stays the append-only
    audit spine (screening events still log there); this table is what the dashboard
    marketplace panel reads. ``mode='demo'`` rows carry no amount/tx (the free
    Warden demo endpoint); ``mode='paid'`` rows record the x402 hire amount and the
    on-chain settle ``tx_hash`` — real evidence of the agents-hiring-agents flow."""

    __tablename__ = "screening_receipts"
    __table_args__ = (
        CheckConstraint("mode IN ('demo','paid')", name="ck_screening_receipts_mode"),
        Index("ix_screening_receipts_store_id", "store_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    mode: Mapped[str] = mapped_column(String(10), nullable=False)
    verdict: Mapped[str] = mapped_column(String(10), nullable=False)
    risk_level: Mapped[str | None] = mapped_column(String(20), nullable=True)
    endpoint: Mapped[str] = mapped_column(String(200), nullable=False)
    amount_micro: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tx_hash: Mapped[str | None] = mapped_column(String(66), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )


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


class MppChannel(Base):
    """One MPP (Merchant Payment Protocol) pay-as-you-go session channel. State
    lives here, NOT in the SDK's FileStore, so every transition is a race-proof
    conditional UPDATE (the M3 pattern) and the state machine unit-tests as pure
    functions over this row with the SA client mocked. A channel is the only
    fund-moving act (open deposits real USDT into the escrow contract), so the
    endpoints stay fail-closed (503) until TILLA_MPP_ENABLED + SA creds are set —
    no synthetic close/settle is ever written."""

    __tablename__ = "mpp_channels"
    __table_args__ = (
        UniqueConstraint("channel_id", name="uq_mpp_channels_channel_id"),
        CheckConstraint(
            "status IN ('open','closing','closed','failed')",
            name="ck_mpp_channels_status",
        ),
        Index("ix_mpp_channels_store_id", "store_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    # The SA-issued channel/session id — the idempotency key for every transition.
    channel_id: Mapped[str] = mapped_column(String(80), nullable=False)
    buyer_addr: Mapped[str | None] = mapped_column(String(42), nullable=True)
    # NON-CUSTODIAL: always the merchant wallet, never Tilla (same as exact).
    pay_to: Mapped[str] = mapped_column(String(42), nullable=False)
    deposit_micro: Mapped[int] = mapped_column(Integer, nullable=False)
    spent_micro: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    unit_price_micro: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(10), nullable=False, default="open", server_default="open"
    )
    last_voucher_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )


class WebhookDelivery(Base):
    """Outbound webhook outbox row. Enqueued as a pure DB INSERT inside the SAME
    transaction as the state change it reports (no HTTP in a transaction), then
    dispatched by the ``webhook_loop`` background task with exponential backoff.
    ``status`` walks pending -> delivered | dead; ``attempts`` and
    ``next_attempt_at`` are advanced by a race-proof conditional UPDATE so a claim
    can only win once."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','delivered','dead')",
            name="ck_webhook_deliveries_status",
        ),
        Index("ix_webhook_deliveries_due", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id"), nullable=False)
    event: Mapped[str] = mapped_column(String(30), nullable=False)
    order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.id"), nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(
        String(10), nullable=False, default="pending", server_default="pending"
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )
    last_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


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

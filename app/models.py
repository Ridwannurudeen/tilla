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
        # Phase 4 custom domains: one hostname → one store, so a verified domain can
        # never be hijacked to a second store. SQLite treats the many unclaimed NULLs
        # as distinct, so unclaimed stores are unaffected.
        UniqueConstraint("custom_domain", name="uq_stores_custom_domain"),
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
    # Phase 1.7 sandbox/hidden mode: 'public' (default, every existing store) shows in
    # discovery / the aggregate feed / the sitemap; 'hidden' keeps a live store out of
    # those bulk surfaces while it stays fully reachable by direct link (owner + agent
    # preview). A hidden store auto-graduates to 'public' on its first delivered sale —
    # the "clears a threshold" gate — and the owner can toggle it from the dashboard.
    visibility: Mapped[str] = mapped_column(
        String(10), nullable=False, default="public", server_default="public"
    )
    # Phase 4 custom domains (app-side up to the DNS/TLS/nginx gate). A merchant
    # CLAIMS a hostname for their store; ownership is proven by a DNS TXT challenge.
    # custom_domain is the validated, lowercased hostname (NULL until claimed);
    # custom_domain_token is the per-claim challenge secret the owner publishes as a
    # TXT record; custom_domain_verified_at is set ONLY once the TXT lookup matched.
    # FAIL-CLOSED: host-based resolution serves a store only when verified_at is set,
    # so an unverified (or merely claimed) domain serves nothing. The operator still
    # provisions server_name + TLS out-of-band (docs/runbooks/custom-domains.md).
    custom_domain: Mapped[str | None] = mapped_column(String(253), nullable=True)
    custom_domain_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    custom_domain_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
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
    # Phase 1.3 SLA: the merchant's delivery-time promise in minutes, surfaced to
    # buyer agents as an ETA (feed.json / MCP). NULL = no per-product override, so
    # the agent surfaces fall back to the platform default DELIVERY_SLA_MINUTES;
    # every pre-1.3 product is NULL, an unchanged instant-delivery promise.
    sla_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
        # M13 affiliate attribution: orders carrying a referring agent's payout
        # wallet (first-write-wins, immutable after creation). The dashboard/summary
        # aggregates accruals, not this column, but the index keeps referrer lookups
        # cheap; SQLite allows unlimited NULLs, so unreferred orders are unaffected.
        Index("ix_orders_referrer_addr", "referrer_addr"),
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
    # M18 cross-chain: the settlement chain (CAIP-2) this order is pinned to at
    # creation. Every verification path (sweeper matching, verify_txhash, refunds)
    # resolves its RPC + asset from THIS value via payment.chain_for(), so a tx on
    # any other chain can never confirm it. Defaults + backfills to the canonical
    # X Layer ledger (eip155:196, INV-2); every pre-M18 order is that chain.
    network: Mapped[str] = mapped_column(
        String(20), nullable=False, default="eip155:196", server_default="eip155:196"
    )
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
    # M8 aggr_deferred reconciliation: the pending aggregated settle reference the
    # facilitator returns for a deferred (async) settle that has NOT yet confirmed
    # on-chain. Captured on the settling order so the reconciliation poller
    # (app.reconcile) can query get_settle_status(settle_ref) and finalize the order
    # only once its aggregated tx confirms. NULL for every other order (exact rail,
    # human orders) — the poller only ever touches rows carrying it.
    settle_ref: Mapped[str | None] = mapped_column(String(66), nullable=True)
    from_addr: Mapped[str | None] = mapped_column(String(42), nullable=True)
    # M13 affiliate attribution: the referring agent's payout wallet, captured at
    # order creation (first-write-wins, immutable afterwards). Lowercased + zero-
    # address-rejected at every capture path. NULL for unreferred orders (every
    # pre-M13 order), so the accrual ledger is only ever written for referred sales.
    referrer_addr: Mapped[str | None] = mapped_column(String(42), nullable=True)
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
    # Phase 1.6 evaluation window: a delivered order's buyer-evaluation state, the
    # basis for a dispute-aware success_rate. 'none' = untracked (every pre-1.6 order,
    # counted as good — unchanged legacy reputation); a NEW delivery sets 'pending';
    # the paying buyer may 'confirmed' (self-eval accept) or 'rejected' (dispute). A
    # 'pending' order auto-confirms by TIME at read (paid_at older than the window =
    # ACP 'skip' mode) — computed in the discovery query, so there is no poller and no
    # crash-window to reconcile. Single writer per order (deliver, then one buyer act).
    eval_status: Mapped[str] = mapped_column(
        String(12), nullable=False, default="none", server_default="none"
    )
    # M11 on-chain depth: the EAS receipt attestation for this order. attest_status
    # walks none -> pending -> sending -> sent -> attested | failed, driven ONLY by the
    # dormant attester worker (app.attest) — a single writer, so no CHECK constraint
    # (which would force an orders-table rebuild and endanger ux_orders_active_amount).
    # Every existing row stays 'none' (never queued), so enabling later attests only
    # post-enable sales. 'sending' records the broadcast intent (attest_tx + attest_nonce)
    # BEFORE the tx is broadcast, so a crash in the send window reconciles by nonce (at
    # most one tx per nonce mines) instead of blind-re-broadcasting. attest_tx is the
    # on-chain attestation tx; attest_nonce is the account nonce it was signed with;
    # attestation_uid is the EAS UID parsed from its Attested log. content_hash is the
    # sha256 (0x + 64 hex) of the delivered payload, computed by the attester at attest
    # time and recorded here as part of the broadcast intent (alongside attest_tx +
    # attest_nonce) so the receipt is provably bound to WHAT was delivered and a reconcile
    # re-broadcast rebuilds the identical attestation. NULL for every non-attesting row.
    attestation_uid: Mapped[str | None] = mapped_column(String(66), nullable=True)
    attest_tx: Mapped[str | None] = mapped_column(String(66), nullable=True)
    attest_nonce: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(66), nullable=True)
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
    # Versioned releases (0028): the release number within a (store, product, kind)
    # lineage. A plain replace inserts a fresh version 1 row (past buyers keep their
    # bought version); publishing a NEW VERSION inserts version = prev + 1, and a past
    # buyer's entitlement rolls forward to the highest active version of the same kind
    # (main._current_version), so they re-download the newest release. Every pre-0028
    # deliverable backfills to 1.
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
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


class Review(Base):
    """Phase 3 verified-buyer review — un-fakeable by construction. A row can only be
    written by the wallet that COMPLETED the purchase: the route requires an
    authenticated buyer session whose address equals ``order.from_addr`` on a
    TERMINAL_DELIVERED order. UNIQUE(order_id) caps it at one review per completed
    order (a duplicate 409s, the Entitlement precedent), and ``body`` is Warden-
    screened BEFORE insert so no unscreened text is ever stored or surfaced. Pure
    reputation — no code turns a row here into a fund movement. ``rating`` is bounded
    1..5 by a CHECK; ``product_id`` snapshots the ordered product (nullable, matching
    Order.product_id)."""

    __tablename__ = "reviews"
    __table_args__ = (
        UniqueConstraint("order_id", name="uq_reviews_order_id"),
        CheckConstraint("rating >= 1 AND rating <= 5", name="ck_reviews_rating"),
        Index("ix_reviews_store_id", "store_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id"), nullable=True
    )
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), nullable=False)
    from_addr: Mapped[str] = mapped_column(String(42), nullable=False)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
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
    """Per-chain sweep cursor: the last block the sweeper has fully processed on the
    chain whose ``chain_id`` is this row's primary key (``id`` == chain_id). One row
    per chain the sweeper scans — the canonical X Layer ledger (id 196) always, plus
    any flag+probe-enabled chain (18.2). Persisting it makes restarts resume gap-free,
    and keying it per-chain means a second chain's cursor can never rewind the
    canonical one. Pre-18.2 the table was a singleton pinned to id=1; migration
    0018 repoints that row to the canonical chain_id (196)."""

    __tablename__ = "chain_cursor"

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


class AffiliatePayout(Base):
    """One verified on-chain USDT0 payout the merchant/operator sent to a referring
    agent from their OWN wallet (non-custodial). Tilla NEVER moves funds — this row
    is verify-and-record only, byte-identical philosophy to the M9 Refund table.
    UNIQUE(tx_hash, log_index) makes one on-chain transfer creditable to a single
    payout ever (the ProcessedTransfer/Refund pattern), so a resubmit is a no-op and
    a tx already consumed by a refund/payout 409s."""

    __tablename__ = "affiliate_payouts"
    __table_args__ = (
        UniqueConstraint("tx_hash", "log_index", name="uq_affiliate_payouts_tx_log"),
        Index("ix_affiliate_payouts_referrer", "referrer_addr"),
        Index("ix_affiliate_payouts_merchant", "merchant_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id"), nullable=False)
    referrer_addr: Mapped[str] = mapped_column(String(42), nullable=False)
    tx_hash: Mapped[str] = mapped_column(String(66), nullable=False)
    log_index: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_micro: Mapped[int] = mapped_column(Integer, nullable=False)
    block_number: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )


class AffiliateAccrual(Base):
    """One rev-share ledger row per referred, settled/delivered order. A pure DB
    number — no code can turn it into a fund movement. UNIQUE(order_id) makes accrual
    idempotent under the deliver()/record_settlement() begin_nested pattern (the
    Entitlement precedent). ``status`` walks accrued -> void (a full M9 refund voids
    it) or accrued -> paid (a verified on-chain payout covers it)."""

    __tablename__ = "affiliate_accruals"
    __table_args__ = (
        UniqueConstraint("order_id", name="uq_affiliate_accruals_order_id"),
        CheckConstraint(
            "status IN ('accrued','void','paid')",
            name="ck_affiliate_accruals_status",
        ),
        Index("ix_affiliate_accruals_referrer", "referrer_addr"),
        Index("ix_affiliate_accruals_merchant_status", "merchant_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), nullable=False)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id"), nullable=False)
    referrer_addr: Mapped[str] = mapped_column(String(42), nullable=False)
    basis_micro: Mapped[int] = mapped_column(Integer, nullable=False)
    rate_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    accrued_micro: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(10), nullable=False, default="accrued", server_default="accrued"
    )
    payout_id: Mapped[int | None] = mapped_column(
        ForeignKey("affiliate_payouts.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )


class EmailSubscriber(Base):
    """One store-level email subscriber. Captured via the public waitlist endpoint
    (source='waitlist') or a buyer checkout (source='checkout'). UNIQUE(store_id,
    email) makes a duplicate a silent no-op (no membership oracle). PII: emails never
    appear in feeds, discovery, logs, or any unauthenticated response — only behind
    the merchant IDOR gate. ``removed_at`` soft-deletes on a removal request while
    keeping the unique slot honest."""

    __tablename__ = "email_subscribers"
    __table_args__ = (
        UniqueConstraint("store_id", "email", name="uq_email_subscribers_store_email"),
        CheckConstraint(
            "source IN ('checkout','waitlist')",
            name="ck_email_subscribers_source",
        ),
        Index("ix_email_subscribers_store_id", "store_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(10), nullable=False, default="waitlist")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )
    removed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AcpSession(Base):
    """One ACP (Agentic Commerce Protocol) checkout session wrapping the proven M3
    order machinery. The high-entropy id is the bearer handle. UNIQUE(store_id,
    idempotency_key) honors the ACP Idempotency-Key replay requirement per store (no
    cross-tenant replay). ``order_id`` is set once a payable order is allocated."""

    __tablename__ = "acp_sessions"
    __table_args__ = (
        UniqueConstraint("order_id", name="uq_acp_sessions_order_id"),
        UniqueConstraint(
            "store_id", "idempotency_key", name="uq_acp_sessions_store_idem"
        ),
        CheckConstraint(
            "status IN "
            "('not_ready_for_payment','ready_for_payment','completed','canceled')",
            name="ck_acp_sessions_status",
        ),
        Index("ix_acp_sessions_store_id", "store_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    buyer: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    items: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    api_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )


class CommissionJob(Base):
    """Roadmap Phase 3 A2A commissioned/custom-build job — the escrow pillar. A buyer
    agent commissions a custom deliverable from a store's provider; the row walks a
    lifecycle (open -> budget_set -> funded -> submitted -> completed, plus cancelled /
    disputed terminals) with an OPTIONAL evaluator that gates release. State is tracked
    here, NOT in any custody: this is the only custodial-adjacent surface in Tilla and
    it is verify-only.

    NON-CUSTODIAL by construction — no code turns a row here into a fund movement. The
    two fund-relevant steps are each a VERIFIED on-chain USDT0 transfer the parties sign
    themselves (the M9 Refund / self_serve pattern): ``funded_tx`` records the buyer's
    deposit to ``escrow_addr`` (a holding wallet the parties designate; Tilla never holds
    its keys) and ``released_tx`` records the release from ``escrow_addr`` to the provider
    (``store.pay_to``). Tilla only verifies each tx landed with the exact ``budget_micro``
    between the pinned wallets and records the hash — it NEVER sends, holds, or auto-
    releases. UNIQUE(funded_tx) / UNIQUE(released_tx) make one on-chain transfer usable by
    a single job ever (the ProcessedTransfer/Refund precedent; SQLite treats the many
    unfunded NULLs as distinct). Every transition is a race-proof conditional UPDATE (the
    M3 ``checkout.transition`` idiom), so no step ever runs twice."""

    __tablename__ = "commission_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('open','budget_set','funded','submitted','completed',"
            "'cancelled','disputed')",
            name="ck_commission_jobs_status",
        ),
        CheckConstraint(
            "budget_micro IS NULL OR budget_micro > 0",
            name="ck_commission_jobs_budget_positive",
        ),
        UniqueConstraint("funded_tx", name="uq_commission_jobs_funded_tx"),
        UniqueConstraint("released_tx", name="uq_commission_jobs_released_tx"),
        Index("ix_commission_jobs_store_id", "store_id"),
        Index("ix_commission_jobs_buyer_addr", "buyer_addr"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    # The commissioning buyer's on-chain wallet (lowercased). Authenticates every
    # buyer-side action and is the pinned ``from`` of the verified deposit tx.
    buyer_addr: Mapped[str] = mapped_column(String(42), nullable=False)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    # Screened at creation (Warden, fail-closed) BEFORE the row is written.
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    brief: Mapped[str] = mapped_column(Text, nullable=False)
    # NULL until set_budget. Exact micro-USDT both verified transfers must match.
    budget_micro: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The holding wallet the buyer deposits into and the provider is released from —
    # designated by the parties, NEVER Tilla (no keys held). Set at set_budget; the
    # pinned ``to`` of the deposit tx and ``from`` of the release tx.
    escrow_addr: Mapped[str | None] = mapped_column(String(42), nullable=True)
    # Optional arbiter. When set, ONLY this wallet may authorize release (gates the
    # submitted -> completed step); NULL leaves release to the buyer.
    evaluator_addr: Mapped[str | None] = mapped_column(String(42), nullable=True)
    status: Mapped[str] = mapped_column(
        String(12), nullable=False, default="open", server_default="open"
    )
    # The verified deposit / release tx hashes (recorded, never sent). NULL until each
    # step's on-chain transfer is verified.
    funded_tx: Mapped[str | None] = mapped_column(String(66), nullable=True)
    # Block of the deposit transfer; the release transfer must not predate it (the M9
    # refund block floor — a release can never precede the deposit it settles).
    funded_block: Mapped[int | None] = mapped_column(Integer, nullable=True)
    released_tx: Mapped[str | None] = mapped_column(String(66), nullable=True)
    # The provider's submitted deliverable reference/note — screened at submit time.
    deliverable: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )
    funded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Plugin(Base):
    """M15.1 provider registry row — the metadata + review-state surface for the
    three formalized extension points (delivery / payment_rail / theme). Built-ins
    are seeded ``source='builtin', status='active'``; any external plugin defaults
    to ``pending_review`` and only an operator act flips it ``active`` (INV-1 keeps
    external installs to the 'theme' kind until the out-of-process runner ships).
    UNIQUE(kind, name) makes the seed idempotent and a re-register a conflict.
    ``artifact_sha256`` is NULL for built-ins (no on-disk artifact) and pins the
    external artifact's hash otherwise."""

    __tablename__ = "plugins"
    __table_args__ = (
        UniqueConstraint("kind", "name", name="uq_plugins_kind_name"),
        CheckConstraint(
            "kind IN ('delivery','payment_rail','theme')", name="ck_plugins_kind"
        ),
        CheckConstraint("source IN ('builtin','external')", name="ck_plugins_source"),
        CheckConstraint(
            "status IN ('pending_review','active','disabled')",
            name="ck_plugins_status",
        ),
        Index("ix_plugins_kind_status", "kind", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    source: Mapped[str] = mapped_column(String(10), nullable=False)
    artifact_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manifest: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="pending_review",
        server_default="pending_review",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )


class GrowthDraft(Base):
    """One queued piece of marketing copy — the M17 growth-agent outbox. Rows are
    fanned from a screened growth kit (M17.1); a scheduled calendar will add
    ``source='scheduled'`` rows later. INVARIANT (INV-1): NOTHING here ever sends —
    ``mark-published`` only RECORDS that a human posted the copy elsewhere. A draft is
    immutable after creation: ``approve`` flips ``status`` only and the
    ``content_sha256`` taken at creation is the tamper anchor, so an approved draft can
    never be silently mutated. A ``status='blocked'`` row (screening BLOCK) has its
    ``body`` withheld from every read path."""

    __tablename__ = "growth_drafts"
    __table_args__ = (
        CheckConstraint(
            "channel IN ('social','email_subject','launch_tweet',"
            "'email_body','product_update')",
            name="ck_growth_drafts_channel",
        ),
        CheckConstraint(
            "source IN ('manual','scheduled')", name="ck_growth_drafts_source"
        ),
        CheckConstraint(
            "status IN ('pending','blocked','approved','published','discarded')",
            name="ck_growth_drafts_status",
        ),
        Index("ix_growth_drafts_store_status", "store_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(
        String(10), nullable=False, default="manual", server_default="manual"
    )
    status: Mapped[str] = mapped_column(
        String(10), nullable=False, default="pending", server_default="pending"
    )
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    screening_status: Mapped[str] = mapped_column(String(10), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    performance_note: Mapped[str | None] = mapped_column(String(500), nullable=True)


class FederatedListing(Base):
    """M16.4 federation ingest — one cached, READ-ONLY row per peer store,
    discovered by fetching an operator-configured peer's /discovery/resources +
    feed.json and schema-validating them against the frozen v1 protocol contract.
    Peer content is DATA: it is never rendered as HTML, only re-emitted json-
    encoded in the ``?include=federated`` discovery echo, and every ``url`` links
    OUT to the PEER's own checkout — Tilla never proxies, quotes, or settles a
    peer's sale (no fund-moving code touches this table). UNIQUE(origin, slug)
    makes a re-ingest an idempotent upsert. Dormant by default (no peers => this
    table stays empty)."""

    __tablename__ = "federated_listings"
    __table_args__ = (
        UniqueConstraint("origin", "slug", name="uq_federated_listings_origin_slug"),
        Index("ix_federated_listings_origin", "origin"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # The peer base URL this row came from (operator-configured origin).
    origin: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Absolute OUTBOUND links into the peer's own surfaces (never a Tilla route).
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    feed_url: Mapped[str] = mapped_column(String(500), nullable=False)
    price_min_micro: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price_max_micro: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(10), nullable=True)
    network: Mapped[str | None] = mapped_column(String(20), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
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


class StoreCreation(Base):
    """A human self-serve store-creation payment intent (the paid dashboard flow).
    The merchant pays the create-store fee (payment.PAYMENT_AMOUNT, currently
    0.05 USDT0) to Tilla's own rail; on a verified on-chain payment
    the store is generated with their wallet as the receive address. The
    description is screened BEFORE a payment is offered, so a merchant is never
    charged for content that would be blocked. UNIQUE(tx_hash) makes one submitted
    payment fund at most one creation (SQLite treats the many pending NULLs as
    distinct)."""

    __tablename__ = "store_creations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','paid','live','failed')",
            name="ck_store_creations_status",
        ),
        UniqueConstraint("tx_hash", name="uq_store_creations_tx_hash"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    merchant_addr: Mapped[str] = mapped_column(String(42), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    theme: Mapped[str | None] = mapped_column(String(40), nullable=True)
    expected_micro: Mapped[int] = mapped_column(Integer, nullable=False)
    pay_to: Mapped[str] = mapped_column(String(42), nullable=False)
    # pending -> paid (payment verified) -> live (store generated); 'paid' with a
    # NULL slug is the retry window when generation failed after a real payment.
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="pending")
    tx_hash: Mapped[str | None] = mapped_column(String(66), nullable=True)
    # Chain head when the intent was opened. A fee transfer mined BEFORE this cannot
    # fund it — the dashboard fee and the x402 /create-store fee are the same amount to
    # the same address, and the x402 path writes no row here, so without a floor a
    # wallet could replay its own earlier fee for a second free store. NULL = no floor
    # recorded (pre-0030 rows, or an RPC blip at intent time) and is not enforced.
    created_block: Mapped[int | None] = mapped_column(Integer, nullable=True)
    slug: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )


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

"""growth: affiliates, email subscribers, ACP sessions

Additive-only on top of 0008 (migrate-before-deploy safe; the running pre-0009 code
never reads the new column or tables). Changes:

``orders`` gains one M13 affiliate-attribution column:
  - ``referrer_addr`` VARCHAR(42) NULL — the referring agent's payout wallet, set at
    order creation. Every existing row backfills to NULL (unreferred), so the accrual
    ledger is only ever written for post-M13 referred sales.

Plus four new tables:
  - ``affiliate_payouts`` — verified on-chain USDT0 payouts (verify-and-record only,
    the M9 Refund pattern; UNIQUE(tx_hash, log_index) idempotency).
  - ``affiliate_accruals`` — the rev-share ledger (a pure number; UNIQUE(order_id)).
  - ``email_subscribers`` — store-level capture (UNIQUE(store_id, email)).
  - ``acp_sessions`` — ACP /checkout_sessions state (UNIQUE(store_id, idem key)).

CRITICAL: ``referrer_addr`` is added with a PLAIN ``op.add_column`` (SQLite-native
ALTER TABLE ADD COLUMN), NOT ``batch_alter_table`` — a batch rebuild of ``orders``
would have to reconstruct the M3 partial unique index ``ux_orders_active_amount``
(exactly what 0007/0008 avoided). Downgrade drops the four tables and batch-drops the
column (a rebuild is acceptable on downgrade only; the migration test re-asserts the
partial index after up AND up-down-up).

Revision ID: 0009_growth
Revises: 0008_onchain_receipts
Create Date: 2026-07-21
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009_growth"
down_revision: Union[str, None] = "0008_onchain_receipts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Native ADD COLUMN — no batch rebuild, so the M3 partial unique index
    # ux_orders_active_amount on orders is provably untouched.
    op.add_column(
        "orders", sa.Column("referrer_addr", sa.String(length=42), nullable=True)
    )
    op.create_index("ix_orders_referrer_addr", "orders", ["referrer_addr"])

    # affiliate_payouts first: affiliate_accruals.payout_id references it.
    op.create_table(
        "affiliate_payouts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "merchant_id", sa.Integer(), sa.ForeignKey("merchants.id"), nullable=False
        ),
        sa.Column("referrer_addr", sa.String(length=42), nullable=False),
        sa.Column("tx_hash", sa.String(length=66), nullable=False),
        sa.Column("log_index", sa.Integer(), nullable=False),
        sa.Column("amount_micro", sa.Integer(), nullable=False),
        sa.Column("block_number", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("tx_hash", "log_index", name="uq_affiliate_payouts_tx_log"),
    )
    op.create_index(
        "ix_affiliate_payouts_referrer", "affiliate_payouts", ["referrer_addr"]
    )
    op.create_index(
        "ix_affiliate_payouts_merchant", "affiliate_payouts", ["merchant_id"]
    )

    op.create_table(
        "affiliate_accruals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "order_id", sa.String(length=32), sa.ForeignKey("orders.id"), nullable=False
        ),
        sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id"), nullable=False),
        sa.Column(
            "merchant_id", sa.Integer(), sa.ForeignKey("merchants.id"), nullable=False
        ),
        sa.Column("referrer_addr", sa.String(length=42), nullable=False),
        sa.Column("basis_micro", sa.Integer(), nullable=False),
        sa.Column("rate_bps", sa.Integer(), nullable=False),
        sa.Column("accrued_micro", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=10),
            nullable=False,
            server_default="accrued",
        ),
        sa.Column(
            "payout_id",
            sa.Integer(),
            sa.ForeignKey("affiliate_payouts.id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("order_id", name="uq_affiliate_accruals_order_id"),
        sa.CheckConstraint(
            "status IN ('accrued','void','paid')",
            name="ck_affiliate_accruals_status",
        ),
    )
    op.create_index(
        "ix_affiliate_accruals_referrer", "affiliate_accruals", ["referrer_addr"]
    )
    op.create_index(
        "ix_affiliate_accruals_merchant_status",
        "affiliate_accruals",
        ["merchant_id", "status"],
    )

    op.create_table(
        "email_subscribers",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id"), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("source", sa.String(length=10), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("removed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "store_id", "email", name="uq_email_subscribers_store_email"
        ),
        sa.CheckConstraint(
            "source IN ('checkout','waitlist')",
            name="ck_email_subscribers_source",
        ),
    )
    op.create_index("ix_email_subscribers_store_id", "email_subscribers", ["store_id"])

    op.create_table(
        "acp_sessions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id"), nullable=False),
        sa.Column(
            "order_id", sa.String(length=32), sa.ForeignKey("orders.id"), nullable=True
        ),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=True),
        sa.Column("buyer", sa.JSON(), nullable=True),
        sa.Column("items", sa.JSON(), nullable=True),
        sa.Column("api_version", sa.String(length=20), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("order_id", name="uq_acp_sessions_order_id"),
        sa.UniqueConstraint(
            "store_id", "idempotency_key", name="uq_acp_sessions_store_idem"
        ),
        sa.CheckConstraint(
            "status IN "
            "('not_ready_for_payment','ready_for_payment','completed','canceled')",
            name="ck_acp_sessions_status",
        ),
    )
    op.create_index("ix_acp_sessions_store_id", "acp_sessions", ["store_id"])


def downgrade() -> None:
    op.drop_index("ix_acp_sessions_store_id", table_name="acp_sessions")
    op.drop_table("acp_sessions")
    op.drop_index("ix_email_subscribers_store_id", table_name="email_subscribers")
    op.drop_table("email_subscribers")
    op.drop_index(
        "ix_affiliate_accruals_merchant_status", table_name="affiliate_accruals"
    )
    op.drop_index("ix_affiliate_accruals_referrer", table_name="affiliate_accruals")
    op.drop_table("affiliate_accruals")
    op.drop_index("ix_affiliate_payouts_merchant", table_name="affiliate_payouts")
    op.drop_index("ix_affiliate_payouts_referrer", table_name="affiliate_payouts")
    op.drop_table("affiliate_payouts")
    op.drop_index("ix_orders_referrer_addr", table_name="orders")
    # Downgrade-only rebuild risk is acceptable (the migration test re-asserts the
    # partial index after up-down-up).
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.drop_column("referrer_addr")

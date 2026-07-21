"""merchant platform

Additive-only on top of 0005 (migrate-before-deploy safe; the running pre-0006
code never reads the new columns/tables). Changes:

1. ``merchants`` gains ``webhook_url`` VARCHAR(500) NULL and ``webhook_secret``
   VARCHAR(64) NULL (the M9 outbound webhook config). ``api_key_hash`` already
   exists from an earlier revision and is reused unchanged.
2. ``orders`` gains ``refunded_micro`` INTEGER NOT NULL server_default '0'. Added
   with a PLAIN ``op.add_column`` (NOT batch_alter_table): SQLite runs a native
   ALTER TABLE ADD COLUMN with no table rebuild, so the M3 partial unique index
   ``ux_orders_active_amount`` is provably untouched. The migration test re-asserts
   the index survives.
3. New ``refunds`` table — one row per verified on-chain refund transfer,
   UNIQUE(tx_hash, log_index) so one transfer credits exactly one order.
4. New ``webhook_deliveries`` outbox table — enqueued in the same transaction as
   the reported state change, dispatched by the background webhook loop.

Revision ID: 0006_merchant_platform
Revises: 0005_payment_methods
Create Date: 2026-07-21
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_merchant_platform"
down_revision: Union[str, None] = "0005_payment_methods"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Native ADD COLUMN on both existing tables — no batch rebuild, so no index
    # (merchants.wallet_address unique, orders' partial unique amount index) moves.
    op.add_column(
        "merchants", sa.Column("webhook_url", sa.String(length=500), nullable=True)
    )
    op.add_column(
        "merchants", sa.Column("webhook_secret", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "orders",
        sa.Column("refunded_micro", sa.Integer(), nullable=False, server_default="0"),
    )

    op.create_table(
        "refunds",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("order_id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("tx_hash", sa.String(length=66), nullable=False),
        sa.Column("log_index", sa.Integer(), nullable=False),
        sa.Column("amount_micro", sa.Integer(), nullable=False),
        sa.Column("block_number", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.CheckConstraint("kind IN ('full','overage')", name="ck_refunds_kind"),
        sa.UniqueConstraint("tx_hash", "log_index", name="uq_refunds_tx_log"),
    )
    op.create_index("ix_refunds_order_id", "refunds", ["order_id"])

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("merchant_id", sa.Integer(), nullable=False),
        sa.Column("event", sa.String(length=30), nullable=False),
        sa.Column("order_id", sa.String(length=32), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column(
            "status", sa.String(length=10), nullable=False, server_default="pending"
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=False),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["merchant_id"], ["merchants.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.CheckConstraint(
            "status IN ('pending','delivered','dead')",
            name="ck_webhook_deliveries_status",
        ),
    )
    op.create_index(
        "ix_webhook_deliveries_due",
        "webhook_deliveries",
        ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_deliveries_due", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
    op.drop_index("ix_refunds_order_id", table_name="refunds")
    op.drop_table("refunds")
    op.drop_column("orders", "refunded_micro")
    op.drop_column("merchants", "webhook_secret")
    op.drop_column("merchants", "webhook_url")

"""payment methods

Additive-only on top of 0004 (migrate-before-deploy safe; the running pre-0005
code never reads the new columns/table). Two changes:

1. ``products`` gains ``pricing_model`` VARCHAR(12) NOT NULL server_default
   'one_time' (+ CHECK IN one_time/batch/metered/subscription) and
   ``pricing_params`` JSON NULL. Every existing product backfills to 'one_time',
   which is byte-identical exact checkout — no behaviour change.
2. New ``mpp_channels`` table for MPP pay-as-you-go session state (dormant until
   TILLA_MPP_ENABLED + SA creds; the endpoints 503 fail-closed meanwhile).

``products`` carries no partial indexes, so the SQLite batch rebuild does not
touch the M3 ``ux_orders_active_amount`` partial unique index on ``orders`` (a
different table). The migration test still re-asserts that invariant survives.

Revision ID: 0005_payment_methods
Revises: 0004_agent_commerce
Create Date: 2026-07-21
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005_payment_methods"
down_revision: Union[str, None] = "0004_agent_commerce"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("products", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "pricing_model",
                sa.String(length=12),
                nullable=False,
                server_default="one_time",
            )
        )
        batch_op.add_column(sa.Column("pricing_params", sa.JSON(), nullable=True))
        batch_op.create_check_constraint(
            "ck_products_pricing_model",
            "pricing_model IN ('one_time','batch','metered','subscription')",
        )

    op.create_table(
        "mpp_channels",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("store_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.String(length=80), nullable=False),
        sa.Column("buyer_addr", sa.String(length=42), nullable=True),
        sa.Column("pay_to", sa.String(length=42), nullable=False),
        sa.Column("deposit_micro", sa.Integer(), nullable=False),
        sa.Column("spent_micro", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unit_price_micro", sa.Integer(), nullable=False),
        sa.Column(
            "status", sa.String(length=10), nullable=False, server_default="open"
        ),
        sa.Column("last_voucher_json", sa.JSON(), nullable=True),
        sa.Column("opened_at", sa.DateTime(), nullable=False),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"]),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.CheckConstraint(
            "status IN ('open','closing','closed','failed')",
            name="ck_mpp_channels_status",
        ),
        sa.UniqueConstraint("channel_id", name="uq_mpp_channels_channel_id"),
    )
    op.create_index("ix_mpp_channels_store_id", "mpp_channels", ["store_id"])


def downgrade() -> None:
    op.drop_index("ix_mpp_channels_store_id", table_name="mpp_channels")
    op.drop_table("mpp_channels")
    with op.batch_alter_table("products", schema=None) as batch_op:
        batch_op.drop_constraint("ck_products_pricing_model", type_="check")
        batch_op.drop_column("pricing_params")
        batch_op.drop_column("pricing_model")

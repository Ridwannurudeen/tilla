"""marketplace citizenship

Additive-only on top of 0006 (migrate-before-deploy safe; the running pre-0007
code never reads the new columns/table). Changes:

1. ``stores`` gains ``marketplace_status`` VARCHAR(12) NOT NULL server_default
   'unlisted' (+ CHECK IN unlisted/prepared/submitted/listed/rejected) and
   ``marketplace_listed_at`` DATETIME NULL. Every existing store backfills to
   'unlisted' (no behaviour change). Added via ``batch_alter_table`` because the
   CHECK constraint needs a SQLite table rebuild; ``stores`` carries no partial
   indexes, so the M3 ``ux_orders_active_amount`` partial unique index on ``orders``
   (a different table) is provably untouched. The migration test re-asserts it.
2. New ``screening_receipts`` table — the typed, queryable money-record surface for
   content screening (the Refund-table precedent). ``mode='paid'`` rows carry the
   x402 hire amount + on-chain settle tx_hash; ``mode='demo'`` rows carry neither.

Revision ID: 0007_marketplace_citizenship
Revises: 0006_merchant_platform
Create Date: 2026-07-21
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007_marketplace_citizenship"
down_revision: Union[str, None] = "0006_merchant_platform"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("stores", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "marketplace_status",
                sa.String(length=12),
                nullable=False,
                server_default="unlisted",
            )
        )
        batch_op.add_column(
            sa.Column("marketplace_listed_at", sa.DateTime(), nullable=True)
        )
        batch_op.create_check_constraint(
            "ck_stores_marketplace_status",
            "marketplace_status IN "
            "('unlisted','prepared','submitted','listed','rejected')",
        )

    op.create_table(
        "screening_receipts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("store_id", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(length=10), nullable=False),
        sa.Column("verdict", sa.String(length=10), nullable=False),
        sa.Column("risk_level", sa.String(length=20), nullable=True),
        sa.Column("endpoint", sa.String(length=200), nullable=False),
        sa.Column("amount_micro", sa.Integer(), nullable=True),
        sa.Column("tx_hash", sa.String(length=66), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"]),
        sa.CheckConstraint(
            "mode IN ('demo','paid')", name="ck_screening_receipts_mode"
        ),
    )
    op.create_index(
        "ix_screening_receipts_store_id", "screening_receipts", ["store_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_screening_receipts_store_id", table_name="screening_receipts")
    op.drop_table("screening_receipts")
    with op.batch_alter_table("stores", schema=None) as batch_op:
        batch_op.drop_constraint("ck_stores_marketplace_status", type_="check")
        batch_op.drop_column("marketplace_listed_at")
        batch_op.drop_column("marketplace_status")

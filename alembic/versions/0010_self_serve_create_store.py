"""self-serve create-store: store_creations table

Additive-only on top of 0009 (migrate-before-deploy safe; pre-0010 code never reads
the new table). One new table backs the paid human create-store flow: a signed-in
merchant pays 1 USDT to Tilla's rail, the payment is verified on-chain, and the store
is generated with their wallet as the receive address.

  - ``store_creations`` — one row per create-store payment intent.
    UNIQUE(tx_hash) makes a submitted payment fund at most one creation (SQLite treats
    the many pending NULLs as distinct). status pending -> paid -> live, with 'paid' +
    NULL slug the post-payment generation-retry window.

No existing table is touched, so the M3 partial unique index ux_orders_active_amount
is provably untouched (create_table only). Downgrade drops the table.

Revision ID: 0010_self_serve_create_store
Revises: 0009_growth
Create Date: 2026-07-22
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010_self_serve_create_store"
down_revision: Union[str, None] = "0009_growth"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "store_creations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("merchant_addr", sa.String(length=42), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("theme", sa.String(length=40), nullable=True),
        sa.Column("expected_micro", sa.Integer(), nullable=False),
        sa.Column("pay_to", sa.String(length=42), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("tx_hash", sa.String(length=66), nullable=True),
        sa.Column("slug", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("tx_hash", name="uq_store_creations_tx_hash"),
        sa.CheckConstraint(
            "status IN ('pending','paid','live','failed')",
            name="ck_store_creations_status",
        ),
    )
    op.create_index("ix_store_creations_merchant", "store_creations", ["merchant_addr"])


def downgrade() -> None:
    op.drop_index("ix_store_creations_merchant", table_name="store_creations")
    op.drop_table("store_creations")

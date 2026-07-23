"""Phase 3 verified-buyer reviews: the reviews table

Additive-only: one new table ``reviews`` (verified-buyer reputation rows), no change
to any existing table, so it is migrate-before-deploy safe (pre-0025 code never reads
it). The ``orders`` table is untouched, so the M3 ``ux_orders_active_amount`` partial
unique index is trivially safe. No fund-moving code touches this table — a row is pure
reputation. UNIQUE(order_id) makes it one review per completed order; CHECK bounds the
rating to 1..5.

Revision ID: 0025_reviews
Revises: 0024_store_visibility
Create Date: 2026-07-22
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0025_reviews"
down_revision: Union[str, None] = "0024_store_visibility"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "reviews",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id"), nullable=False),
        sa.Column(
            "product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=True
        ),
        sa.Column(
            "order_id", sa.String(length=32), sa.ForeignKey("orders.id"), nullable=False
        ),
        sa.Column("from_addr", sa.String(length=42), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("order_id", name="uq_reviews_order_id"),
        sa.CheckConstraint("rating >= 1 AND rating <= 5", name="ck_reviews_rating"),
    )
    op.create_index("ix_reviews_store_id", "reviews", ["store_id"])


def downgrade() -> None:
    op.drop_index("ix_reviews_store_id", table_name="reviews")
    op.drop_table("reviews")

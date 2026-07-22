"""Phase 1.7 sandbox/hidden mode: stores.visibility

Additive-only on top of 0012 (migrate-before-deploy safe; pre-1.7 code never reads the
new column). ``stores`` gains ``visibility`` VARCHAR(10) NOT NULL server_default 'public'
— 'public' shows in discovery / aggregate feed / sitemap; 'hidden' keeps a live store
out of those bulk surfaces while it stays reachable by direct link.

``stores`` carries no partial indexes (the M3 ``ux_orders_active_amount`` is on the
``orders`` table), so a native ADD COLUMN is trivially safe; the server_default backfills
every existing store to 'public' (unchanged discovery behaviour). Downgrade drops the
column via ``batch_alter_table``.

Revision ID: 0013_store_visibility
Revises: 0012_order_eval_status
Create Date: 2026-07-22
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013_store_visibility"
down_revision: Union[str, None] = "0012_order_eval_status"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "stores",
        sa.Column(
            "visibility",
            sa.String(length=10),
            nullable=False,
            server_default="public",
        ),
    )


def downgrade() -> None:
    with op.batch_alter_table("stores", schema=None) as batch_op:
        batch_op.drop_column("visibility")

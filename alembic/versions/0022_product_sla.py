"""Phase 1.3 SLA: products.sla_minutes

Additive-only on top of 0010 (migrate-before-deploy safe; pre-1.3 code never reads
the new column). ``products`` gains ``sla_minutes`` INTEGER NULL — the merchant's
delivery-time promise, surfaced to buyer agents as an ETA in feed.json / MCP. NULL
means "no per-product override" so the agent surfaces fall back to the platform
default; every existing product backfills to NULL, an unchanged instant-delivery
promise.

Upgrade uses a PLAIN ``op.add_column`` (SQLite-native ALTER TABLE ADD COLUMN). The
``orders`` table — the only one carrying the M3 partial unique index
``ux_orders_active_amount`` — is not touched at all, so that index is provably safe.
Downgrade drops the column via ``batch_alter_table`` (a products rebuild, harmless:
``products`` carries no partial indexes).

Revision ID: 0022_product_sla
Revises: 0021_self_serve_create_store
Create Date: 2026-07-22
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0022_product_sla"
down_revision: Union[str, None] = "0021_self_serve_create_store"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("products", sa.Column("sla_minutes", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("products", schema=None) as batch_op:
        batch_op.drop_column("sla_minutes")

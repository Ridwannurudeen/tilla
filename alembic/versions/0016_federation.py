"""federation ingest: cached peer listings

Additive-only: one new table ``federated_listings`` (the M16.4 mirror-of-mirrors
ingest cache), no change to any existing table, so it is migrate-before-deploy
safe (pre-0016 code never reads it). No fund-moving code touches this table — the
rows are read-only peer discovery data that link OUT to the peer's own checkout.

RENUMBER-AT-INTEGRATION: authored as 0016 with ``down_revision='0014_crosschain'``
(the current head) because 0015 may be claimed by another module landing in the
same integration window. If this lands after a 0015 head, renumber to the next
free head and repoint ``down_revision`` — this file depends on no table beyond
what already exists (it is fully standalone).

Revision ID: 0016_federation
Revises: 0014_crosschain
Create Date: 2026-07-21
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0016_federation"
down_revision: Union[str, None] = "0014_crosschain"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "federated_listings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("origin", sa.String(length=200), nullable=False),
        sa.Column("slug", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("url", sa.String(length=500), nullable=False),
        sa.Column("feed_url", sa.String(length=500), nullable=False),
        sa.Column("price_min_micro", sa.Integer(), nullable=True),
        sa.Column("price_max_micro", sa.Integer(), nullable=True),
        sa.Column("currency", sa.String(length=10), nullable=True),
        sa.Column("network", sa.String(length=20), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("origin", "slug", name="uq_federated_listings_origin_slug"),
    )
    op.create_index("ix_federated_listings_origin", "federated_listings", ["origin"])


def downgrade() -> None:
    op.drop_index("ix_federated_listings_origin", table_name="federated_listings")
    op.drop_table("federated_listings")

"""Roadmap Phase 3 storefront depth: deliverable version marker

Additive-only: one new column ``deliverables.version`` (native ``add_column`` with a
server_default of ``1``), no table rebuild, so it is migrate-before-deploy safe (pre-0028
code never reads it and every existing deliverable backfills to version 1). The
``orders`` table is untouched, so the M3 ``ux_orders_active_amount`` partial unique index
is trivially safe.

This column powers **versioned releases**: publishing a new version of a deliverable
inserts a higher-versioned active row in the same (store, product, kind) scope; a past
buyer's entitlement rolls FORWARD to the store's current active version at read time
(``main._current_version``), so they re-download the newest version from their wallet
library. A plain replace keeps version 1 and never rolls a past buyer forward.

Membership tiers and pay-what-you-want ride the existing ``products.pricing_params`` JSON
column (no schema change), so they need no migration.

NON-CUSTODIAL: no fund-moving code touches this column — it is a display/resolution marker
only.

Revision ID: 0028_storefront_depth
Revises: 0027_commission_jobs
Create Date: 2026-07-22

NOTE (renumber-at-integration): if a concurrent branch lands a migration numbered 0028
first, renumber this file's revision id + filename to the next free slot and keep
``down_revision`` pointing at whatever becomes the new head; the upgrade/downgrade bodies
are order-independent (a single additive column on ``deliverables``).
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0028_storefront_depth"
down_revision: Union[str, None] = "0027_commission_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "deliverables",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("deliverables", "version")

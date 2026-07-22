"""Phase 4 custom domains: stores.custom_domain (+ token + verified_at)

Additive-only: ``stores`` gains three nullable columns — ``custom_domain`` (the
validated, lowercased hostname a merchant claims), ``custom_domain_token`` (the DNS
TXT challenge secret) and ``custom_domain_verified_at`` (set only once ownership is
proven) — plus a UNIQUE index on ``custom_domain`` so one hostname maps to at most one
store (a verified domain can never be hijacked to a second store). Migrate-before-deploy
safe: pre-0029 code never reads the new columns.

``stores`` carries no partial indexes (the M3 ``ux_orders_active_amount`` is on the
``orders`` table, which is untouched here), so native ADD COLUMNs are trivially safe;
every existing store backfills to NULL (unclaimed — host resolution serves nothing).
SQLite treats the many NULL ``custom_domain`` values as distinct, so the UNIQUE index
only ever fires on a real duplicate claim. Downgrade drops the index then the columns.

Renumbered to 0029 at integration (down_revision pinned to 0027_commission_jobs, the
migration head on the integration trunk this branch merged from).

Revision ID: 0029_custom_domain
Revises: 0027_commission_jobs
Create Date: 2026-07-22
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0029_custom_domain"
down_revision: Union[str, None] = "0027_commission_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "stores", sa.Column("custom_domain", sa.String(length=253), nullable=True)
    )
    op.add_column(
        "stores", sa.Column("custom_domain_token", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "stores", sa.Column("custom_domain_verified_at", sa.DateTime(), nullable=True)
    )
    op.create_index("uq_stores_custom_domain", "stores", ["custom_domain"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_stores_custom_domain", table_name="stores")
    with op.batch_alter_table("stores", schema=None) as batch_op:
        batch_op.drop_column("custom_domain_verified_at")
        batch_op.drop_column("custom_domain_token")
        batch_op.drop_column("custom_domain")

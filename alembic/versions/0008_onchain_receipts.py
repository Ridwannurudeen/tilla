"""onchain receipts

Additive-only on top of 0007 (migrate-before-deploy safe; the running pre-0008 code
never reads the new columns). Changes:

``orders`` gains three M11 EAS-attestation columns:
  - ``attestation_uid`` VARCHAR(66) NULL — the EAS attestation UID once attested.
  - ``attest_tx`` VARCHAR(66) NULL — the on-chain attestation transaction hash.
  - ``attest_status`` VARCHAR(12) NOT NULL server_default 'none' — app-level state
    (none/pending/sent/attested/failed). Every existing row backfills to 'none'
    (never queued), so enabling attestation later attests only post-enable sales.

CRITICAL: added with a PLAIN ``op.add_column`` (SQLite-native ALTER TABLE ADD COLUMN),
NOT ``batch_alter_table`` — a batch rebuild of ``orders`` would have to reconstruct the
M3 partial unique index ``ux_orders_active_amount`` (exactly what 0007 avoided by only
touching ``stores``). No CHECK constraint on ``attest_status`` (a CHECK would force the
rebuild); the single-writer attester worker + tests enforce the enum instead. Plus one
plain index ``ix_orders_attest_status`` for the worker's pending-order query. The
migration test re-asserts ``ux_orders_active_amount`` survives after up AND up-down-up.

Revision ID: 0008_onchain_receipts
Revises: 0007_marketplace_citizenship
Create Date: 2026-07-21
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008_onchain_receipts"
down_revision: Union[str, None] = "0007_marketplace_citizenship"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Native ADD COLUMN — no batch rebuild, so the M3 partial unique index
    # ux_orders_active_amount on orders is provably untouched.
    op.add_column(
        "orders", sa.Column("attestation_uid", sa.String(length=66), nullable=True)
    )
    op.add_column("orders", sa.Column("attest_tx", sa.String(length=66), nullable=True))
    op.add_column(
        "orders",
        sa.Column(
            "attest_status",
            sa.String(length=12),
            nullable=False,
            server_default="none",
        ),
    )
    op.create_index("ix_orders_attest_status", "orders", ["attest_status"])


def downgrade() -> None:
    op.drop_index("ix_orders_attest_status", table_name="orders")
    # Downgrade-only rebuild risk is acceptable (the migration test re-asserts the
    # partial index after up-down-up).
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.drop_column("attest_status")
        batch_op.drop_column("attest_tx")
        batch_op.drop_column("attestation_uid")

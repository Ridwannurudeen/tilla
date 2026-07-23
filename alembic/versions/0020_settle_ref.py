"""settle_ref: capture the pending aggregated settle reference for aggr_deferred

Additive-only on top of 0018 (migrate-before-deploy safe; the running pre-0019 code
never reads the new column). ``orders`` gains one M8 column:

  - ``settle_ref`` VARCHAR(66) NULL — the pending aggregated settle reference the
    facilitator returns for a deferred (async) settle whose aggregated tx has NOT yet
    confirmed. The reconciliation poller (``app.reconcile``) reads it to query
    ``get_settle_status(settle_ref)`` and finalize the order only once the tx
    confirms. Every existing row backfills to NULL (untouched by the poller).

CRITICAL: added with a PLAIN ``op.add_column`` (SQLite-native ALTER TABLE ADD COLUMN),
NOT ``batch_alter_table`` — a batch rebuild of ``orders`` would have to reconstruct the
M3 partial unique index ``ux_orders_active_amount``. The downgrade rebuild risk is
acceptable (mirrors 0008).

RENUMBER-AT-INTEGRATION: authored as 0019 on top of ``0018_perchain_cursor`` (this
worktree's head). At integration, renumber this file + ``revision`` to the next free
head and repoint ``down_revision`` to the true predecessor.

Revision ID: 0020_settle_ref
Revises: 0018_perchain_cursor
Create Date: 2026-07-21
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0020_settle_ref"
down_revision: Union[str, None] = "0019_attest_nonce"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Native ADD COLUMN — no batch rebuild, so the M3 partial unique index
    # ux_orders_active_amount on orders is provably untouched.
    op.add_column(
        "orders", sa.Column("settle_ref", sa.String(length=66), nullable=True)
    )


def downgrade() -> None:
    # Downgrade-only rebuild risk is acceptable (mirrors 0008_onchain_receipts).
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.drop_column("settle_ref")

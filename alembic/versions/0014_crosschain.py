"""crosschain: orders.network (per-order settlement-chain pin)

Additive-only on top of 0009 (migrate-before-deploy safe; the running pre-M18 code
never reads the new column). Adds one column:

``orders.network`` VARCHAR(20) NOT NULL DEFAULT 'eip155:196' — the CAIP-2 settlement
chain an order is pinned to at creation. Every existing row backfills to the
canonical X Layer ledger (eip155:196, INV-2) via the server_default, so pre-M18
orders are unambiguously the one proven chain.

CRITICAL: added with a PLAIN ``op.add_column`` (SQLite-native ALTER TABLE ADD
COLUMN), NOT ``batch_alter_table`` — a batch rebuild of ``orders`` would have to
reconstruct the M3 partial unique index ``ux_orders_active_amount`` (exactly what
0007/0008/0009 avoided). Downgrade batch-drops the column (a rebuild is acceptable
on downgrade only; the migration test re-asserts the partial index after up AND
up-down-up).

RENUMBER-AT-INTEGRATION: authored as 0014 because M15–M17 claim 0010–0013 in their
own worktrees; this worktree's head is 0009_growth, so ``down_revision`` pins there.
At integration, renumber this file + ``revision`` to the next free head and repoint
``down_revision`` to whatever the true predecessor turns out to be (0009_growth if
the M15–M17 migrations have not yet landed, otherwise 0013).

Revision ID: 0014_crosschain
Revises: 0009_growth
Create Date: 2026-07-21
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014_crosschain"
down_revision: Union[str, None] = "0009_growth"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Native ADD COLUMN — no batch rebuild, so the M3 partial unique index
    # ux_orders_active_amount on orders is provably untouched. The server_default
    # backfills every existing order to the canonical chain.
    op.add_column(
        "orders",
        sa.Column(
            "network",
            sa.String(length=20),
            nullable=False,
            server_default="eip155:196",
        ),
    )


def downgrade() -> None:
    # Downgrade-only rebuild risk is acceptable (the migration test re-asserts the
    # partial index after up-down-up).
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.drop_column("network")

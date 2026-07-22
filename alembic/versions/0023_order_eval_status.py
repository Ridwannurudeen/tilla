"""Phase 1.6 evaluation window: orders.eval_status

Additive-only on top of 0011 (migrate-before-deploy safe; pre-1.6 code never reads
the new column). ``orders`` gains ``eval_status`` VARCHAR(12) NOT NULL server_default
'none' — a delivered order's buyer-evaluation state (none | pending | confirmed |
rejected), the basis for a dispute-aware success_rate.

CRITICAL: ``eval_status`` is added with a PLAIN ``op.add_column`` (SQLite-native ALTER
TABLE ADD COLUMN), NOT ``batch_alter_table`` — a batch rebuild of ``orders`` would
reconstruct (and could drop) the M3 partial unique index ``ux_orders_active_amount``.
Native ADD COLUMN leaves every existing index untouched. The server_default backfills
every existing row to 'none' (untracked = counted as good, unchanged reputation).

Downgrade drops the column via ``batch_alter_table`` (an orders rebuild); the test
re-asserts ``ux_orders_active_amount`` survives the down/up round-trip.

Revision ID: 0023_order_eval_status
Revises: 0022_product_sla
Create Date: 2026-07-22
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0023_order_eval_status"
down_revision: Union[str, None] = "0022_product_sla"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column(
            "eval_status",
            sa.String(length=12),
            nullable=False,
            server_default="none",
        ),
    )


def downgrade() -> None:
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.drop_column("eval_status")

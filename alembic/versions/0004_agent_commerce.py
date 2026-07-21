"""agent commerce

Additive-only on top of 0003. Two columns on ``orders`` for x402 agent buys:
``channel`` (VARCHAR(10) NOT NULL server_default 'web' — marks the reaper scope,
analytics, support triage) and ``x402_nonce`` (VARCHAR(66) nullable + a UNIQUE
index, the EIP-3009 authorization nonce that is the idempotency/replay key).
SQLite allows unlimited NULLs in a unique index, so every existing human order
(NULL nonce) is unaffected. Both columns are defaulted/nullable, so the RUNNING
pre-0004 code is untouched by the new schema — migrate-before-deploy is safe and
downgrade is a clean reversal.

Revision ID: 0004_agent_commerce
Revises: 0003_gated_delivery
Create Date: 2026-07-21
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_agent_commerce"
down_revision: Union[str, None] = "0003_gated_delivery"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "channel",
                sa.String(length=10),
                nullable=False,
                server_default="web",
            )
        )
        batch_op.add_column(
            sa.Column("x402_nonce", sa.String(length=66), nullable=True)
        )
        batch_op.create_index("ux_orders_x402_nonce", ["x402_nonce"], unique=True)


def downgrade() -> None:
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.drop_index("ux_orders_x402_nonce")
        batch_op.drop_column("x402_nonce")
        batch_op.drop_column("channel")
    # batch_alter_table drop_column rebuilds ``orders`` from reflection on SQLite,
    # and reflection does NOT carry SQLite partial indexes — so the M3 partial
    # unique index ux_orders_active_amount (the unique-amount / double-spend
    # money invariant) is silently dropped by the rebuild above. Re-create it with
    # its original partial WHERE clause (idempotent IF NOT EXISTS) so a rollback
    # never loses the invariant. Mirrors 0002_hardened_checkout._ACTIVE_STATES.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_active_amount "
        "ON orders (pay_to, expected_micro) "
        "WHERE status IN ('pending','detected','underpaid','expired','late_paid')"
    )

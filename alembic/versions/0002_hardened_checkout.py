"""hardened checkout

Additive-only on top of 0001. Adds the M3 order columns (expected/paid/overpaid
micro, from_addr, block_number), backfills expected_micro from amount_micro so
legacy orders keep exact-price matching, relaxes baseline_micro to nullable, and
adds the partial unique index that reserves an amount while an order is active.
Two new tables: processed_transfers (one-tx-one-order idempotency) and
chain_cursor (single-row sweep cursor). Downgrade is a clean reversal.

Revision ID: 0002_hardened_checkout
Revises: 0001_persistence_core
Create Date: 2026-07-21
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_hardened_checkout"
down_revision: Union[str, None] = "0001_persistence_core"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ACTIVE_STATES = "status IN ('pending','detected','underpaid','expired','late_paid')"


def upgrade() -> None:
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "expected_micro",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(
            sa.Column("paid_micro", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column(
                "overpaid_micro", sa.Integer(), nullable=False, server_default="0"
            )
        )
        batch_op.add_column(sa.Column("from_addr", sa.String(length=42), nullable=True))
        batch_op.add_column(sa.Column("block_number", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("created_block", sa.Integer(), nullable=True))
        batch_op.alter_column(
            "baseline_micro", existing_type=sa.Integer(), nullable=True
        )

    # Legacy orders keep exact-price matching: expected == price (offset 0).
    op.execute("UPDATE orders SET expected_micro = amount_micro")

    # M2 had no unique-amount offset and no expiry, so a real DB can hold several
    # stale pending orders sharing one (pay_to, amount_micro). After the backfill
    # above they collide on the partial unique index below and abort the upgrade.
    # Park every duplicate pending (keep one per group) in a terminal status that
    # sits OUTSIDE the index predicate so the index builds cleanly.
    op.execute(
        "UPDATE orders SET status = 'canceled' "
        "WHERE status = 'pending' AND id NOT IN ("
        "  SELECT MIN(id) FROM orders WHERE status = 'pending' "
        "  GROUP BY pay_to, expected_micro"
        ")"
    )

    op.create_index(
        "ux_orders_active_amount",
        "orders",
        ["pay_to", "expected_micro"],
        unique=True,
        sqlite_where=sa.text(_ACTIVE_STATES),
    )

    op.create_table(
        "processed_transfers",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tx_hash", sa.String(length=66), nullable=False),
        sa.Column("log_index", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.String(length=32), nullable=False),
        sa.Column("pay_to", sa.String(length=42), nullable=False),
        sa.Column("from_addr", sa.String(length=42), nullable=False),
        sa.Column("amount_micro", sa.Integer(), nullable=False),
        sa.Column("block_number", sa.Integer(), nullable=False),
        sa.Column("seen_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tx_hash", "log_index", name="uq_processed_tx_log"),
    )
    op.create_index(
        "ix_processed_transfers_order_id",
        "processed_transfers",
        ["order_id"],
        unique=False,
    )

    op.create_table(
        "chain_cursor",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("last_block", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_chain_cursor_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("chain_cursor")
    op.drop_index("ix_processed_transfers_order_id", table_name="processed_transfers")
    op.drop_table("processed_transfers")
    op.drop_index("ux_orders_active_amount", table_name="orders")
    # M3 code leaves baseline_micro NULL (it is legacy balance-delta state). Backfill
    # before restoring NOT NULL, or the batch table-rebuild fails on any M3 row.
    op.execute("UPDATE orders SET baseline_micro = 0 WHERE baseline_micro IS NULL")
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.alter_column(
            "baseline_micro", existing_type=sa.Integer(), nullable=False
        )
        batch_op.drop_column("created_block")
        batch_op.drop_column("block_number")
        batch_op.drop_column("from_addr")
        batch_op.drop_column("overpaid_micro")
        batch_op.drop_column("paid_micro")
        batch_op.drop_column("expected_micro")

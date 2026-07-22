"""Roadmap Phase 3 escrow: the commission_jobs table

Additive-only: one new table ``commission_jobs`` (the A2A commissioned/custom-build
escrow job machine), no change to any existing table, so it is migrate-before-deploy
safe (pre-0027 code never reads it). The ``orders`` table is untouched, so the M3
``ux_orders_active_amount`` partial unique index is trivially safe.

NON-CUSTODIAL: no fund-moving code touches this table. The two fund-relevant columns
(``funded_tx``, ``released_tx``) are hashes of user-signed on-chain transfers Tilla
merely VERIFIED and recorded — it never holds keys or sends funds. UNIQUE(funded_tx) /
UNIQUE(released_tx) make one on-chain transfer usable by a single job ever (SQLite
treats the many unfunded NULLs as distinct); a CHECK bounds ``status`` to the lifecycle.

Revision ID: 0027_commission_jobs
Revises: 0026_attest_content_hash
Create Date: 2026-07-22
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0027_commission_jobs"
down_revision: Union[str, None] = "0026_attest_content_hash"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "commission_jobs",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("buyer_addr", sa.String(length=42), nullable=False),
        sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id"), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("brief", sa.Text(), nullable=False),
        sa.Column("budget_micro", sa.Integer(), nullable=True),
        sa.Column("escrow_addr", sa.String(length=42), nullable=True),
        sa.Column("evaluator_addr", sa.String(length=42), nullable=True),
        sa.Column(
            "status",
            sa.String(length=12),
            nullable=False,
            server_default="open",
        ),
        sa.Column("funded_tx", sa.String(length=66), nullable=True),
        sa.Column("funded_block", sa.Integer(), nullable=True),
        sa.Column("released_tx", sa.String(length=66), nullable=True),
        sa.Column("deliverable", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("funded_at", sa.DateTime(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('open','budget_set','funded','submitted','completed',"
            "'cancelled','disputed')",
            name="ck_commission_jobs_status",
        ),
        sa.CheckConstraint(
            "budget_micro IS NULL OR budget_micro > 0",
            name="ck_commission_jobs_budget_positive",
        ),
        sa.UniqueConstraint("funded_tx", name="uq_commission_jobs_funded_tx"),
        sa.UniqueConstraint("released_tx", name="uq_commission_jobs_released_tx"),
    )
    op.create_index("ix_commission_jobs_store_id", "commission_jobs", ["store_id"])
    op.create_index("ix_commission_jobs_buyer_addr", "commission_jobs", ["buyer_addr"])


def downgrade() -> None:
    op.drop_index("ix_commission_jobs_buyer_addr", table_name="commission_jobs")
    op.drop_index("ix_commission_jobs_store_id", table_name="commission_jobs")
    op.drop_table("commission_jobs")

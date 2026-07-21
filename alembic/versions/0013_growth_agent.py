"""growth agent: draft outbox

Additive-only: one new table ``growth_drafts`` (the M17 growth-agent outbox), no
change to any existing table, so it is migrate-before-deploy safe (pre-0013 code
never reads it).

RENUMBER-AT-INTEGRATION: authored as 0013 with ``down_revision='0009_growth'``
(the current head) because 0010 is pending elsewhere and M15/M16 claim 0011/0012.
When those land, renumber this to the next free head and repoint its
``down_revision`` — this file carries no dependency on 0010/0011/0012, only on the
tables 0009 already created (``stores``).

``growth_drafts`` — the queued marketing copy fanned from a screened growth kit.
INV-1: nothing here ever sends; ``mark-published`` only records a human post. A
``status='blocked'`` row's body is withheld from every read path.

Revision ID: 0013_growth_agent
Revises: 0009_growth
Create Date: 2026-07-21
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013_growth_agent"
down_revision: Union[str, None] = "0009_growth"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "growth_drafts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id"), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "source", sa.String(length=10), nullable=False, server_default="manual"
        ),
        sa.Column(
            "status", sa.String(length=10), nullable=False, server_default="pending"
        ),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("screening_status", sa.String(length=10), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("performance_note", sa.String(length=500), nullable=True),
        sa.CheckConstraint(
            "channel IN ('social','email_subject','launch_tweet')",
            name="ck_growth_drafts_channel",
        ),
        sa.CheckConstraint(
            "source IN ('manual','scheduled')", name="ck_growth_drafts_source"
        ),
        sa.CheckConstraint(
            "status IN ('pending','blocked','approved','published','discarded')",
            name="ck_growth_drafts_status",
        ),
    )
    op.create_index(
        "ix_growth_drafts_store_status", "growth_drafts", ["store_id", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_growth_drafts_store_status", table_name="growth_drafts")
    op.drop_table("growth_drafts")

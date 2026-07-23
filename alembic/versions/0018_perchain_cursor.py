"""perchain_cursor: relax the singleton sweep cursor to one row per chain

18.2 makes sweeping per-chain (own cursor + own head per chain) so a second chain
minting orders can never orphan them or rewind the canonical cursor. Pre-18.2
``chain_cursor`` was a singleton pinned by ``CHECK (id = 1)``; this migration drops
that constraint (via a SQLite table rebuild) and repoints the existing canonical
cursor row from the legacy id=1 to the canonical chain_id (196), so the sweeper
resumes gap-free under per-chain keying. Additional chains (flag+probe-enabled) get
their own rows keyed by chain_id at first sweep — no data needed here.

The rebuild is explicit CREATE + copy (not batch_alter_table) so the CHECK
constraint is provably gone on SQLite; the table is a single row, so a rebuild is
trivial (unlike ``orders``, whose partial unique index we never rebuild). The
``CASE WHEN id = 1 THEN 196`` copy is a no-op on a fresh DB (no cursor row yet).

RENUMBER-AT-INTEGRATION: authored as 0018 on top of ``0014_crosschain`` (this
worktree's M18 head). At integration, renumber this file + ``revision`` to the next
free head and repoint ``down_revision`` to the true predecessor.

Revision ID: 0018_perchain_cursor
Revises: 0014_crosschain
Create Date: 2026-07-21
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0018_perchain_cursor"
down_revision: Union[str, None] = "0017_growth_channels"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.rename_table("chain_cursor", "chain_cursor_old")
    op.create_table(
        "chain_cursor",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("last_block", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.execute(
        "INSERT INTO chain_cursor (id, last_block, updated_at) "
        "SELECT CASE WHEN id = 1 THEN 196 ELSE id END, last_block, updated_at "
        "FROM chain_cursor_old"
    )
    op.drop_table("chain_cursor_old")


def downgrade() -> None:
    op.rename_table("chain_cursor", "chain_cursor_old")
    op.create_table(
        "chain_cursor",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("last_block", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_chain_cursor_singleton"),
    )
    op.execute(
        "INSERT INTO chain_cursor (id, last_block, updated_at) "
        "SELECT CASE WHEN id = 196 THEN 1 ELSE id END, last_block, updated_at "
        "FROM chain_cursor_old"
    )
    op.drop_table("chain_cursor_old")

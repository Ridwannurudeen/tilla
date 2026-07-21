"""attest_nonce: record the attester broadcast nonce for crash-window dedup

M11 attester flag-flip hardening. The dormant EAS attester gains an intermediate
``sending`` state so a crash between broadcasting the attest tx and the DB claim can
never blind-re-broadcast (wasteful OKB gas — NOT fund loss). To reconcile a ``sending``
row against the chain, the worker records the exact broadcast intent BEFORE sending:
``attest_tx`` (already present since 0008) plus the transaction ``nonce``. On restart a
``sending`` row is resolved to ``attested`` if its tx is found, else re-broadcast ONLY
when the recorded nonce proves the attestation is definitively not on chain — the nonce
is the dedup key (at most one tx per nonce can ever mine).

``orders`` gains one nullable column:
  - ``attest_nonce`` INTEGER NULL — the attester account nonce the attest tx was signed
    with. NULL for every existing row and for the non-attesting path; set only when the
    worker claims ``pending -> sending``.

Additive-only PLAIN ``op.add_column`` (SQLite-native ALTER TABLE ADD COLUMN), NOT
``batch_alter_table`` — a batch rebuild of ``orders`` would have to reconstruct the M3
partial unique index ``ux_orders_active_amount`` (0008's constraint). No new state enum
constraint (single-writer worker enforces it, as with ``attest_status``).

RENUMBER-AT-INTEGRATION: authored as 0019 on top of ``0018_perchain_cursor`` (this
worktree's head). At integration, renumber this file + ``revision`` to the next free
head and repoint ``down_revision`` to the true predecessor.

Revision ID: 0019_attest_nonce
Revises: 0018_perchain_cursor
Create Date: 2026-07-21
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0019_attest_nonce"
down_revision: Union[str, None] = "0018_perchain_cursor"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Native ADD COLUMN — no batch rebuild, so the M3 partial unique index
    # ux_orders_active_amount on orders is provably untouched.
    op.add_column("orders", sa.Column("attest_nonce", sa.Integer(), nullable=True))


def downgrade() -> None:
    # Downgrade-only rebuild risk is acceptable (the migration test re-asserts the
    # partial index after up-down-up).
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.drop_column("attest_nonce")

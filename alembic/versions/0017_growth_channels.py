"""growth agent: widen growth_drafts.channel for the M17.4 multi-channel shapes

Additive-only: extend the ``ck_growth_drafts_channel`` CHECK to allow two new draft
channels — ``email_body`` and ``product_update`` — alongside the existing
``social`` / ``email_subject`` / ``launch_tweet``. No column added, no data touched;
pre-0017 rows all carry an already-allowed channel, so up/down/up is lossless.

SQLite cannot ALTER a CHECK in place, so the constraint is swapped via
``batch_alter_table`` (a table rebuild). ``growth_drafts`` carries only a plain
secondary index (``ix_growth_drafts_store_status``) and no partial-unique index, so
the rebuild is safe (unlike the ``orders`` table the M18 migration deliberately
avoided rebuilding). The migration test re-asserts the table after up AND up-down-up.

RENUMBER-AT-INTEGRATION: authored as 0017 with ``down_revision='0014_crosschain'``
(this worktree's head). At integration renumber ``revision`` to the next free head
and repoint ``down_revision`` to the true predecessor.

Revision ID: 0017_growth_channels
Revises: 0014_crosschain
Create Date: 2026-07-21
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0017_growth_channels"
down_revision: Union[str, None] = "0016_federation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD = "channel IN ('social','email_subject','launch_tweet')"
_NEW = (
    "channel IN ('social','email_subject','launch_tweet','email_body','product_update')"
)


def upgrade() -> None:
    with op.batch_alter_table("growth_drafts", schema=None) as batch_op:
        batch_op.drop_constraint("ck_growth_drafts_channel", type_="check")
        batch_op.create_check_constraint("ck_growth_drafts_channel", _NEW)


def downgrade() -> None:
    with op.batch_alter_table("growth_drafts", schema=None) as batch_op:
        batch_op.drop_constraint("ck_growth_drafts_channel", type_="check")
        batch_op.create_check_constraint("ck_growth_drafts_channel", _OLD)

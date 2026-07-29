"""merchants.contact_agent_id — who to tell when something happens to their store

Additive-only: one nullable Integer column, no backfill, no index.

Why it exists: Tilla captured no merchant contact of any kind. `create-store` took
a description, a receive address, a theme and the goods — never a way to reach the
person or agent behind the store. When 36 live stores were found serving a delivery
link that 404s, exactly one of six external merchants could be contacted about it,
and only because their store wallet happened to equal their marketplace agent wallet.

Holds a CLAIMED ERC-8004 agent id. Deliberately not verified on-chain at capture:
that would put a network call in the paid create path, and a slow create is what
timed out a marketplace reviewer at 30s once already. The value is used only to send
a merchant news about their own store, so an incorrect claim leaks the claimant's
data to a third party rather than exposing anyone else's.

NULL — every existing row — means no contact, which is exactly today's behaviour.

Migrate-before-deploy safe: pre-0032 code never reads the column. Downgrade drops it.

Revision ID: 0032_merchant_contact
Revises: 0031_buyer_inputs
Create Date: 2026-07-29
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0032_merchant_contact"
down_revision: Union[str, None] = "0031_buyer_inputs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "merchants", sa.Column("contact_agent_id", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("merchants", "contact_agent_id")

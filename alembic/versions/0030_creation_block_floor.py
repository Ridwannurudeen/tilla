"""store_creations.created_block — a block floor for the self-serve fee

Additive-only: ``store_creations`` gains one nullable ``created_block`` column, the
chain head when the payment intent was opened.

Why it exists: ``self_serve._verify_payment`` accepted ANY succeeded USDT0 transfer of
the exact fee from the merchant to Tilla's rail, with no lower bound in time. The
dashboard fee and the agent-facing x402 ``POST /create-store`` fee are the same amount
to the same address, and the x402 path writes no ``store_creations`` row — so a wallet
that had already paid the x402 fee once could open a dashboard intent and replay its own
historical transfer for a second store it never paid for. ``orders`` has carried exactly
this floor since M3 (``orders.created_block``, enforced in ``checkout`` and
``refunds``); this brings the create-store flow to the same rule: a transfer mined
before the intent existed can never fund it.

Nullable and backfilled to NULL on purpose. NULL means "no floor recorded" and is
treated as unenforced for that row — the two pre-existing rows keep working, and an RPC
blip at intent time degrades to today's behaviour rather than refusing a real payment.

``store_creations`` carries no partial indexes (the M3 ``ux_orders_active_amount`` is on
``orders``, untouched here), so a native ADD COLUMN is trivially safe. Migrate-before-
deploy safe: pre-0030 code never reads the column. Downgrade drops it.

Revision ID: 0030_creation_block_floor
Revises: 0029_custom_domain
Create Date: 2026-07-25
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0030_creation_block_floor"
down_revision: Union[str, None] = "0029_custom_domain"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "store_creations", sa.Column("created_block", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("store_creations", "created_block")

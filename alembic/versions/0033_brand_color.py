"""store_creations.brand_color — the merchant's stated brand colour

Additive-only: one nullable String(7) column, no backfill, no index.

Why it exists: first-hour website feedback was that generated stores felt imposed
("an auto fixed price... merchants should set their own"). Prices got that fix at
the words level; this is the same principle for the store's look. The create form
gains an optional colour; a stated colour's HUE feeds the palette system (harmony,
mood and every contrast floor stay derived, so the choice cannot make the store
illegible), and auto remains the default for everyone who states nothing.

It lives on the payment INTENT, not just the request, because the store is built
after payment — sometimes on a retry long after — and a choice held only in the
browser would vanish on exactly the retry that most needs to reproduce the
original request.

NULL — every existing row — means auto, today's behaviour.

Migrate-before-deploy safe: pre-0033 code never reads the column. Downgrade drops it.

Revision ID: 0033_brand_color
Revises: 0032_merchant_contact
Create Date: 2026-07-29
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0033_brand_color"
down_revision: Union[str, None] = "0032_merchant_contact"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "store_creations", sa.Column("brand_color", sa.String(7), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("store_creations", "brand_color")

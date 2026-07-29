"""products.buyer_inputs + orders.buyer_inputs — what the buyer must tell the merchant

Additive-only: two nullable JSON columns, no backfill, no index.

Why it exists: a store could take payment and have no way to ask the buyer anything.
A due-diligence report needs to know WHICH token; an engraving needs the text. The
buy endpoint accepted no body at all, so a merchant selling a service received money
and no brief, and the buyer received nothing — reported by a merchant whose store
"can take 0.01 and deliver nothing, because its checkout can't collect a token
address".

``products.buyer_inputs`` is the merchant's declaration — a list of
``{name, label, required}``. ``orders.buyer_inputs`` is what the buyer supplied,
``{name: value}``, captured BEFORE settlement so an order that exists always carries
the inputs its product demanded.

NULL on both is the pre-existing world and is what every current row gets: a product
declaring nothing accepts a bodyless buy exactly as before, which is the compatibility
property that matters — an unattended marketplace reviewer POSTs no body, and that must
keep working.

Migrate-before-deploy safe: pre-0031 code never reads either column. ``products`` and
``orders`` both carry indexes, but a native ADD COLUMN of a nullable column rewrites
nothing on SQLite. Downgrade drops both.

Revision ID: 0031_buyer_inputs
Revises: 0030_creation_block_floor
Create Date: 2026-07-29
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0031_buyer_inputs"
down_revision: Union[str, None] = "0030_creation_block_floor"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("products", sa.Column("buyer_inputs", sa.JSON(), nullable=True))
    op.add_column("orders", sa.Column("buyer_inputs", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "buyer_inputs")
    op.drop_column("products", "buyer_inputs")

"""EAS receipt content binding: orders.content_hash

Additive-only on top of 0024. The dormant EAS attester now binds the PRODUCT and a
CONTENT HASH into every receipt — ``orders`` gains ``content_hash`` VARCHAR(66) NULL,
the sha256 (0x + 64 hex) of the delivered payload the attester computes at attest time
and records as part of the broadcast intent (alongside ``attest_tx`` + ``attest_nonce``),
so a receipt is provably bound to WHAT was delivered, not merely that money moved. The
product identity rides the already-present ``orders.product_id`` — no new column for it.

CRITICAL: ``content_hash`` is added with a PLAIN ``op.add_column`` (SQLite-native ALTER
TABLE ADD COLUMN), NOT ``batch_alter_table`` — a batch rebuild of ``orders`` would
reconstruct (and could drop) the M3 partial unique index ``ux_orders_active_amount``.
Native ADD COLUMN leaves every existing index untouched. The column is nullable with no
server_default; every existing row stays NULL (never attested), so enabling the attester
later binds only post-enable sales.

Downgrade drops the column via ``batch_alter_table`` (an orders rebuild); the test
re-asserts ``ux_orders_active_amount`` survives the down/up round-trip.

Revision ID: 0026_attest_content_hash
Revises: 0025_reviews
Create Date: 2026-07-22
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0026_attest_content_hash"
down_revision: Union[str, None] = "0025_reviews"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Native ADD COLUMN — no batch rebuild, so the M3 partial unique index
    # ux_orders_active_amount on orders is provably untouched.
    op.add_column(
        "orders", sa.Column("content_hash", sa.String(length=66), nullable=True)
    )


def downgrade() -> None:
    # Downgrade-only rebuild risk is acceptable (the migration test re-asserts the
    # partial index after up-down-up).
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.drop_column("content_hash")

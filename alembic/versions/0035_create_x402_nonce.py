"""stores.create_x402_nonce — the payment a create can be compensated against

Additive-only: one nullable column on ``stores`` — ``create_x402_nonce``
(String(66), the EIP-3009 authorization nonce of the x402 payment that funded the
create, the same value the buy path stores as ``orders.x402_nonce``) — plus a
NON-unique index for the compensator's lookup. No backfill.

Why it exists (docs/ISSUES.md #11): the x402 middleware calls the handler FIRST and
settles afterwards, so ``create_store`` has already committed the Store, its Products
and its Deliverable by the time settlement is attempted. If settlement then FAILS, a
live store persists that nobody paid for — and since ``/create-store`` had no
``settlement_failed_response_body``, the caller got a bare 402 with an empty body, no
slug and no manage_key. The 0034 Idempotency-Key 409 then made that orphan
retrievable, turning an unusable unpaid store into a delivered free one. Recording the
nonce ON the store row is what lets the settle-failure hook find the store its own
failed payment created and quarantine it (status='blocked' — refused by every money
path) instead of guessing.

Written INSIDE the store's own transaction (engine.create_store), never patched on
afterwards — the same reasoning as 0034: a post-commit write reintroduces exactly the
window it is supposed to close.

NON-UNIQUE, unlike ``ux_orders_x402_nonce``. A nonce is single-use on chain, but a
settle that FAILED never consumed it, so the honest retry the 402 body invites arrives
carrying the SAME nonce and legitimately creates a second store. A unique index would
turn that retry into an IntegrityError inside create_store's insert, where 0034's
classifier — it asks only "is this the idempotency pair?" — would read it as a slug
collision, rmtree the directory, re-slug and then raise: the one recovery path we
promise, broken by the index meant to protect it. The hook does not need uniqueness;
it refuses to act when the handle is ambiguous.

TWO STEPS, and they cannot be merged even though this index is not unique: keeping the
column and the index as separate PLAIN ops is what 0034 had to do (SQLite refuses
``ALTER TABLE ... ADD COLUMN x TEXT UNIQUE``) and what 0019's warning requires — a
``batch_alter_table`` rebuild of ``stores`` would have to reconstruct the 0029
custom-domain unique index and the 0034 idempotency index by reflection. Native ADD
COLUMN touches neither. Every existing store backfills to NULL, which means "no
recorded payment" and is byte-identical to today's behaviour.

Migrate-before-deploy safe: pre-0035 code never reads the column. Downgrade drops the
index then the column.

Revision ID: 0035_create_x402_nonce
Revises: 0034_create_idempotency
Create Date: 2026-08-05
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0035_create_x402_nonce"
down_revision: Union[str, None] = "0034_create_idempotency"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "stores",
        sa.Column("create_x402_nonce", sa.String(length=66), nullable=True),
    )
    op.create_index(
        "ix_stores_create_x402_nonce",
        "stores",
        ["create_x402_nonce"],
    )


def downgrade() -> None:
    op.drop_index("ix_stores_create_x402_nonce", table_name="stores")
    op.drop_column("stores", "create_x402_nonce")

"""stores.create_idempotency_(addr|key) — one paid create per payer per key

Additive-only: two nullable columns on ``stores`` — ``create_idempotency_addr``
(String(42), the EIP-3009 payer of the x402 payment that funded the create) and
``create_idempotency_key`` (String(200), the client's ``Idempotency-Key`` header) —
plus a composite UNIQUE index over the pair, so one key can fund at most one store
per payer, ever. No backfill.

Why it exists: a buyer (0xqdee, 2026-08-05) paid for a store, the store WAS created
and the payment DID settle — then a deploy-window 502 lost the response. Holding no
slug and no manage_key, the only move left is a retry, and a retry carrying a NEW
signed payment buys a SECOND store and pays a SECOND time. Payment-keyed idempotency
cannot close that: a retry with the same proof is rejected upstream as a consumed
nonce and never reaches the handler, so the surviving handle has to be one the client
chose. A repeat request whose (payer, key) matches an existing store is answered 409 —
which is also what makes the retry free, because the x402 middleware skips settlement
on any status >= 400.

Scoped to the PAYER, never the bare key — the same tenant-scoped shape as
``uq_acp_sessions_store_idem`` ("no cross-tenant replay"). A global key space would
let the first caller to use "retry-1" refuse every later caller's paid create for a
string they never chose, and hand them that stranger's slug, url, product and price.

TWO STEPS, and they cannot be merged: SQLite refuses
``ALTER TABLE ... ADD COLUMN x TEXT UNIQUE`` outright ("Cannot add a UNIQUE column"),
so the columns are added plain and the unique index is created separately. Both are
PLAIN ops, never ``batch_alter_table`` — a batch rebuild of ``stores`` would have to
reconstruct every index by reflection (0019's warning), and this table now carries the
0029 custom-domain unique index as well as this one. ``stores`` has no partial index,
so native ADD COLUMN is trivially safe on the live DB; every existing store backfills
to (NULL, NULL), which means "no key" and is byte-identical to today's behaviour.
SQLite counts a row with any NULL in the tuple as distinct, so the index only ever
fires on a real duplicate — the ``ux_orders_x402_nonce`` precedent.

Migrate-before-deploy safe: pre-0034 code never reads either column. Downgrade drops
the index then the columns.

Revision ID: 0034_create_idempotency
Revises: 0033_brand_color
Create Date: 2026-08-05
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0034_create_idempotency"
down_revision: Union[str, None] = "0033_brand_color"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "stores",
        sa.Column("create_idempotency_addr", sa.String(length=42), nullable=True),
    )
    op.add_column(
        "stores",
        sa.Column("create_idempotency_key", sa.String(length=200), nullable=True),
    )
    op.create_index(
        "uq_stores_create_idem",
        "stores",
        ["create_idempotency_addr", "create_idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_stores_create_idem", table_name="stores")
    op.drop_column("stores", "create_idempotency_key")
    op.drop_column("stores", "create_idempotency_addr")

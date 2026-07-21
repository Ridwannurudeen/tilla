"""gated delivery

Additive-only on top of 0002. Two NULLable columns (stores.manage_key_hash,
orders.buyer_email) and four new tables (deliverables, entitlements, auth_nonces,
license_activations) plus the from_addr lookup index. No backfill: a NULL
manage_key_hash and the absence of any deliverable row ARE the legacy behaviour,
so every existing store keeps serving its store.delivery text unchanged.
Downgrade is a clean reversal.

Revision ID: 0003_gated_delivery
Revises: 0002_hardened_checkout
Create Date: 2026-07-21
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_gated_delivery"
down_revision: Union[str, None] = "0002_hardened_checkout"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("stores", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("manage_key_hash", sa.String(length=64), nullable=True)
        )

    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("buyer_email", sa.String(length=255), nullable=True)
        )
        batch_op.create_index("ix_orders_from_addr", ["from_addr"], unique=False)

    op.create_table(
        "deliverables",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("store_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.Column("file_sha256", sa.String(length=64), nullable=True),
        sa.Column("file_name", sa.String(length=255), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=True),
        sa.Column("mime", sa.String(length=100), nullable=True),
        sa.Column("max_downloads", sa.Integer(), nullable=False, server_default="5"),
        sa.Column(
            "link_ttl_seconds", sa.Integer(), nullable=False, server_default="86400"
        ),
        sa.Column("max_activations", sa.Integer(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('file','text','license')", name="ck_deliverables_kind"
        ),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_deliverables_store_id", "deliverables", ["store_id"], unique=False
    )

    op.create_table(
        "entitlements",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.String(length=32), nullable=False),
        sa.Column("deliverable_id", sa.Integer(), nullable=False),
        sa.Column("buyer_addr", sa.String(length=42), nullable=True),
        sa.Column("download_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("activations_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("license_key", sa.String(length=64), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["deliverable_id"], ["deliverables.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id", name="uq_entitlements_order_id"),
        sa.UniqueConstraint("license_key", name="uq_entitlements_license_key"),
    )
    op.create_index(
        "ix_entitlements_deliverable_id",
        "entitlements",
        ["deliverable_id"],
        unique=False,
    )

    op.create_table(
        "auth_nonces",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("address", sa.String(length=42), nullable=False),
        sa.Column("nonce", sa.String(length=32), nullable=False),
        sa.Column("issued_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_auth_nonces_address", "auth_nonces", ["address"], unique=False)

    op.create_table(
        "license_activations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("entitlement_id", sa.Integer(), nullable=False),
        sa.Column("device_id", sa.String(length=128), nullable=False),
        sa.Column("activated_at", sa.DateTime(), nullable=False),
        sa.Column("deactivated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["entitlement_id"], ["entitlements.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "entitlement_id", "device_id", name="uq_license_activation_device"
        ),
    )


def downgrade() -> None:
    op.drop_table("license_activations")
    op.drop_index("ix_auth_nonces_address", table_name="auth_nonces")
    op.drop_table("auth_nonces")
    op.drop_index("ix_entitlements_deliverable_id", table_name="entitlements")
    op.drop_table("entitlements")
    op.drop_index("ix_deliverables_store_id", table_name="deliverables")
    op.drop_table("deliverables")

    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.drop_index("ix_orders_from_addr")
        batch_op.drop_column("buyer_email")

    with op.batch_alter_table("stores", schema=None) as batch_op:
        batch_op.drop_column("manage_key_hash")

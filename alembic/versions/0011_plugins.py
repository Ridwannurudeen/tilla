"""plugins: M15.1 provider registry

Additive-only (migrate-before-deploy safe; the running pre-0011 code never reads
the new table). Adds one ``plugins`` table — the metadata + review-state surface
for the three formalized extension points (delivery / payment_rail / theme) — and
seeds a row for every built-in (``source='builtin', status='active'``). No
existing table is touched, so the M3 partial unique index ``ux_orders_active_amount``
on ``orders`` is trivially unaffected (the migration test re-asserts it).

NUMBERING: authored as 0011 with down_revision 0009 because 0010 is pending on
another branch. RENUMBER this to the next free head (0010) at integration time,
once that branch lands, keeping the linear chain intact.

Revision ID: 0011_plugins
Revises: 0009_growth
Create Date: 2026-07-21
"""

from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011_plugins"
down_revision: Union[str, None] = "0009_growth"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The built-in seed rows — mirrors app.providers.BUILTIN_SEED. Kept as a literal
# here (not an app import) so the migration stays self-contained and replayable.
_BUILTINS = (
    ("delivery", "file", {"seam": "download_token"}),
    ("delivery", "text", {"seam": "text_secret"}),
    ("delivery", "license", {"seam": "license_key"}),
    ("payment_rail", "exact", {"scheme": "exact"}),
    ("payment_rail", "mpp", {"family": "endpoint"}),
    ("payment_rail", "period", {"family": "endpoint"}),
    ("theme", "original", {"template": "original.html"}),
    ("theme", "bold", {"template": "bold.html"}),
    ("theme", "editorial", {"template": "editorial.html"}),
)


def upgrade() -> None:
    plugins = op.create_table(
        "plugins",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column("version", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=10), nullable=False),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=True),
        sa.Column("manifest", sa.JSON(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="pending_review",
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("kind", "name", name="uq_plugins_kind_name"),
        sa.CheckConstraint(
            "kind IN ('delivery','payment_rail','theme')", name="ck_plugins_kind"
        ),
        sa.CheckConstraint(
            "source IN ('builtin','external')", name="ck_plugins_source"
        ),
        sa.CheckConstraint(
            "status IN ('pending_review','active','disabled')",
            name="ck_plugins_status",
        ),
    )
    op.create_index("ix_plugins_kind_status", "plugins", ["kind", "status"])

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    op.bulk_insert(
        plugins,
        [
            {
                "kind": kind,
                "name": name,
                "version": "1",
                "source": "builtin",
                "artifact_sha256": None,
                "manifest": manifest,
                "status": "active",
                "created_at": now,
            }
            for kind, name, manifest in _BUILTINS
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_plugins_kind_status", table_name="plugins")
    op.drop_table("plugins")

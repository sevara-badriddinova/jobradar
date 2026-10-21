"""Digest of each board's last fully ingested snapshot.

Additive and instant (nullable columns, no rewrite). A worker poll whose
snapshot digest equals the stored one skips ingestion entirely; the digest is
trusted for at most a day, so every board is still fully re-ingested daily.

Revision ID: 0011
Revises: 0010
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '15s'")
    op.add_column("board_state", sa.Column("snapshot_hash", sa.Text))
    op.add_column("board_state", sa.Column("snapshot_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("board_state", "snapshot_at")
    op.drop_column("board_state", "snapshot_hash")

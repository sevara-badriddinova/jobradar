"""Hard filters: record when a job was filtered and why it was rejected.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("filtered_at", sa.DateTime(timezone=True)))
    op.add_column("jobs", sa.Column("filter_reason", sa.Text))


def downgrade() -> None:
    op.drop_column("jobs", "filter_reason")
    op.drop_column("jobs", "filtered_at")

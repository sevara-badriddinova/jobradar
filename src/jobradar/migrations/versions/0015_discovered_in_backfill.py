"""jobs.discovered_in_backfill: found by its board's initial scan.

Freshness now also accepts postings JobRadar first saw within the alert window
whose employer date is within max_discovery_age_days, except postings from a
board's initial scan (a newly added board's whole backlog). Existing rows
default to false; their first_seen_at is old, so they are unaffected.

ADD COLUMN with a constant default is metadata-only in PostgreSQL 11+ (no
table rewrite, brief lock). The previous image ignores the column.

Revision ID: 0015
Revises: 0014
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("jobs", sa.Column("discovered_in_backfill", sa.Boolean, nullable=False,
                                    server_default=sa.text("false")))


def downgrade():
    op.drop_column("jobs", "discovered_in_backfill")

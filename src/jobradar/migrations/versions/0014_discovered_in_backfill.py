"""jobs.discovered_in_backfill: found by its board's initial scan.

Freshness now also accepts postings JobRadar first saw within the alert window
whose employer date is within max_discovery_age_days, except postings from a
board's initial scan (a newly added board's whole backlog). Existing rows
default to false; their first_seen_at is old, so they are unaffected.

ADD COLUMN with a constant default is metadata-only in PostgreSQL 11+ (no
table rewrite, brief lock). The previous image ignores the column.

Data step: re-queue recent "older than" rejections the discovery rule may now
accept (see upgrade()), instead of a full filter recheck.

Revision ID: 0014
Revises: 0013
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("jobs", sa.Column("discovered_in_backfill", sa.Boolean, nullable=False,
                                    server_default=sa.text("false")))
    # The filter policy signature excludes the discovery window (no full
    # recheck on deploy). Instead, re-queue the only rows it can change: open
    # jobs rejected as too old that JobRadar first saw in the last 3 days and
    # whose employer date is within 30 days (a superset of any window). The
    # next filter pass decides them with the configured rule; the previous
    # image would simply reject them again. Index: ix_jobs_status_first_seen.
    op.execute(sa.text(
        "UPDATE jobs SET filtered_at = NULL WHERE status = 'FILTERED' AND closed_at IS NULL "
        "AND first_seen_at > now() - interval '3 days' AND posted_at > now() - interval '30 days' "
        "AND filter_reason LIKE 'posting date: older than %'"))


def downgrade():
    op.drop_column("jobs", "discovered_in_backfill")

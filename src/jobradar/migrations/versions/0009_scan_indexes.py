"""Bound the disk work for incremental filtering and application linking."""

from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

INDEXES = [
    ("ix_jobs_company_key", ["company_key"], None),
    ("ix_jobs_open_publication", ["posted_at", "id"], "closed_at IS NULL"),
    ("ix_jobs_pending_filter", ["id"],
     "closed_at IS NULL AND (filtered_at IS NULL OR filter_reason = 'posting date: in the future')"),
    ("ix_jobs_expiry_candidates", ["posted_at"],
     "closed_at IS NULL AND (filtered_at IS NULL OR status <> 'FILTERED' OR filter_reason IS NULL "
     "OR filter_reason NOT LIKE 'posting date:%' OR filter_reason = 'posting date: in the future')"),
]


def upgrade():
    # Avoid blocking the listener's writes while indexing the existing history.
    # Drop only our invalid remnants if a previous concurrent build was killed.
    with op.get_context().autocommit_block():
        for name, columns, predicate in INDEXES:
            valid = op.get_bind().execute(sa.text(
                "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)"
            ), {"name": name}).scalar_one_or_none()
            if valid is False:
                op.drop_index(name, table_name="jobs", postgresql_concurrently=True)
            if valid is not True:
                op.create_index(name, "jobs", columns, postgresql_concurrently=True,
                                postgresql_where=sa.text(predicate) if predicate else None)


def downgrade():
    with op.get_context().autocommit_block():
        for name, _, _ in reversed(INDEXES):
            op.drop_index(name, table_name="jobs", postgresql_concurrently=True)

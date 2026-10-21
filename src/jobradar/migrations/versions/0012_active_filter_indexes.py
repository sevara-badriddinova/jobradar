"""Filter-sweep indexes restricted to filterable statuses.

The 0009 partial indexes also contain every never-filtered DUPLICATE row
(duplicates are never filtered, so filtered_at stays NULL forever). On the
production copy that made PostgreSQL estimate ~58k candidates for the
"posting date: unavailable" sweep and choose a sequential scan of the whole
jobs heap (717 MB, ~36 s of disk on the VM) every ten minutes, and made the
pending-job scan heap-check every duplicate. These indexes hold only rows the
sweep can actually change, so both statements become small index scans.

Built CONCURRENTLY (no write lock on jobs); safe to re-run after an interruption.

Revision ID: 0012
Revises: 0011
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

ACTIVE = "closed_at IS NULL AND status IN ('DISCOVERED', 'REPOSTED', 'FILTERED', 'MATCHED', 'NOTIFIED')"
INDEXES = [
    ("ix_jobs_pending_active", ["id"],
     f"{ACTIVE} AND (filtered_at IS NULL OR filter_reason = 'posting date: in the future')"),
    ("ix_jobs_expiry_active", ["posted_at"],
     f"{ACTIVE} AND (filtered_at IS NULL OR status <> 'FILTERED' OR filter_reason IS NULL "
     "OR filter_reason NOT LIKE 'posting date:%' OR filter_reason = 'posting date: in the future')"),
]


def upgrade():
    with op.get_context().autocommit_block():
        for name, columns, predicate in INDEXES:
            valid = op.get_bind().execute(sa.text(
                "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)"), {"name": name}
            ).scalar_one_or_none()
            if valid is False:
                op.drop_index(name, table_name="jobs", postgresql_concurrently=True)
            if valid is not True:
                op.create_index(name, "jobs", columns, postgresql_concurrently=True,
                                postgresql_where=sa.text(predicate))


def downgrade():
    with op.get_context().autocommit_block():
        for name, _, _ in reversed(INDEXES):
            op.drop_index(name, table_name="jobs", postgresql_concurrently=True)

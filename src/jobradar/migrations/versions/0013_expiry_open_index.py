"""Expiry-sweep index without content-rejected rows.

The sweep no longer relabels title/visa/experience/location rejections as
"posting date: older than N days" once they age (that rewrote thousands of
rows a day). Its candidate set is now only unfiltered, still-eligible
(DISCOVERED/MATCHED/NOTIFIED) and future-dated open jobs, a few thousand rows
instead of every recent rejection; this index holds exactly that set.

0012's ix_jobs_expiry_active is kept so the previous image still finds the
index its own sweep needs (rollback compatibility); a later cleanup migration
can drop it once the new image is the only one in use.

Built CONCURRENTLY (no write lock on jobs); safe to re-run after an interruption.

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

ACTIVE = "closed_at IS NULL AND status IN ('DISCOVERED', 'REPOSTED', 'FILTERED', 'MATCHED', 'NOTIFIED')"
INDEXES = [
    ("ix_jobs_expiry_open", ["posted_at"],
     f"{ACTIVE} AND (status <> 'FILTERED' OR filtered_at IS NULL OR filter_reason IS NULL "
     "OR filter_reason = 'posting date: in the future')"),
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

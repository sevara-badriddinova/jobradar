"""jobs.raw_digest: content of a pruned job.

`jobradar prune-content` empties the description and raw payload of jobs that
can never qualify again (filtered or duplicate, undated or published long ago)
and keeps the MD5 of the payload as compared by ingestion (volatile keys
removed) here. Ingestion compares a pruned row by this digest and restores the
full content whenever the posting changes, reopens or gets a new date.
NULL for every row that keeps its content.

ADD COLUMN without a default is metadata-only (no table rewrite). The previous
image ignores the column; it would see pruned rows as changed and rewrite them.

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
    # Never queue behind a long transaction on the hot jobs table.
    op.execute("SET LOCAL lock_timeout = '15s'")
    op.add_column("jobs", sa.Column("raw_digest", sa.Text))


def downgrade():
    op.drop_column("jobs", "raw_digest")

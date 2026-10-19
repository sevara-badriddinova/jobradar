"""Remove Greenhouse update timestamps from publication dates.

Revision ID: 0008
Revises: 0007
"""

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    table = sa.table("jobs", sa.column("id", sa.BigInteger), sa.column("source", sa.Text),
                     sa.column("raw", sa.JSON), sa.column("posted_at", sa.DateTime(timezone=True)))
    for row in conn.execute(sa.select(table.c.id, table.c.raw).where(table.c.source == "greenhouse")):
        value = (row.raw or {}).get("first_published")
        try:
            published = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if published.tzinfo is None:
                published = published.replace(tzinfo=UTC)
        except (AttributeError, TypeError, ValueError):
            published = None
        conn.execute(table.update().where(table.c.id == row.id).values(posted_at=published))


def downgrade():
    # Correct publication dates remain valid for older versions as well.
    pass

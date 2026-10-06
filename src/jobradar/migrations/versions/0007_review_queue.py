"""Persistent job review choices and Telegram queue summaries."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_reviews",
        sa.Column("job_id", sa.BigInteger, sa.ForeignKey("jobs.id"), primary_key=True),
        sa.Column("choice", sa.Text, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("choice IN ('QUEUED','IGNORED','SPONSORSHIP','APPLIED')", name="ck_job_reviews_choice"),
    )
    # Preserve old button choices. An application fact takes precedence over
    # an old ignore click, even when the latter was recorded more recently.
    op.execute("""
        INSERT INTO job_reviews (job_id, choice, updated_at)
        SELECT DISTINCT ON (job_id) job_id, kind, created_at
        FROM feedback WHERE kind IN ('APPLIED', 'IGNORED')
        ORDER BY job_id, (kind = 'APPLIED') DESC, created_at DESC, id DESC
    """)
    op.create_table(
        "review_responses",
        sa.Column("update_id", sa.BigInteger, primary_key=True),
        sa.Column("part", sa.Integer, primary_key=True),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("message_id", sa.BigInteger),
        sa.CheckConstraint("status IN ('pending','sending','sent','failed')", name="ck_review_responses_status"),
    )


def downgrade() -> None:
    op.drop_table("review_responses")
    op.drop_table("job_reviews")

"""AI match assessments, cached per (job, matching context); jobs point at their latest one.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_matches",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("job_id", sa.BigInteger, sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("profile_id", sa.BigInteger, sa.ForeignKey("candidate_profiles.id"), nullable=False),
        sa.Column("context_hash", sa.Text, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("prompt_version", sa.Text, nullable=False),
        sa.Column("assessment", JSONB, nullable=False),
        sa.Column("score", sa.Numeric(4, 1), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("job_id", "context_hash", name="uq_job_matches_job_context"),
    )
    op.add_column("jobs", sa.Column("match_id", sa.BigInteger,
                                    sa.ForeignKey("job_matches.id", name="jobs_match_id_fkey")))


def downgrade() -> None:
    op.drop_column("jobs", "match_id")
    op.drop_table("job_matches")

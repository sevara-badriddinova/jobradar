"""Baseline: the schema as created by `init-db` before migrations existed (slices 1-4).

Revision ID: 0001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)
STATUSES = ("DISCOVERED", "FILTERED", "MATCHED", "NOTIFIED", "APPLIED", "REJECTED", "REPOSTED", "DUPLICATE")


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("board", sa.Text, nullable=False),
        sa.Column("source_job_id", sa.Text, nullable=False),
        sa.Column("company", sa.Text, nullable=False),
        sa.Column("company_key", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("location", sa.Text),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("canonical_url", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("description_hash", sa.Text, nullable=False),
        sa.Column("fingerprint", sa.Text, nullable=False),
        sa.Column("posted_at", TS),
        sa.Column("first_seen_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("content_changed_at", TS),
        sa.Column("closed_at", TS),
        sa.Column("status", sa.Text, nullable=False, server_default="DISCOVERED"),
        sa.Column("duplicate_of_id", sa.BigInteger, sa.ForeignKey("jobs.id")),
        sa.Column("dedup_reason", sa.Text),
        sa.Column("match_score", sa.Numeric(5, 2)),
        sa.Column("notified_at", TS),
        sa.Column("applied_at", TS),
        sa.Column("raw", JSONB, nullable=False),
        sa.UniqueConstraint("source", "source_job_id", name="uq_jobs_source_job"),
        sa.CheckConstraint("status IN (" + ",".join(f"'{s}'" for s in STATUSES) + ")", name="ck_jobs_status"),
        sa.CheckConstraint("(duplicate_of_id IS NULL) = (dedup_reason IS NULL)", name="ck_jobs_dedup_link"),
    )
    op.create_index("ix_jobs_canonical_url", "jobs", ["canonical_url"])
    op.create_index("ix_jobs_fingerprint", "jobs", ["fingerprint"])
    op.create_index("ix_jobs_open_by_board", "jobs", ["source", "board"],
                    postgresql_where=sa.text("closed_at IS NULL"))
    op.create_index("ix_jobs_status_first_seen", "jobs", ["status", "first_seen_at"])

    op.create_table(
        "companies",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("collector", sa.Text, nullable=False),
        sa.Column("board", sa.Text, nullable=False),
        sa.Column("priority", sa.Integer, nullable=False, server_default="2"),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("collector", "board", name="uq_companies_collector_board"),
        sa.CheckConstraint("priority BETWEEN 1 AND 3", name="ck_companies_priority"),
        sa.CheckConstraint("board = lower(board) AND board <> ''", name="ck_companies_board_normalized"),
    )

    op.create_table(
        "collector_runs",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("board", sa.Text, nullable=False),
        sa.Column("started_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", TS),
        sa.Column("ok", sa.Boolean),
        *(sa.Column(name, sa.Integer) for name in
          ("fetched", "new", "seen", "changed", "duplicates", "closed", "reopened")),
        sa.Column("error", sa.Text),
    )
    op.create_index("ix_collector_runs_board_started", "collector_runs", ["source", "board", "started_at"])


def downgrade() -> None:
    op.drop_table("collector_runs")
    op.drop_table("companies")
    op.drop_table("jobs")

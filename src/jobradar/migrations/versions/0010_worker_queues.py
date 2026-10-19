"""Durable PostgreSQL work queues for the continuous multi-worker pipeline.

Additive only: the legacy `cycle` keeps working against this schema.

- board_state: one row per registry board; the collection schedule, lease,
  failure backoff, and last payload size (used to reserve the byte budget).
- work_queue: filter/match tasks, unique per (kind, job). Claimed with
  FOR UPDATE SKIP LOCKED; expired leases are reclaimable; `generation` makes a
  re-enqueue during processing re-run the task instead of being lost.
- notifications.origin_at: when the job became new/changed, for latency.
- ai_usage: per-UTC-day, per-model token and cost ledger behind the daily cap.

Revision ID: 0010
Revises: 0009
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    # Never queue behind a long transaction on the hot notifications table.
    op.execute("SET LOCAL lock_timeout = '15s'")
    op.create_table(
        "board_state",
        sa.Column("company_id", sa.BigInteger, sa.ForeignKey("companies.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("next_due_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("lease_owner", sa.Text),
        sa.Column("lease_until", TS),
        sa.Column("consecutive_failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_started_at", TS),
        sa.Column("last_finished_at", TS),
        sa.Column("last_success_at", TS),
        sa.Column("last_error", sa.Text),
        sa.Column("last_bytes", sa.BigInteger),
        sa.Column("last_fetched", sa.Integer),
    )
    op.create_index("ix_board_state_due", "board_state", ["next_due_at"])
    op.create_table(
        "work_queue",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("job_id", sa.BigInteger, sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("state", sa.Text, nullable=False, server_default="ready"),
        sa.Column("priority", sa.SmallInteger, nullable=False, server_default="100"),
        sa.Column("generation", sa.Integer, nullable=False, server_default="0"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer, nullable=False, server_default="5"),
        sa.Column("available_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("origin_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("lease_owner", sa.Text),
        sa.Column("lease_until", TS),
        sa.Column("last_error", sa.Text),
        sa.UniqueConstraint("kind", "job_id", name="uq_work_queue_kind_job"),
        sa.CheckConstraint("kind IN ('filter','match')", name="ck_work_queue_kind"),
        sa.CheckConstraint("state IN ('ready','leased','dead')", name="ck_work_queue_state"),
    )
    op.create_index("ix_work_queue_claim", "work_queue", ["kind", "priority", "available_at", "id"],
                    postgresql_where=sa.text("state <> 'dead'"))
    op.add_column("notifications", sa.Column("origin_at", TS))
    op.create_table(
        "ai_usage",
        sa.Column("day", sa.Date, primary_key=True),
        sa.Column("model", sa.Text, primary_key=True),
        sa.Column("calls", sa.Integer, nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("ai_usage")
    op.drop_column("notifications", "origin_at")
    op.drop_index("ix_work_queue_claim", table_name="work_queue")
    op.drop_table("work_queue")
    op.drop_index("ix_board_state_due", table_name="board_state")
    op.drop_table("board_state")

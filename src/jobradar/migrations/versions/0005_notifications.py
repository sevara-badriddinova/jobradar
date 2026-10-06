"""Notifications outbox, user feedback, and small persistent app state.

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("job_id", sa.BigInteger, sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("channel", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text),
        sa.Column("message_id", sa.BigInteger),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("sent_at", TS),
        sa.UniqueConstraint("job_id", "channel", name="uq_notifications_job_channel"),
        sa.CheckConstraint("kind IN ('immediate','digest')", name="ck_notifications_kind"),
        sa.CheckConstraint("status IN ('pending','sending','sent','failed','cancelled')",
                           name="ck_notifications_status"),
    )
    op.create_index("ix_notifications_status_kind", "notifications", ["status", "kind"])
    op.create_table(
        "feedback",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("job_id", sa.BigInteger, sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("job_id", "kind", name="uq_feedback_job_kind"),
        sa.CheckConstraint("kind IN ('APPLIED','IGNORED','INTERVIEW','REJECTED','OFFER')",
                           name="ck_feedback_kind"),
    )
    op.create_table(
        "app_state",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("app_state")
    op.drop_table("feedback")
    op.drop_table("notifications")

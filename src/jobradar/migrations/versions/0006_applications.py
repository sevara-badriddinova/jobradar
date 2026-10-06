"""Applications you made (from Gmail, Notion or by hand) and emails already read.

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "applications",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("external_id", sa.Text, nullable=False),
        sa.Column("company", sa.Text, nullable=False),
        sa.Column("company_key", sa.Text, nullable=False),
        sa.Column("role", sa.Text),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("occurred_at", TS),
        sa.Column("job_id", sa.BigInteger, sa.ForeignKey("jobs.id")),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("source", "external_id", name="uq_applications_source_external"),
        sa.CheckConstraint("kind IN ('APPLIED','REJECTED','INTERVIEW','OFFER')", name="ck_applications_kind"),
    )
    op.create_index("ix_applications_company_key", "applications", ["company_key"])
    op.create_table(
        "processed_emails",
        sa.Column("message_id", sa.Text, primary_key=True),
        sa.Column("verdict", sa.Text, nullable=False),
        sa.Column("processed_at", TS, nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("processed_emails")
    op.drop_table("applications")

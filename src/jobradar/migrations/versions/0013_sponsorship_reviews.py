"""Sponsorship review queue.

Fresh jobs that pass every hard filter but neither offer nor refuse visa
sponsorship were rejected ("no explicit sponsorship"). They are now held in
this deduplicated queue for the user (no AI calls, no Telegram alerts);
explicit support still goes to the AI pipeline, explicit refusal is rejected.

Data step: open jobs rejected for "no explicit sponsorship" within the last
3 days are made pending again (filtered_at = NULL), so the next filter pass
moves them into the queue. Rollback-safe: the previous image re-filters them
to the same rejection; the new table is simply ignored.

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

OLD_REASON = "visa: no explicit sponsorship or STEM OPT support in this posting"


def upgrade():
    op.create_table(
        "sponsorship_reviews",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("fingerprint", sa.Text, nullable=False),
        sa.Column("job_id", sa.BigInteger, sa.ForeignKey("jobs.id", ondelete="SET NULL")),
        sa.Column("company", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("location", sa.Text),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True)),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("state", sa.Text, nullable=False, server_default="pending"),
        sa.Column("resolution", sa.Text),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("fingerprint", name="uq_sponsorship_reviews_fingerprint"),
        sa.CheckConstraint("state IN ('pending','kept','dismissed','resolved')",
                           name="ck_sponsorship_reviews_state"),
    )
    op.create_index("ix_sponsorship_reviews_job", "sponsorship_reviews", ["job_id"])
    op.create_index("ix_sponsorship_reviews_pending", "sponsorship_reviews", ["last_seen_at"],
                    postgresql_where=sa.text("state = 'pending'"))
    # Small (hundreds of rows): posted_at range via the open-publication index.
    op.execute(sa.text(
        "UPDATE jobs SET filtered_at = NULL WHERE closed_at IS NULL AND status = 'FILTERED' "
        "AND filter_reason = :reason AND posted_at > now() - interval '3 days'").bindparams(reason=OLD_REASON))


def downgrade():
    op.drop_index("ix_sponsorship_reviews_pending", table_name="sponsorship_reviews")
    op.drop_index("ix_sponsorship_reviews_job", table_name="sponsorship_reviews")
    op.drop_table("sponsorship_reviews")

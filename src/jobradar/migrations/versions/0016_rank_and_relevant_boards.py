"""Newest postings first; productive boards checked more often.

- work_queue.rank_at: the job's employer publication date, set when a match
  task is queued. Matching takes the newest publication day first. Tasks queued
  before this migration keep NULL and are taken after every dated one, in their
  old order; nothing is re-queued or re-scored.
- board_state.relevant_at: last time one of the board's postings passed the
  filters. Such boards are revisited more often (worker.relevant_interval).
  Seeded from the boards of jobs scored in the last 14 days or waiting to be
  scored (a few hundred primary-key lookups, no jobs scan).

Both columns are nullable without a default: metadata-only ALTERs. The previous
image ignores them.

Revision ID: 0016
Revises: 0015
"""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade():
    # Never queue behind a long transaction on these hot tables.
    op.execute("SET LOCAL lock_timeout = '15s'")
    op.add_column("work_queue", sa.Column("rank_at", sa.DateTime(timezone=True)))
    op.add_column("board_state", sa.Column("relevant_at", sa.DateTime(timezone=True)))
    op.execute(sa.text("""
        UPDATE board_state b SET relevant_at = now()
        FROM companies c
        WHERE c.id = b.company_id AND (c.collector, c.board) IN (
            SELECT j.source, j.board FROM jobs j
            WHERE j.id IN (SELECT job_id FROM job_matches WHERE created_at > now() - interval '14 days'
                           UNION SELECT job_id FROM work_queue WHERE kind = 'match'))
    """))


def downgrade():
    op.drop_column("board_state", "relevant_at")
    op.drop_column("work_queue", "rank_at")

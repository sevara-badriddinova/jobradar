"""Schema. PostgreSQL is the source of truth for what has been seen.

SQLAlchemy Core (not the ORM) is used on purpose: ingestion is set-based work
(bulk upserts, ON CONFLICT, conditional updates), where explicit SQL is
clearer and faster than tracking objects in a session.
"""

import os

from alembic import command
from alembic.config import Config
from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Column, DateTime, ForeignKey, Identity,
    Index, Integer, MetaData, Numeric, Table, Text, UniqueConstraint, create_engine,
    func, inspect, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine, make_url

DEFAULT_DATABASE_URL = "postgresql+psycopg://jobradar:jobradar@localhost:5433/jobradar"

# Pipeline states. Open/closed is tracked separately in closed_at (see docs):
# a job can be APPLIED *and* closed, and we must not lose the APPLIED fact.
JOB_STATUSES = (
    "DISCOVERED",  # new canonical posting, awaiting filters
    "FILTERED",    # rejected by hard filters
    "MATCHED",     # scored by the matcher
    "NOTIFIED",    # sent to the user
    "APPLIED",     # user applied (from Gmail sync or manual)
    "REJECTED",    # user/company rejected
    "REPOSTED",    # same role re-listed after the original closed
    "DUPLICATE",   # same posting seen via another URL/ID while original is open
)

metadata = MetaData()

TS = DateTime(timezone=True)

jobs = Table(
    "jobs",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("source", Text, nullable=False),
    Column("board", Text, nullable=False),  # e.g. Greenhouse board token
    Column("source_job_id", Text, nullable=False),
    Column("company", Text, nullable=False),
    Column("company_key", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("location", Text),
    Column("url", Text, nullable=False),
    Column("canonical_url", Text, nullable=False),
    Column("description", Text, nullable=False),
    Column("description_hash", Text, nullable=False),
    Column("fingerprint", Text, nullable=False),
    Column("posted_at", TS),
    Column("first_seen_at", TS, nullable=False, server_default=func.now()),
    Column("last_seen_at", TS, nullable=False, server_default=func.now()),
    Column("content_changed_at", TS),
    Column("closed_at", TS),
    Column("status", Text, nullable=False, server_default="DISCOVERED"),
    Column("duplicate_of_id", BigInteger, ForeignKey("jobs.id")),
    Column("dedup_reason", Text),
    Column("match_score", Numeric(5, 2)),
    # The assessment behind match_score. jobs <-> job_matches reference each other,
    # so this key is added after both tables exist (use_alter).
    Column("match_id", BigInteger, ForeignKey("job_matches.id", use_alter=True, name="jobs_match_id_fkey")),
    Column("notified_at", TS),
    Column("applied_at", TS),
    Column("filtered_at", TS),     # set once hard filters have looked at the job
    Column("filter_reason", Text),  # why it was rejected (status FILTERED)
    Column("raw", JSONB, nullable=False),
    # Level-1 dedup + idempotency: the database, not application code,
    # guarantees one row per source posting even under retries/races.
    UniqueConstraint("source", "source_job_id", name="uq_jobs_source_job"),
    CheckConstraint(
        "status IN (" + ",".join(f"'{s}'" for s in JOB_STATUSES) + ")",
        name="ck_jobs_status",
    ),
    CheckConstraint(
        "(duplicate_of_id IS NULL) = (dedup_reason IS NULL)", name="ck_jobs_dedup_link"
    ),
    # Indexes match the queries we actually run:
    Index("ix_jobs_canonical_url", "canonical_url"),            # level-2 lookup
    Index("ix_jobs_fingerprint", "fingerprint"),                # level-3 lookup
    Index("ix_jobs_open_by_board", "source", "board",           # close detection
          postgresql_where=text("closed_at IS NULL")),
    Index("ix_jobs_status_first_seen", "status", "first_seen_at"),  # work queues
)

companies = Table(
    "companies",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("name", Text, nullable=False),
    # Not a DB CHECK on purpose: the set of collectors grows with the code,
    # and adding one shouldn't need a schema migration. Validated on sync.
    Column("collector", Text, nullable=False),
    Column("board", Text, nullable=False),
    Column("priority", Integer, nullable=False, server_default="2"),
    Column("enabled", Boolean, nullable=False, server_default=text("true")),
    Column("created_at", TS, nullable=False, server_default=func.now()),
    Column("updated_at", TS, nullable=False, server_default=func.now()),
    # One registry row per board; also what jobs.(source, board) refers to.
    UniqueConstraint("collector", "board", name="uq_companies_collector_board"),
    CheckConstraint("priority BETWEEN 1 AND 3", name="ck_companies_priority"),
    CheckConstraint("board = lower(board) AND board <> ''", name="ck_companies_board_normalized"),
)

# One row per extracted version; the newest is the active profile.
candidate_profiles = Table(
    "candidate_profiles",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("created_at", TS, nullable=False, server_default=func.now()),
    Column("resume_sha256", Text, nullable=False),  # of the extracted text
    Column("prompt_version", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("profile", JSONB, nullable=False),        # a CandidateProfile
)

# One AI assessment per (job, matching context). The context hash covers the
# profile version, preferences, prompt version and model: if none of those
# changed, the stored answer is reused instead of paying for a new call.
job_matches = Table(
    "job_matches",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("job_id", BigInteger, ForeignKey("jobs.id"), nullable=False),
    Column("profile_id", BigInteger, ForeignKey("candidate_profiles.id"), nullable=False),
    Column("context_hash", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("prompt_version", Text, nullable=False),
    Column("assessment", JSONB, nullable=False),  # a MatchAssessment, exactly as the model returned it
    Column("score", Numeric(4, 1), nullable=False),  # computed by our code, 0-100
    Column("created_at", TS, nullable=False, server_default=func.now()),
    UniqueConstraint("job_id", "context_hash", name="uq_job_matches_job_context"),
)

# Outbox: a row is written before anything is sent, so a job is announced at
# most once per channel (UNIQUE), whatever retries or crashes happen.
notifications = Table(
    "notifications",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("job_id", BigInteger, ForeignKey("jobs.id"), nullable=False),
    Column("channel", Text, nullable=False),          # "telegram"
    Column("kind", Text, nullable=False),             # immediate | digest
    # pending -> sending -> sent; or back to pending (retry), failed, cancelled (job closed).
    # "sending" that never finishes = delivery unknown; not resent automatically.
    Column("status", Text, nullable=False, server_default="pending"),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("last_error", Text),
    Column("message_id", BigInteger),                 # Telegram's id, to edit the buttons later
    Column("created_at", TS, nullable=False, server_default=func.now()),
    Column("sent_at", TS),
    UniqueConstraint("job_id", "channel", name="uq_notifications_job_channel"),
    CheckConstraint("kind IN ('immediate','digest')", name="ck_notifications_kind"),
    CheckConstraint("status IN ('pending','sending','sent','failed','cancelled')",
                    name="ck_notifications_status"),
    Index("ix_notifications_status_kind", "status", "kind"),
)

# What you did about a job. Future training data for a personal ranking model.
feedback = Table(
    "feedback",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("job_id", BigInteger, ForeignKey("jobs.id"), nullable=False),
    Column("kind", Text, nullable=False),
    Column("source", Text, nullable=False),           # "telegram", later "gmail"
    Column("created_at", TS, nullable=False, server_default=func.now()),
    UniqueConstraint("job_id", "kind", name="uq_feedback_job_kind"),  # pressing twice is a no-op
    CheckConstraint("kind IN ('APPLIED','IGNORED','INTERVIEW','REJECTED','OFFER')", name="ck_feedback_kind"),
)

# Small values that must survive restarts, e.g. the last processed Telegram update.
app_state = Table(
    "app_state",
    metadata,
    Column("key", Text, primary_key=True),
    Column("value", Text, nullable=False),
)

# Current review choice, separate from the immutable fact of having applied.
job_reviews = Table(
    "job_reviews", metadata,
    Column("job_id", BigInteger, ForeignKey("jobs.id"), primary_key=True),
    Column("choice", Text, nullable=False),
    Column("updated_at", TS, nullable=False, server_default=func.now()),
    CheckConstraint("choice IN ('QUEUED','IGNORED','SPONSORSHIP','APPLIED')", name="ck_job_reviews_choice"),
)

# Queue summaries are saved with the triggering Telegram update. A restart
# cannot lose an unsent response; uncertain deliveries are never auto-retried.
review_responses = Table(
    "review_responses", metadata,
    Column("update_id", BigInteger, primary_key=True),
    Column("part", Integer, primary_key=True),
    Column("body", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="pending"),
    Column("message_id", BigInteger),
    CheckConstraint("status IN ('pending','sending','sent','failed')", name="ck_review_responses_status"),
)

# Applications you made, whatever told us about them. Linked to jobs by
# company + similar title, so every listing of that role is suppressed.
applications = Table(
    "applications",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("source", Text, nullable=False),       # gmail | notion | manual | telegram
    Column("external_id", Text, nullable=False),  # Gmail message id, Notion page id, job id
    Column("company", Text, nullable=False),
    Column("company_key", Text, nullable=False),
    Column("role", Text),                         # unknown when the email didn't say
    Column("kind", Text, nullable=False),         # APPLIED | REJECTED | INTERVIEW | OFFER
    Column("occurred_at", TS),
    Column("job_id", BigInteger, ForeignKey("jobs.id")),  # best matching job, once found
    Column("created_at", TS, nullable=False, server_default=func.now()),
    UniqueConstraint("source", "external_id", name="uq_applications_source_external"),
    CheckConstraint("kind IN ('APPLIED','REJECTED','INTERVIEW','OFFER')", name="ck_applications_kind"),
    Index("ix_applications_company_key", "company_key"),
)

# Every email the inbox sync has already read, application or not, so the AI is
# never asked about the same email twice.
processed_emails = Table(
    "processed_emails",
    metadata,
    Column("message_id", Text, primary_key=True),  # Gmail's X-GM-MSGID
    Column("verdict", Text, nullable=False),        # what it was: application_received, other, ...
    Column("processed_at", TS, nullable=False, server_default=func.now()),
)

collector_runs = Table(
    "collector_runs",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("source", Text, nullable=False),
    Column("board", Text, nullable=False),
    Column("started_at", TS, nullable=False, server_default=func.now()),
    Column("finished_at", TS),
    Column("ok", Boolean),
    Column("fetched", Integer),
    Column("new", Integer),
    Column("seen", Integer),
    Column("changed", Integer),
    Column("duplicates", Integer),
    Column("closed", Integer),
    Column("reopened", Integer),
    Column("error", Text),
    Index("ix_collector_runs_board_started", "source", "board", "started_at"),
)


def make_engine(url: str | None = None) -> Engine:
    url = url or os.environ.get("JOBRADAR_DATABASE_URL", DEFAULT_DATABASE_URL)
    # Neon supplies a standard PostgreSQL URL; explicitly select our psycopg 3 driver.
    parsed = make_url(url)
    if parsed.drivername in {"postgres", "postgresql"}:
        parsed = parsed.set(drivername="postgresql+psycopg")
    return create_engine(parsed, pool_pre_ping=True, connect_args={"connect_timeout": 30})


def migrate(engine: Engine) -> None:
    """Create or upgrade the schema to the latest migration. Safe to re-run."""
    config = Config()
    config.set_main_option("script_location", "jobradar:migrations")
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        tables = inspect(conn).get_table_names()
        if "jobs" in tables and "alembic_version" not in tables:
            # Created by `init-db` before migrations existed: it already has the
            # baseline schema, so record that instead of re-creating tables.
            command.stamp(config, "0001")
        command.upgrade(config, "head")

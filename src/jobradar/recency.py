"""Strict publication window shared by filtering, scoring, and delivery."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, true

from jobradar.db import jobs


def publication_reason(posted_at, max_age_days, now=None):
    if max_age_days is None:
        return None
    if posted_at is None:
        return "posting date: unavailable"
    now = now or datetime.now(UTC)
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=UTC)
    if posted_at > now:
        return "posting date: in the future"
    if posted_at < now - timedelta(days=max_age_days):
        return f"posting date: older than {max_age_days} days"
    return None


def recent_jobs(max_age_days, now=None):
    if max_age_days is None:
        return true()
    now = now or datetime.now(UTC)
    # Explicit null handling keeps NOT recent_jobs() correct while exposing
    # ordinary range predicates to the publication-date index.
    return and_(jobs.c.posted_at.is_not(None),
                jobs.c.posted_at >= now - timedelta(days=max_age_days), jobs.c.posted_at <= now)

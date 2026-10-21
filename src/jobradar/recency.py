"""Freshness window shared by filtering, scoring, and delivery.

Two clocks: the employer's publication date (posted_at) and when JobRadar
first saw the posting (first_seen_at). A job is fresh when

  - the employer published it within max_age_days (the strict rule), or
  - with discovery_days set: JobRadar first saw it within max_age_days, the
    employer published it within discovery_days, and it was not found by the
    board's initial scan (discovered_in_backfill). Aggregators list long-open
    new-grad roles as "new" when they surface; this catches the ones a board
    newly lists, without flooding on a newly added board's whole backlog.

Undated postings are never fresh (no evidence of age).
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, false, or_, true

from jobradar.db import jobs


def _aware(value):
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


def publication_reason(posted_at, max_age_days, now=None, *, first_seen_at=None, backfill=False,
                       discovery_days=None):
    if max_age_days is None:
        return None
    if posted_at is None:
        return "posting date: unavailable"
    now = now or datetime.now(UTC)
    posted_at, first_seen_at = _aware(posted_at), _aware(first_seen_at)
    if posted_at > now:
        return "posting date: in the future"
    if posted_at < now - timedelta(days=max_age_days):
        if (discovery_days and first_seen_at is not None and not backfill
                and first_seen_at >= now - timedelta(days=max_age_days)
                and posted_at >= now - timedelta(days=discovery_days)):
            return None
        return f"posting date: older than {max_age_days} days"
    return None


def discovered_recently(max_age_days, discovery_days, now=None):
    """SQL: fresh by the discovery rule (see module docstring)."""
    if max_age_days is None or not discovery_days:
        return false()
    now = now or datetime.now(UTC)
    return and_(jobs.c.first_seen_at >= now - timedelta(days=max_age_days),
                jobs.c.discovered_in_backfill.is_(False),
                jobs.c.posted_at.is_not(None),
                jobs.c.posted_at >= now - timedelta(days=discovery_days))


def recent_jobs(max_age_days, now=None, discovery_days=None):
    if max_age_days is None:
        return true()
    now = now or datetime.now(UTC)
    # Explicit null handling keeps NOT recent_jobs() correct while exposing
    # ordinary range predicates to the publication-date index.
    strict = and_(jobs.c.posted_at.is_not(None),
                  jobs.c.posted_at >= now - timedelta(days=max_age_days), jobs.c.posted_at <= now)
    if not discovery_days:
        return strict
    return or_(strict, and_(jobs.c.posted_at <= now, discovered_recently(max_age_days, discovery_days, now)))

"""Human-readable dates without confusing discovery or updates with publication."""

from datetime import UTC, datetime

from jobradar.collectors.base import parse_iso_datetime


def posting_age(job, now=None):
    now = now or datetime.now(UTC)
    timestamp = getattr(job, "posted_at", None)
    label = "Posted"
    # Updates may be displayed in history, but never qualify as publication.
    raw = getattr(job, "raw", None) or {}
    if getattr(job, "source", None) == "greenhouse":
        published = parse_iso_datetime(raw.get("first_published"))
        updated = parse_iso_datetime(raw.get("updated_at"))
        if published:
            timestamp = published
        elif updated:
            timestamp, label = updated, "Updated"
    if timestamp is None:
        timestamp, label = getattr(job, "first_seen_at", None), "First seen"
    if timestamp is None:
        return "Posting date unavailable"
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    seconds = max(0, (now - timestamp).total_seconds())
    if seconds < 60:
        return f"{label} just now"
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            count = int(seconds // size)
            return f"{label} {count} {unit}{'s' if count != 1 else ''} ago"

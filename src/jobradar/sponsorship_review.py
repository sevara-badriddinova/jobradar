"""Visa sponsorship, three ways.

- explicit support in the posting  -> the normal AI pipeline
- explicit refusal                 -> rejected by the filters
- neither (unknown)                -> held here for the user: no AI call, no alert

Held jobs stay FILTERED with UNKNOWN_REASON, which keeps them out of matching
and notifications by construction. The queue is keyed by the job fingerprint
(company, title, location): a repost of the same role updates its entry, and a
role the user dismissed stays dismissed. "Keep" is the user's explicit
override: the role then goes through the normal pipeline (AI score, alerts).
"""

from __future__ import annotations

from sqlalchemy import and_, func, select, update
from sqlalchemy.dialects.postgresql import distinct_on, insert
from sqlalchemy.engine import Connection

from jobradar.db import jobs, sponsorship_reviews

UNKNOWN_REASON = "visa: sponsorship not stated (held for review)"
STATES = ("pending", "kept", "dismissed", "resolved")


def hold(conn: Connection, job_ids: list[int]) -> int:
    """Add or refresh queue entries for these held jobs. Idempotent."""
    if not job_ids:
        return 0
    # One row per fingerprint (the newest job): ON CONFLICT DO UPDATE fails if
    # one statement proposes the same key twice (e.g. a repost and its
    # reopened original held in the same batch).
    rows = (select(jobs.c.fingerprint, jobs.c.id, jobs.c.company, jobs.c.title, jobs.c.location,
                   jobs.c.url, jobs.c.posted_at).where(jobs.c.id.in_(job_ids))
            .ext(distinct_on(jobs.c.fingerprint)).order_by(jobs.c.fingerprint, jobs.c.id.desc()))
    stmt = insert(sponsorship_reviews).from_select(
        ["fingerprint", "job_id", "company", "title", "location", "url", "posted_at"], rows)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_sponsorship_reviews_fingerprint",
        set_={"job_id": stmt.excluded.job_id, "url": stmt.excluded.url, "title": stmt.excluded.title,
              "location": stmt.excluded.location,
              "posted_at": func.coalesce(stmt.excluded.posted_at, sponsorship_reviews.c.posted_at),
              "last_seen_at": func.now(),
              # A role that left the queue (e.g. the posting later stated
              # support and then dropped it) comes back; user decisions stand.
              "state": func.coalesce(func.nullif(sponsorship_reviews.c.state, "resolved"), "pending"),
              "resolution": None})
    return conn.execute(stmt).rowcount


def resolve(conn: Connection, job_ids: list[int], resolution: str) -> int:
    """Pending entries whose job got a definite answer (support, refusal, another rejection)."""
    if not job_ids:
        return 0
    return conn.execute(update(sponsorship_reviews).where(
        sponsorship_reviews.c.job_id.in_(job_ids), sponsorship_reviews.c.state == "pending")
        .values(state="resolved", resolution=resolution[:200], decided_at=func.now())).rowcount


def release_held(conn: Connection) -> list[int]:
    """require_explicit_sponsorship was turned off: every pending entry's open,
    still-held job goes back to the filters (and from there to AI scoring).

    Entries are resolved, not deleted; if the setting is turned on again, a
    re-held role reopens its entry and earlier keep/dismiss decisions stand.
    Cheap: it reads only pending entries, so it is a no-op once released."""
    job_ids = conn.execute(update(sponsorship_reviews).where(sponsorship_reviews.c.state == "pending")
        .values(state="resolved", resolution="released: explicit sponsorship no longer required",
                decided_at=func.now())
        .returning(sponsorship_reviews.c.job_id)).scalars().all()
    job_ids = sorted({i for i in job_ids if i is not None})
    released = []
    for start in range(0, len(job_ids), 500):
        released += conn.execute(update(jobs).where(
            jobs.c.id.in_(job_ids[start:start + 500]), jobs.c.status == "FILTERED",
            jobs.c.filter_reason == UNKNOWN_REASON, jobs.c.closed_at.is_(None))
            .values(filtered_at=None).returning(jobs.c.id)).scalars().all()
    return sorted(released)


def kept_fingerprints(conn: Connection, fingerprints) -> set[str]:
    fingerprints = [f for f in set(fingerprints) if f]
    if not fingerprints:
        return set()
    return set(conn.execute(select(sponsorship_reviews.c.fingerprint).where(
        sponsorship_reviews.c.fingerprint.in_(fingerprints),
        sponsorship_reviews.c.state == "kept")).scalars())


def pending(conn: Connection, limit: int = 50):
    """Open, undecided roles, most recently seen first."""
    return conn.execute(
        select(sponsorship_reviews.c.id, sponsorship_reviews.c.company, sponsorship_reviews.c.title,
               sponsorship_reviews.c.location, sponsorship_reviews.c.url, sponsorship_reviews.c.posted_at,
               sponsorship_reviews.c.last_seen_at, sponsorship_reviews.c.job_id)
        .join(jobs, jobs.c.id == sponsorship_reviews.c.job_id)
        .where(sponsorship_reviews.c.state == "pending", jobs.c.closed_at.is_(None),
               jobs.c.status == "FILTERED", jobs.c.filter_reason == UNKNOWN_REASON)
        .order_by(sponsorship_reviews.c.last_seen_at.desc(), sponsorship_reviews.c.id.desc())
        .limit(limit)).all()


def decide(conn: Connection, review_id: int, state: str) -> int | None:
    """Keep or dismiss an entry. Keeping releases its job into the AI pipeline
    (returns the job id to queue for matching); dismissing changes nothing else."""
    if state not in ("kept", "dismissed"):
        raise ValueError("state must be 'kept' or 'dismissed'")
    row = conn.execute(update(sponsorship_reviews).where(sponsorship_reviews.c.id == review_id)
                       .values(state=state, decided_at=func.now())
                       .returning(sponsorship_reviews.c.job_id)).first()
    if row is None:
        raise LookupError(f"no sponsorship review #{review_id}")
    if state == "kept" and row.job_id is not None:
        released = conn.execute(update(jobs).where(and_(
            jobs.c.id == row.job_id, jobs.c.status == "FILTERED", jobs.c.filter_reason == UNKNOWN_REASON,
            jobs.c.closed_at.is_(None))).values(status="DISCOVERED", filter_reason=None,
                                                filtered_at=func.now())).rowcount
        return row.job_id if released else None
    return None


def counts(conn: Connection) -> dict[str, int]:
    rows = conn.execute(select(sponsorship_reviews.c.state, func.count())
                        .group_by(sponsorship_reviews.c.state)).all()
    return {state: n for state, n in rows}

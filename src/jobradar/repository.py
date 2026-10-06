"""Idempotent ingestion of a board snapshot, with deterministic dedup.

One call == one transaction == one complete snapshot of one board. If the
process crashes half-way, the transaction rolls back and the next poll simply
redoes the work: re-running is always safe (idempotent).
"""

import logging
from dataclasses import dataclass, field

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection

from jobradar import dedup
from jobradar.db import jobs as jobs_t
from jobradar.models import Job

log = logging.getLogger(__name__)


@dataclass
class IngestResult:
    new_ids: list[int] = field(default_factory=list)  # genuinely new canonical jobs
    seen: int = 0          # already known (level-1 hit)
    changed: int = 0       # known, but title/location/description changed
    duplicates: int = 0    # stored, linked to an original via level 2/3
    closed: int = 0        # disappeared from the board
    reopened: int = 0      # was closed, reappeared with the same ID

    @property
    def new(self) -> int:
        return len(self.new_ids)


def _derived(job: Job) -> dict:
    return {
        "company_key": dedup.company_key(job.company),
        "canonical_url": dedup.canonical_url(job.url),
        "description_hash": dedup.description_hash(job.description),
        "fingerprint": dedup.fingerprint(job.company, job.title, job.location),
    }


def _find_original(conn: Connection, column, value: str):
    """Oldest canonical (non-duplicate) job with this key, if any."""
    return conn.execute(
        select(jobs_t.c.id, jobs_t.c.closed_at)
        .where(column == value, jobs_t.c.duplicate_of_id.is_(None))
        .order_by(jobs_t.c.id)
        .limit(1)
    ).first()


def ingest_snapshot(
    conn: Connection, *, source: str, board: str, jobs: list[Job], complete: bool = True
) -> IngestResult:
    result = IngestResult()

    # Serialize concurrent ingests of the *same* board (e.g. an overlapping
    # scheduler tick) so level-2/3 lookups can't race. Different boards still
    # run fully in parallel. Released automatically at commit/rollback.
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                 {"k": f"{source}:{board}"})

    snapshot: dict[str, Job] = {}
    for job in jobs:
        if job.source != source:
            raise ValueError(f"job from {job.source!r} in a {source!r} snapshot")
        snapshot[job.source_job_id] = job  # collapse in-batch repeats
    ids = list(snapshot)

    # --- Level 1: one round-trip to find everything we already know --------
    existing = {r.source_job_id: r for r in conn.execute(
        select(jobs_t.c.id, jobs_t.c.source_job_id, jobs_t.c.title, jobs_t.c.location,
               jobs_t.c.url, jobs_t.c.description_hash, jobs_t.c.closed_at)
        .where(jobs_t.c.source == source, jobs_t.c.source_job_id.in_(ids))
    )}
    result.seen = len(existing)
    result.reopened = sum(1 for r in existing.values() if r.closed_at is not None)
    conn.execute(
        update(jobs_t)
        .where(jobs_t.c.id.in_([r.id for r in existing.values()]))
        .values(last_seen_at=func.now(), closed_at=None)
    )
    # Change detection: compare cheap hashes/fields, rewrite only what changed.
    for sid, row in existing.items():
        job = snapshot[sid]
        d = _derived(job)
        if (row.description_hash, row.title, row.location, row.url) != (
            d["description_hash"], job.title, job.location, job.url
        ):
            conn.execute(
                update(jobs_t).where(jobs_t.c.id == row.id).values(
                    title=job.title, location=job.location, url=job.url,
                    description=job.description, posted_at=job.posted_at,
                    raw=job.raw, content_changed_at=func.now(), **d,
                )
            )
            result.changed += 1

    # --- Unknown IDs: levels 2 and 3, then insert ---------------------------
    for sid, job in snapshot.items():
        if sid in existing:
            continue
        d = _derived(job)
        status, original_id, reason = "DISCOVERED", None, None

        if (hit := _find_original(conn, jobs_t.c.canonical_url, d["canonical_url"])):
            status, original_id, reason = "DUPLICATE", hit.id, "canonical_url"
        elif (hit := _find_original(conn, jobs_t.c.fingerprint, d["fingerprint"])):
            # Same company+title+location under a new ID. If the original is
            # gone it's a repost; if it's still open, it's a parallel listing.
            status = "REPOSTED" if hit.closed_at is not None else "DUPLICATE"
            original_id, reason = hit.id, "fingerprint"

        new_id = conn.execute(
            insert(jobs_t)
            .values(
                source=source, board=board, source_job_id=sid, company=job.company,
                title=job.title, location=job.location, url=job.url,
                description=job.description, posted_at=job.posted_at, raw=job.raw,
                status=status, duplicate_of_id=original_id, dedup_reason=reason, **d,
            )
            .on_conflict_do_nothing(constraint="uq_jobs_source_job")
            .returning(jobs_t.c.id)
        ).scalar_one_or_none()

        if new_id is None:
            result.seen += 1  # another writer inserted it first; still idempotent
        elif original_id is not None:
            result.duplicates += 1
        else:
            result.new_ids.append(new_id)

    # --- Close detection ----------------------------------------------------
    if complete and ids:
        result.closed = conn.execute(
            update(jobs_t)
            .where(jobs_t.c.source == source, jobs_t.c.board == board,
                   jobs_t.c.closed_at.is_(None), jobs_t.c.source_job_id.not_in(ids))
            .values(closed_at=func.now())
        ).rowcount
    elif complete:
        # An empty response is far more often an API hiccup than a company
        # closing every job at once. Don't mass-close on it.
        log.warning("%s/%s returned 0 jobs; skipping close detection", source, board)

    return result

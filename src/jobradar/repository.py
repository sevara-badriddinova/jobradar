"""Idempotent ingestion of a board snapshot, with deterministic dedup.

One call == one transaction == one complete snapshot of one board. If the
process crashes half-way, the transaction rolls back and the next poll simply
redoes the work: re-running is always safe (idempotent).
"""

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import Text, case, column, exists, func, literal, or_, select, text, update, values
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.engine import Connection

from jobradar import dedup
from jobradar.db import jobs as jobs_t
from jobradar.models import Job

log = logging.getLogger(__name__)

# JSON parameters are expanded by PostgreSQL while parsing/planning. A row
# limit alone allowed hundreds of large descriptions to exhaust the VM's
# database cgroup before work_mem could limit anything.
COMPARISON_MAX_ROWS = 25
COMPARISON_MAX_BYTES = 512 * 1024


def _comparison_batches(snapshot):
    batch, size = [], 0
    for sid, job in snapshot.items():
        payload_size = len(json.dumps(job.raw, ensure_ascii=True).encode("utf-8"))
        if batch and (len(batch) >= COMPARISON_MAX_ROWS or size + payload_size > COMPARISON_MAX_BYTES):
            yield batch
            batch, size = [], 0
        batch.append((sid, job.raw))
        size += payload_size
    if batch:
        yield batch


@dataclass
class IngestResult:
    new_ids: list[int] = field(default_factory=list)  # genuinely new canonical jobs
    seen: int = 0          # already known (level-1 hit)
    changed: int = 0       # known, but title/location/description changed
    duplicates: int = 0    # stored, linked to an original via level 2/3
    closed: int = 0        # disappeared from the board
    reopened: int = 0      # was closed, reappeared with the same ID
    skipped_old: int = 0   # never stored: published before the ingestion window

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
    """Prefer a live canonical/reposted listing to a closed ancestor."""
    return conn.execute(
        select(jobs_t.c.id, jobs_t.c.closed_at, jobs_t.c.source, jobs_t.c.board, jobs_t.c.source_job_id)
        .where(column == value, jobs_t.c.status != "DUPLICATE",
               or_(jobs_t.c.duplicate_of_id.is_(None), jobs_t.c.dedup_reason == "fingerprint"))
        .order_by(jobs_t.c.closed_at.is_not(None), jobs_t.c.id)
        .limit(1)
    ).first()


# Raw fields that change on every poll without the posting changing. Workday's
# "Posted N Days Ago" label ticks daily; comparing it rewrote and re-filtered
# every undated Workday posting each day.
VOLATILE_RAW_KEYS = {"workday": ("postedOn",)}


def _stable(raw, source: str):
    for key in VOLATILE_RAW_KEYS.get(source, ()):
        raw = raw.op("-")(literal(key, Text))
    return raw


SNAPSHOT_DIGEST_VERSION = 1


def snapshot_digest(source: str, jobs: list[Job], *, complete: bool, max_age_days: int | None) -> str | None:
    """Order-independent digest of everything ingestion would store for a board.

    Two polls with the same digest would make ingestion write nothing (same IDs,
    same stored fields, volatile raw keys excluded, same window), so an identical
    poll can skip ingestion. None for empty snapshots, which are never trusted.
    """
    if not jobs:
        return None
    volatile = VOLATILE_RAW_KEYS.get(source, ())
    entries = sorted(json.dumps([
        job.source_job_id, job.company, job.title, job.location, job.url, job.description,
        job.posted_at.isoformat() if job.posted_at else None,
        {k: v for k, v in job.raw.items() if k not in volatile},
    ], sort_keys=True, ensure_ascii=True, default=str) for job in jobs)
    h = hashlib.sha256(json.dumps([SNAPSHOT_DIGEST_VERSION, source, complete, max_age_days]).encode())
    for entry in entries:
        h.update(b"\n")
        h.update(entry.encode())
    return h.hexdigest()


def _is_initial_scan(conn: Connection, source: str, board: str, existing: dict) -> bool:
    """No open postings stored for this board yet (index-only, ix_jobs_open_by_board)."""
    return not existing and not conn.execute(select(exists().where(
        jobs_t.c.source == source, jobs_t.c.board == board, jobs_t.c.closed_at.is_(None)))).scalar()


def ingest_snapshot(
    conn: Connection, *, source: str, board: str, jobs: list[Job], complete: bool = True,
    skip_new: Callable[[Job], bool] | None = None,
) -> IngestResult:
    """Upsert one board snapshot.

    skip_new(job) -> True marks a posting as certainly older than the ingestion
    window; it is stored only if the database already knows it. Close detection
    still uses every ID in the snapshot, so skipping never closes a job.
    """
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
    ids = list(snapshot)  # close detection: everything the board still lists

    if skip_new is not None:
        old = [sid for sid, job in snapshot.items() if skip_new(job)]
        known = set()
        for start in range(0, len(old), 1000):  # unique index (source, source_job_id)
            known.update(conn.execute(select(jobs_t.c.source_job_id).where(
                jobs_t.c.source == source, jobs_t.c.source_job_id.in_(old[start:start + 1000]))).scalars())
        for sid in old:
            if sid not in known:
                del snapshot[sid]
                result.skipped_old += 1

    # --- Level 1: one round-trip to find everything we already know --------
    existing = {}
    # Compare JSONB inside Postgres. Downloading every stored HTML/JSON payload
    # just to compare it with the API response burns the network allowance.
    # Bounded VALUES batches also stay below the driver's parameter limit.
    for batch in _comparison_batches(snapshot):
        incoming = values(column("sid", Text), column("payload", JSONB), name="incoming").data(
            batch)
        rows = conn.execute(select(
            jobs_t.c.id, jobs_t.c.source_job_id, jobs_t.c.title, jobs_t.c.location,
            jobs_t.c.url, jobs_t.c.description_hash, jobs_t.c.closed_at, jobs_t.c.posted_at,
            _stable(jobs_t.c.raw, source).is_distinct_from(_stable(incoming.c.payload, source))
            .label("raw_changed"), jobs_t.c.company
        ).join(incoming, jobs_t.c.source_job_id == incoming.c.sid).where(jobs_t.c.source == source))
        existing.update((r.source_job_id, r) for r in rows)
    result.seen = len(existing)
    # Only reopened rows need a write. Rewriting every listed row on every poll
    # (to refresh last_seen_at) created a new tuple, WAL, and index entries for
    # each one: the dominant disk cost on the VM. An open job's last sighting is
    # its board's last successful poll (board_state.last_success_at).
    reopened = [r.id for r in existing.values() if r.closed_at is not None]
    result.reopened = len(reopened)
    if reopened:
        conn.execute(update(jobs_t).where(jobs_t.c.id.in_(reopened))
                     .values(last_seen_at=func.now(), closed_at=None))
    # Change detection: compare cheap hashes/fields, rewrite only what changed.
    for sid, row in existing.items():
        job = snapshot[sid]
        d = _derived(job)
        changed = row.raw_changed or (row.description_hash, row.title, row.location, row.url, row.company) != (
            d["description_hash"], job.title, job.location, job.url, job.company)
        if changed or row.closed_at is not None:
            # Preserve terminal user decisions and previous deliveries. Reopened
            # or changed unacted-on jobs must pass filters and matching again.
            resettable = jobs_t.c.status.in_(["DISCOVERED", "MATCHED", "FILTERED", "REPOSTED"])
            conn.execute(update(jobs_t).where(jobs_t.c.id == row.id).values(
                status=case((resettable, "DISCOVERED"), else_=jobs_t.c.status),
                filtered_at=case((or_(resettable, jobs_t.c.status == "NOTIFIED"), None),
                                 else_=jobs_t.c.filtered_at),
                filter_reason=case((resettable, None), else_=jobs_t.c.filter_reason),
                match_score=case((resettable, None), else_=jobs_t.c.match_score),
                match_id=case((resettable, None), else_=jobs_t.c.match_id)))
        if changed:
            conn.execute(
                update(jobs_t).where(jobs_t.c.id == row.id).values(
                    company=job.company, title=job.title, location=job.location, url=job.url,
                    description=job.description, posted_at=job.posted_at,
                    raw=job.raw, content_changed_at=func.now(), **d,
                )
            )
            result.changed += 1
        elif row.posted_at != job.posted_at:
            # Sources can add/correct publication dates without changing the text.
            conn.execute(update(jobs_t).where(jobs_t.c.id == row.id)
                         .values(posted_at=job.posted_at, raw=job.raw,
                                 filtered_at=case((jobs_t.c.status.in_([
                                     "DISCOVERED", "REPOSTED", "FILTERED", "MATCHED", "NOTIFIED"
                                 ]), None), else_=jobs_t.c.filtered_at)))

    # --- Unknown IDs: levels 2 and 3, then insert ---------------------------
    # A board with no open postings yet is on its initial scan: everything it
    # lists is backlog, not news (see recency.py). Index-only check
    # (ix_jobs_open_by_board); a board whose postings all closed counts as
    # initial too, which only errs towards fewer alerts.
    backfill = _is_initial_scan(conn, source, board, existing)
    listed = set(ids)
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
            # An original on this same board that this complete snapshot no
            # longer lists is being closed right now (close detection runs
            # below): that is a repost too, not a duplicate of a live posting.
            leaving = (complete and hit.source == source and hit.board == board
                       and hit.source_job_id not in listed)
            status = "REPOSTED" if hit.closed_at is not None or leaving else "DUPLICATE"
            original_id, reason = hit.id, "fingerprint"

        new_id = conn.execute(
            insert(jobs_t)
            .values(
                source=source, board=board, source_job_id=sid, company=job.company,
                title=job.title, location=job.location, url=job.url,
                description=job.description, posted_at=job.posted_at, raw=job.raw,
                status=status, duplicate_of_id=original_id, dedup_reason=reason,
                **({"discovered_in_backfill": True} if backfill else {}), **d,
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

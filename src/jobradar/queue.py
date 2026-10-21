"""PostgreSQL-backed durable queues: filter/match tasks and the board schedule.

Claiming uses one statement (`FOR UPDATE SKIP LOCKED` + `UPDATE ... RETURNING`),
so concurrent workers never receive the same row and never block each other.
A lease that is not completed or extended in time expires and the row becomes
claimable again (crash recovery). Every handler is idempotent, so a task that
runs twice after a crash produces the same database state.

`generation` protects against lost updates: if a job is re-enqueued while its
task is leased (e.g. the posting changed again), completion re-arms the task
instead of deleting it.
"""

from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection

log = logging.getLogger(__name__)

KINDS = ("filter", "match")


@dataclass(frozen=True)
class Task:
    id: int
    kind: str
    job_id: int
    generation: int
    attempts: int
    max_attempts: int
    origin_at: datetime


# --- work_queue --------------------------------------------------------------------

def enqueue(conn: Connection, kind: str, job_ids, *, origin_at: datetime | None = None,
            priority: int = 100, delay_seconds: float = 0, max_attempts: int = 5) -> int:
    """Idempotently (re)queue tasks. Returns how many rows were inserted or re-armed.

    An existing ready/leased task keeps its place (earliest availability and
    origin win) and its generation is bumped so an in-flight run repeats. A dead
    task is revived with a fresh attempt budget: enqueueing is an explicit
    request that the job be processed again.
    """
    if kind not in KINDS:
        raise ValueError(kind)
    ids = sorted({int(i) for i in job_ids})
    if not ids:
        return 0
    return conn.execute(text("""
        INSERT INTO work_queue (kind, job_id, priority, available_at, origin_at, max_attempts)
        SELECT :kind, j, :priority, now() + make_interval(secs => :delay), coalesce(:origin, now()),
               :max_attempts
        FROM unnest(CAST(:ids AS bigint[])) AS j
        ON CONFLICT (kind, job_id) DO UPDATE SET
            generation = work_queue.generation + 1,
            priority = least(work_queue.priority, excluded.priority),
            available_at = CASE WHEN work_queue.state = 'dead' THEN excluded.available_at
                                ELSE least(work_queue.available_at, excluded.available_at) END,
            origin_at = least(work_queue.origin_at, excluded.origin_at),
            attempts = CASE WHEN work_queue.state = 'dead' THEN 0 ELSE work_queue.attempts END,
            state = CASE WHEN work_queue.state = 'dead' THEN 'ready' ELSE work_queue.state END,
            last_error = CASE WHEN work_queue.state = 'dead' THEN NULL ELSE work_queue.last_error END,
            max_attempts = greatest(work_queue.max_attempts, excluded.max_attempts)
    """), {"kind": kind, "ids": ids, "priority": priority, "delay": float(delay_seconds),
           "origin": origin_at, "max_attempts": int(max_attempts)}).rowcount


def enqueue_pending_filters(conn: Connection, source: str, board: str) -> int:
    """Queue filter tasks for this board's new/changed (unfiltered) open jobs.

    Mirrors the legacy incremental selection in `run_filters`: rows whose
    `filtered_at` was cleared by ingestion. Unchanged rows are never queued.
    """
    return conn.execute(text("""
        INSERT INTO work_queue (kind, job_id)
        SELECT 'filter', j.id FROM jobs j
        WHERE j.source = :source AND j.board = :board AND j.closed_at IS NULL
          AND j.filtered_at IS NULL
          AND j.status IN ('DISCOVERED','REPOSTED','FILTERED','MATCHED','NOTIFIED')
        ON CONFLICT (kind, job_id) DO UPDATE SET generation = work_queue.generation + 1,
            state = CASE WHEN work_queue.state = 'dead' THEN 'ready' ELSE work_queue.state END,
            attempts = CASE WHEN work_queue.state = 'dead' THEN 0 ELSE work_queue.attempts END,
            available_at = CASE WHEN work_queue.state = 'dead' THEN now() ELSE work_queue.available_at END
    """), {"source": source, "board": board}).rowcount


def claim(conn: Connection, kind: str, owner: str, limit: int, lease_seconds: float = 120) -> list[Task]:
    """Atomically lease up to `limit` available tasks (expired leases included)."""
    rows = conn.execute(text("""
        WITH picked AS (
            SELECT id FROM work_queue
            WHERE kind = :kind AND state <> 'dead' AND available_at <= now()
              AND (state = 'ready' OR lease_until < now())
            ORDER BY priority,
                     -- matching is the paid bottleneck: newest discoveries first
                     -- (as the legacy matcher did); filtering is FIFO
                     CASE WHEN kind = 'match' THEN -extract(epoch FROM origin_at) END,
                     available_at, id
            LIMIT :limit
            FOR UPDATE SKIP LOCKED
        )
        UPDATE work_queue w SET state = 'leased', lease_owner = :owner,
            lease_until = now() + make_interval(secs => :lease), attempts = w.attempts + 1
        FROM picked WHERE w.id = picked.id
        RETURNING w.id, w.kind, w.job_id, w.generation, w.attempts, w.max_attempts, w.origin_at
    """), {"kind": kind, "owner": owner, "limit": int(limit), "lease": float(lease_seconds)}).all()
    return sorted((Task(*r) for r in rows), key=lambda t: t.id)


def extend(conn: Connection, tasks: list[Task], owner: str, lease_seconds: float = 120) -> int:
    if not tasks:
        return 0
    return conn.execute(text("""
        UPDATE work_queue SET lease_until = now() + make_interval(secs => :lease)
        WHERE id = ANY(CAST(:ids AS bigint[])) AND lease_owner = :owner AND state = 'leased'
    """), {"ids": [t.id for t in tasks], "owner": owner, "lease": float(lease_seconds)}).rowcount


def complete(conn: Connection, task: Task, owner: str) -> bool:
    """Delete a finished task. If it was re-enqueued meanwhile, re-arm it instead.

    Returns False when the lease was lost (another worker took over after expiry);
    the handler's writes are idempotent so that is harmless.
    """
    deleted = conn.execute(text("""
        DELETE FROM work_queue WHERE id = :id AND lease_owner = :owner AND generation = :generation
    """), {"id": task.id, "owner": owner, "generation": task.generation}).rowcount
    if deleted:
        return True
    return bool(conn.execute(text("""
        UPDATE work_queue SET state = 'ready', lease_owner = NULL, lease_until = NULL, attempts = 0
        WHERE id = :id AND lease_owner = :owner
    """), {"id": task.id, "owner": owner}).rowcount)


def retry_delay(attempts: int, base: float = 30.0, cap: float = 3600.0) -> float:
    """Exponential backoff with jitter for a task that failed `attempts` times."""
    return min(cap, base * 2 ** max(0, attempts - 1)) * random.uniform(0.8, 1.2)


def fail(conn: Connection, task: Task, owner: str, error: str, *, delay: float | None = None) -> str:
    """Release a failed task for a later retry, or bury it after max attempts."""
    state = conn.execute(text("""
        UPDATE work_queue SET
            state = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'ready' END,
            available_at = now() + make_interval(secs => :delay),
            lease_owner = NULL, lease_until = NULL, last_error = left(:error, 1000)
        WHERE id = :id AND lease_owner = :owner
        RETURNING state
    """), {"id": task.id, "owner": owner, "error": error,
           "delay": retry_delay(task.attempts) if delay is None else float(delay)}).scalar_one_or_none()
    return state or "lost"


def release(conn: Connection, tasks: list[Task], owner: str, *, delay: float = 0) -> int:
    """Give tasks back without charging an attempt (shutdown, backpressure)."""
    if not tasks:
        return 0
    return conn.execute(text("""
        UPDATE work_queue SET state = 'ready', lease_owner = NULL, lease_until = NULL,
            attempts = greatest(0, attempts - 1), available_at = now() + make_interval(secs => :delay)
        WHERE id = ANY(CAST(:ids AS bigint[])) AND lease_owner = :owner
    """), {"ids": [t.id for t in tasks], "owner": owner, "delay": float(delay)}).rowcount


def release_owner(conn: Connection, owner: str) -> int:
    """Return everything an exiting worker still holds, including board leases."""
    tasks = conn.execute(text("""
        UPDATE work_queue SET state = 'ready', lease_owner = NULL, lease_until = NULL,
            attempts = greatest(0, attempts - 1)
        WHERE lease_owner = :owner AND state = 'leased'
    """), {"owner": owner}).rowcount
    boards = conn.execute(text("""
        UPDATE board_state SET lease_owner = NULL, lease_until = NULL WHERE lease_owner = :owner
    """), {"owner": owner}).rowcount
    return tasks + boards


def bury_exhausted(conn: Connection) -> int:
    """Expired leases that already used every attempt (e.g. a poison job that
    crashes the worker each time) become dead instead of looping forever."""
    return conn.execute(text("""
        UPDATE work_queue SET state = 'dead', lease_owner = NULL, lease_until = NULL,
            last_error = coalesce(last_error, 'lease expired after final attempt')
        WHERE state = 'leased' AND lease_until < now() AND attempts >= max_attempts
    """)).rowcount


# --- board schedule ----------------------------------------------------------------

@dataclass(frozen=True)
class Board:
    company_id: int
    name: str
    collector: str
    board: str
    priority: int
    last_bytes: int | None
    consecutive_failures: int
    snapshot_hash: str | None = None   # only while recent enough to trust (see claim_boards)


def seed_boards(conn: Connection) -> int:
    """Create schedule rows for registry boards; never-polled boards come first,
    then the least recently attempted (continuing the legacy rotation)."""
    return conn.execute(text("""
        INSERT INTO board_state (company_id, next_due_at)
        SELECT c.id, coalesce(r.last_attempt, '-infinity'::timestamptz)
        FROM companies c
        LEFT JOIN (SELECT source, board, max(started_at) AS last_attempt
                   FROM collector_runs GROUP BY source, board) r
          ON r.source = c.collector AND r.board = c.board
        WHERE NOT EXISTS (SELECT 1 FROM board_state b WHERE b.company_id = c.id)
        ON CONFLICT (company_id) DO NOTHING
    """)).rowcount


# A stored snapshot digest lets an identical poll skip ingestion entirely. Every
# board is still fully re-ingested at least this often (self-healing).
SNAPSHOT_TRUST_SECONDS = 24 * 3600


def claim_boards(conn: Connection, collector: str, owner: str, limit: int,
                 lease_seconds: float = 900) -> list[Board]:
    """Lease the `limit` most overdue enabled boards of one provider."""
    if limit <= 0:
        return []
    rows = conn.execute(text("""
        WITH picked AS (
            SELECT b.company_id FROM board_state b JOIN companies c ON c.id = b.company_id
            WHERE c.enabled AND c.collector = :collector AND b.next_due_at <= now()
              AND (b.lease_until IS NULL OR b.lease_until < now())
            ORDER BY b.next_due_at, b.company_id
            LIMIT :limit
            FOR UPDATE OF b SKIP LOCKED
        )
        UPDATE board_state b SET lease_owner = :owner, last_started_at = now(),
            lease_until = now() + make_interval(secs => :lease)
        FROM picked, companies c
        WHERE b.company_id = picked.company_id AND c.id = b.company_id
        RETURNING b.company_id, c.name, c.collector, c.board, c.priority, b.last_bytes,
                  b.consecutive_failures,
                  CASE WHEN b.snapshot_at > now() - make_interval(secs => :trust) THEN b.snapshot_hash END
    """), {"collector": collector, "owner": owner, "limit": int(limit),
           "lease": float(lease_seconds), "trust": float(SNAPSHOT_TRUST_SECONDS)}).all()
    return [Board(*r) for r in rows]


def due_providers(conn: Connection) -> dict[str, int]:
    """Due board count per provider (the coverage metric)."""
    return {p: n for p, (n, _) in due_overview(conn).items()}


def due_overview(conn: Connection) -> dict[str, tuple[int, float]]:
    """Per provider: due board count and its most overdue `next_due_at` (epoch
    seconds; never-polled '-infinity' boards sort first as -1e18).

    Claiming orders providers by the oldest due board, not by backlog size:
    ordering by count let the providers with the biggest backlog take every
    slot forever while small providers (Lever, Personio, ...) never ran.
    """
    rows = conn.execute(text("""
        SELECT c.collector, count(*),
               min(CASE WHEN isfinite(b.next_due_at) THEN extract(epoch FROM b.next_due_at) ELSE -1e18 END)
        FROM board_state b JOIN companies c ON c.id = b.company_id
        WHERE c.enabled AND b.next_due_at <= now() AND (b.lease_until IS NULL OR b.lease_until < now())
        GROUP BY c.collector
    """)).all()
    return {r[0]: (r[1], float(r[2])) for r in rows}


_STATUS = re.compile(r"\bHTTP (\d{3})\b")
PERMANENT_STATUS = frozenset({400, 401, 403, 404, 405, 410, 422})


def failure_kind(error: str | None) -> str:
    """permanent (a bad/removed board address), or transient (network/server/format)."""
    match = _STATUS.search(error or "")
    if match and int(match.group(1)) in PERMANENT_STATUS:
        return "permanent"
    return "transient"


def next_poll_delay(*, ok: bool, priority: int, failures: int, error: str | None,
                    base_interval: float = 1800.0) -> float:
    """Seconds until the board is due again.

    Healthy boards revisit every `base_interval * priority` (fairness comes
    from always claiming the most overdue first). Failures back off
    exponentially: transient ones from 10 minutes up to 6 hours, permanent
    4xx ones from 6 hours up to 7 days, so dead addresses stop costing
    capacity without being forgotten.
    """
    jitter = random.uniform(0.9, 1.1)
    if ok:
        return base_interval * max(1, priority) * jitter
    if failure_kind(error) == "permanent":
        return min(7 * 86400, 6 * 3600 * 2 ** max(0, failures - 1)) * jitter
    return min(6 * 3600, 600 * 2 ** max(0, failures - 1)) * jitter


def finish_board(conn: Connection, board: Board, owner: str, *, ok: bool, error: str | None,
                 fetched: int | None, nbytes: int | None, base_interval: float = 1800.0,
                 retry_in: float | None = None, snapshot_hash: str | None = None) -> bool:
    """Record a poll outcome and schedule the next one.

    `retry_in` reschedules without counting a failure (backpressure/shutdown).
    `snapshot_hash` records the digest of a snapshot that was just fully
    ingested (and committed); unchanged polls pass None and keep the old one.
    Returns False if the lease was lost to another worker.
    """
    if retry_in is not None:
        return bool(conn.execute(text("""
            UPDATE board_state SET lease_owner = NULL, lease_until = NULL,
                next_due_at = now() + make_interval(secs => :delay)
            WHERE company_id = :id AND lease_owner = :owner
        """), {"id": board.company_id, "owner": owner, "delay": float(retry_in)}).rowcount)
    failures = 0 if ok else board.consecutive_failures + 1
    delay = next_poll_delay(ok=ok, priority=board.priority, failures=failures, error=error,
                            base_interval=base_interval)
    return bool(conn.execute(text("""
        UPDATE board_state SET lease_owner = NULL, lease_until = NULL, last_finished_at = now(),
            next_due_at = now() + make_interval(secs => :delay),
            consecutive_failures = :failures,
            last_success_at = CASE WHEN :ok THEN now() ELSE last_success_at END,
            last_error = CASE WHEN :ok THEN NULL ELSE left(:error, 1000) END,
            last_bytes = coalesce(:nbytes, last_bytes), last_fetched = coalesce(:fetched, last_fetched),
            snapshot_hash = coalesce(CAST(:snap AS text), snapshot_hash),
            snapshot_at = CASE WHEN CAST(:snap AS text) IS NOT NULL THEN now() ELSE snapshot_at END
        WHERE company_id = :id AND lease_owner = :owner
    """), {"id": board.company_id, "owner": owner, "delay": delay, "failures": failures, "ok": ok,
           "error": error or "", "nbytes": nbytes, "fetched": fetched,
           "snap": snapshot_hash if ok else None}).rowcount)


def extend_board(conn: Connection, board: Board, owner: str, lease_seconds: float = 900) -> bool:
    return bool(conn.execute(text("""
        UPDATE board_state SET lease_until = now() + make_interval(secs => :lease)
        WHERE company_id = :id AND lease_owner = :owner
    """), {"id": board.company_id, "owner": owner, "lease": float(lease_seconds)}).rowcount)


def requeue_boards(conn: Connection, names: list[str] | None = None) -> int:
    """Make boards due now (explicit operator request)."""
    if names:
        return conn.execute(text("""
            UPDATE board_state b SET next_due_at = now() FROM companies c
            WHERE c.id = b.company_id AND (lower(c.name) = ANY(:n) OR lower(c.board) = ANY(:n))
        """), {"n": [n.lower() for n in names]}).rowcount
    return conn.execute(text("UPDATE board_state SET next_due_at = now()")).rowcount


__all__ = ["Task", "Board", "enqueue", "enqueue_pending_filters", "claim", "extend", "complete",
           "fail", "release", "release_owner", "bury_exhausted", "retry_delay", "seed_boards",
           "claim_boards", "due_providers", "due_overview", "failure_kind", "next_poll_delay", "finish_board",
           "extend_board", "requeue_boards"]

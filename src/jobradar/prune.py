"""Prune the stored text of jobs that can never qualify again.

About 99.8% of stored jobs are filtered or duplicates, and their posting text
and raw payload (about 3.5 KB a row) made the jobs table 6.4 GB on a 1 GB VM:
backups outgrew their time limit and collection waits on the disk.

A pruned row keeps everything the pipeline uses to recognise it (IDs, title,
company, location, URL, dates, status, fingerprint, description_hash) plus
raw_digest, the MD5 of its payload as ingestion compares it. Ingestion compares
a pruned row by that digest, and restores the full content whenever the
posting changes, reopens or gets a new date (repository.py).

Only rows no posting-date rule can accept are pruned: filtered or duplicate,
first seen more than `min_age_days` ago, and undated or published more than
`posted_before_days` ago. Open postings of collectors that reuse stored detail
payloads (Workday, Personio, Eightfold, Oracle) keep their payload: those
collectors would otherwise fetch every detail page again.

Batches are short transactions that skip locked rows, so it can run beside
the workers. Space is reusable at once but the file shrinks only with
VACUUM FULL jobs (exclusive lock; run it in a maintenance window).

`rewrite_prune` does both in one pass for a maintenance window (workers
stopped): a single ALTER TABLE rewrites the table once, emptying prunable
rows on the way, and rebuilds the indexes. On a disk with little random I/O
this replaces ~12 random index writes per pruned row with sequential writes,
and the file shrinks at once. One transaction: a failure or timeout leaves
the table exactly as it was.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

from sqlalchemy import Text, case, cast, func, literal, select, text, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine

from jobradar.collectors import COLLECTORS
from jobradar.db import jobs
from jobradar.repository import VOLATILE_RAW_KEYS, raw_digest
from jobradar.sponsorship_review import UNKNOWN_REASON

log = logging.getLogger(__name__)

DETAIL_SOURCES = tuple(sorted(name for name, cls in COLLECTORS.items()
                              if getattr(cls, "reuses_known_details", None)))


@dataclass
class PruneResult:
    pruned: int = 0
    batches: int = 0
    last_id: int = 0


def prunable(*, min_age_days: int, posted_before_days: int):
    """Rows that can never pass a posting-date rule again and are safe to empty."""
    seen_before = func.now() - func.make_interval(0, 0, 0, min_age_days)
    posted_before = func.now() - func.make_interval(0, 0, 0, posted_before_days)
    return (jobs.c.raw_digest.is_(None)
            & jobs.c.status.in_(["FILTERED", "DUPLICATE"])
            & jobs.c.filter_reason.is_distinct_from(UNKNOWN_REASON)
            & (jobs.c.first_seen_at < seen_before)
            & (jobs.c.posted_at.is_(None) | (jobs.c.posted_at < posted_before))
            & (jobs.c.closed_at.is_not(None) | jobs.c.source.not_in(DETAIL_SOURCES)))


def _digest_by_source():
    """raw_digest(raw, source) for every row, choosing the source's volatile keys."""
    whens = [(jobs.c.source == source, raw_digest(jobs.c.raw, source)) for source in VOLATILE_RAW_KEYS]
    plain = raw_digest(jobs.c.raw, "")
    return case(*whens, else_=plain) if whens else plain


def prune_content(engine: Engine, *, min_age_days: int = 3, posted_before_days: int = 14,
                  batch: int = 2000, limit: int | None = None, pause: float = 0.0,
                  start_after: int = 0) -> PruneResult:
    if min_age_days < 3 or posted_before_days < 14:
        raise ValueError("pruning younger rows could empty jobs the freshness rules still accept")
    result = PruneResult(last_id=start_after)
    where = prunable(min_age_days=min_age_days, posted_before_days=posted_before_days)
    while limit is None or result.pruned < limit:
        size = batch if limit is None else min(batch, limit - result.pruned)
        with engine.begin() as conn:
            # Walk the primary key: each batch reads forward from the last row seen.
            ids = conn.execute(select(jobs.c.id).where(where, jobs.c.id > result.last_id)
                               .order_by(jobs.c.id).limit(size).with_for_update(skip_locked=True)).scalars().all()
            if not ids:
                break
            conn.execute(update(jobs).where(jobs.c.id.in_(ids)).values(
                raw_digest=_digest_by_source(), raw=cast(literal("{}"), JSONB), description=""))
        result.pruned += len(ids)
        result.batches += 1
        result.last_id = ids[-1]
        if result.batches % 25 == 0:
            log.info("prune: %d rows emptied (through id %d)", result.pruned, result.last_id)
        if pause:
            time.sleep(pause)
    log.info("prune: done, %d rows emptied in %d batches (through id %d)",
             result.pruned, result.batches, result.last_id)
    return result


@dataclass
class RewriteResult:
    before_bytes: int
    after_bytes: int
    pruned_estimate: int
    seconds: float


def _sql(expr) -> str:
    """Inline SQL for an expression on `jobs` (ALTER TABLE takes no bind parameters)."""
    sql = str(expr.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    # USING expressions name the row's own columns, unqualified.
    names = "|".join(sorted((re.escape(c.name) for c in jobs.columns), key=len, reverse=True))
    return re.sub(rf"\bjobs\.({names})\b", r"\1", sql)


def rewrite_statement(*, min_age_days: int = 3, posted_before_days: int = 14) -> str:
    if min_age_days < 3 or posted_before_days < 14:
        raise ValueError("pruning younger rows could empty jobs the freshness rules still accept")
    cond = _sql(prunable(min_age_days=min_age_days, posted_before_days=posted_before_days))
    digest = _sql(_digest_by_source())
    # A USING clause other than the column itself forces one rewrite of the
    # table for all three columns together; every expression sees the old row.
    return (f"ALTER TABLE jobs "
            f"ALTER COLUMN raw TYPE jsonb USING CASE WHEN {cond} THEN '{{}}'::jsonb ELSE raw END, "
            f"ALTER COLUMN description TYPE text USING CASE WHEN {cond} THEN '' ELSE description END, "
            f"ALTER COLUMN raw_digest TYPE text USING CASE WHEN {cond} THEN {digest} ELSE raw_digest END")


def rewrite_prune(engine: Engine, *, min_age_days: int = 3, posted_before_days: int = 14,
                  statement_timeout_s: int = 5 * 3600, lock_timeout_s: int = 300,
                  maintenance_work_mem: str = "64MB") -> RewriteResult:
    """Prune and compact `jobs` in one exclusive rewrite. Stop the workers first."""
    statement = rewrite_statement(min_age_days=min_age_days, posted_before_days=posted_before_days)
    size = text("SELECT pg_total_relation_size('jobs')")
    with engine.connect() as conn:
        before = conn.execute(size).scalar_one()
    started = time.monotonic()
    log.info("prune rewrite: started (jobs %.0f MB)", before / 2**20)
    with engine.begin() as conn:
        conn.execute(text(f"SET LOCAL statement_timeout = '{int(statement_timeout_s)}s'"))
        conn.execute(text(f"SET LOCAL lock_timeout = '{int(lock_timeout_s)}s'"))
        conn.execute(text("SELECT set_config('maintenance_work_mem', :m, true)"), {"m": maintenance_work_mem})
        conn.execute(text("SET LOCAL max_parallel_maintenance_workers = 0"))  # 256 MB container
        conn.execute(text(statement))
    seconds = time.monotonic() - started
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("VACUUM (ANALYZE) jobs"))  # fresh statistics and visibility map
        after = conn.execute(size).scalar_one()
        pruned = conn.execute(text("""
            SELECT round(c.reltuples * (1 - coalesce(s.null_frac, 1)))::bigint
            FROM pg_class c LEFT JOIN pg_stats s
              ON s.tablename = 'jobs' AND s.attname = 'raw_digest' AND s.schemaname = current_schema()
            WHERE c.oid = 'jobs'::regclass""")).scalar_one() or 0
    log.info("prune rewrite: done in %.0f s, jobs %.0f MB -> %.0f MB, ~%d rows pruned",
             seconds, before / 2**20, after / 2**20, pruned)
    return RewriteResult(before, after, int(pruned), seconds)

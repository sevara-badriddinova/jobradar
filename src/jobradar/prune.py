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
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from sqlalchemy import Text, case, cast, func, literal, select, update
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

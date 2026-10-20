"""Run collectors with failure isolation and record every run."""

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Engine

from jobradar.collectors.base import JobCollector
from jobradar.db import collector_runs
from jobradar.db import jobs as jobs_t
from jobradar.repository import IngestResult, ingest_snapshot

log = logging.getLogger(__name__)


@dataclass
class RunReport:
    source: str
    board: str
    ok: bool
    fetched: int = 0
    result: IngestResult | None = None
    error: str | None = None
    seconds: float = 0.0
    queued: int = 0          # filter tasks queued by the workers' after_ingest hook


def _known_details(engine: Engine, collector: JobCollector) -> dict[str, dict]:
    """Stored detail payloads (raw[collector.reuses_known_details]) of this board's
    open postings, so the collector can skip re-fetching them."""
    key = collector.reuses_known_details
    with engine.connect() as conn:
        rows = conn.execute(select(jobs_t.c.source_job_id, jobs_t.c.raw[key]).where(
            jobs_t.c.source == collector.source, jobs_t.c.board == collector.board,
            jobs_t.c.closed_at.is_(None), jobs_t.c.raw.has_key(key)))
        return {sid: {key: value} for sid, value in rows if isinstance(value, dict)}


def run_collector(engine: Engine, collector: JobCollector, *, after_ingest=None,
                  record_unchanged: bool = True, ingest_max_age_days: int | None = None) -> RunReport:
    """Collect and ingest one board. `after_ingest(conn, report)` runs inside the
    ingest transaction (the workers queue filter tasks there, atomically).

    record_unchanged=False (continuous workers) writes a collector_runs row only
    for failures and polls that changed something; board_state keeps the latest
    outcome of every board, so the history table does not grow by ~200k
    no-op rows a day.
    """
    source, board = collector.source, collector.board
    started = time.monotonic()
    run_id = None
    if record_unchanged:
        with engine.begin() as conn:
            run_id = conn.execute(
                insert(collector_runs).values(source=source, board=board).returning(collector_runs.c.id)
            ).scalar_one()

    report = RunReport(source=source, board=board, ok=False)
    skip_new = None
    older = getattr(collector, "known_older_than", None)  # duck-typed collectors: never skip
    if ingest_max_age_days is not None and older is not None:
        cutoff = datetime.now(UTC) - timedelta(days=ingest_max_age_days)
        skip_new = lambda job: older(job, cutoff)  # noqa: E731
    try:
        if getattr(collector, "reuses_known_details", None):
            collector.known = _known_details(engine, collector)
        jobs = collector.collect()
        report.fetched = len(jobs)
        with engine.begin() as conn:
            report.result = ingest_snapshot(conn, source=source, board=board, jobs=jobs,
                                            complete=collector.complete_snapshot, skip_new=skip_new)
            if after_ingest is not None:
                after_ingest(conn, report)
        report.ok = True
    except Exception as exc:  # boundary: one board's failure must not stop the rest
        report.error = f"{type(exc).__name__}: {exc}"
        log.error("%s/%s failed: %s", source, board, report.error)
    report.seconds = time.monotonic() - started

    r = report.result or IngestResult()
    values = dict(finished_at=func.clock_timestamp(),
                  ok=report.ok, fetched=report.fetched, new=r.new, seen=r.seen,
                  changed=r.changed, duplicates=r.duplicates, closed=r.closed,
                  reopened=r.reopened, error=report.error)
    try:
        with engine.begin() as conn:
            if run_id is not None:
                conn.execute(update(collector_runs).where(collector_runs.c.id == run_id).values(**values))
            elif not report.ok or r.new or r.changed or r.duplicates or r.closed or r.reopened:
                conn.execute(insert(collector_runs).values(
                    source=source, board=board,
                    started_at=func.clock_timestamp() - func.make_interval(0, 0, 0, 0, 0, 0, report.seconds),
                    **values))
    except Exception:
        log.exception("could not record run %s", run_id)
    return report


def run_all(engine: Engine, collectors: list[JobCollector], *,
            ingest_max_age_days: int | None = None) -> list[RunReport]:
    return [run_collector(engine, c, ingest_max_age_days=ingest_max_age_days) for c in collectors]

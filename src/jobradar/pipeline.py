"""Run collectors with failure isolation and record every run."""

import logging
import time
from dataclasses import dataclass

from sqlalchemy import func, insert, update
from sqlalchemy.engine import Engine

from jobradar.collectors.base import JobCollector
from jobradar.db import collector_runs
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


def run_collector(engine: Engine, collector: JobCollector) -> RunReport:
    source, board = collector.source, collector.board
    started = time.monotonic()
    with engine.begin() as conn:
        run_id = conn.execute(
            insert(collector_runs).values(source=source, board=board).returning(collector_runs.c.id)
        ).scalar_one()

    report = RunReport(source=source, board=board, ok=False)
    try:
        jobs = collector.collect()
        report.fetched = len(jobs)
        with engine.begin() as conn:
            report.result = ingest_snapshot(conn, source=source, board=board, jobs=jobs,
                                            complete=collector.complete_snapshot)
        report.ok = True
    except Exception as exc:  # boundary: one board's failure must not stop the rest
        report.error = f"{type(exc).__name__}: {exc}"
        log.error("%s/%s failed: %s", source, board, report.error)
    report.seconds = time.monotonic() - started

    r = report.result or IngestResult()
    try:
        with engine.begin() as conn:
            conn.execute(
                update(collector_runs).where(collector_runs.c.id == run_id).values(
                    finished_at=func.clock_timestamp(),
                    ok=report.ok, fetched=report.fetched, new=r.new, seen=r.seen,
                    changed=r.changed, duplicates=r.duplicates, closed=r.closed,
                    reopened=r.reopened, error=report.error,
                )
            )
    except Exception:
        log.exception("could not record run %s", run_id)
    return report


def run_all(engine: Engine, collectors: list[JobCollector]) -> list[RunReport]:
    return [run_collector(engine, c) for c in collectors]

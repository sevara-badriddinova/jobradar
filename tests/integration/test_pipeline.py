from sqlalchemy import select

from jobradar.collectors.base import CollectorError, JobCollector
from jobradar.db import collector_runs
from jobradar.pipeline import run_all, run_collector
from tests.factories import make_job


class Fake(JobCollector):
    source = "greenhouse"

    def __init__(self, board, jobs=None, error=None, complete=True):
        self._board, self._jobs, self._error = board, jobs or [], error
        self.complete_snapshot = complete

    @property
    def board(self):
        return self._board

    def normalize(self, raw):
        raise NotImplementedError

    def collect(self):
        if self._error:
            raise self._error
        return self._jobs


def runs(engine):
    with engine.connect() as conn:
        return conn.execute(select(collector_runs).order_by(collector_runs.c.id)).all()


def test_successful_run_is_recorded_with_counts(engine):
    run_collector(engine, Fake("a", [make_job("1"), make_job("2", title="X")]))
    rep = run_collector(engine, Fake("a", [make_job("1")]))
    assert rep.ok and rep.fetched == 1 and rep.seconds >= 0
    first, second = runs(engine)
    assert (first.ok, first.fetched, first.new, first.seen, first.closed) == (True, 2, 2, 0, 0)
    assert (second.ok, second.fetched, second.new, second.seen, second.closed) == (True, 1, 0, 1, 1)
    assert second.finished_at >= second.started_at and second.error is None


def test_one_failing_collector_does_not_stop_the_others(engine):
    reports = run_all(engine, [
        Fake("a", [make_job("1")]),
        Fake("broken", error=CollectorError("HTTP 503")),
        Fake("boom", error=RuntimeError("bug in collector")),
        Fake("c", [make_job("2", company="Cee", title="SWE")]),
    ])
    assert [r.ok for r in reports] == [True, False, False, True]
    assert sum(r.result.new for r in reports if r.ok) == 2
    recorded = runs(engine)
    assert [(r.board, r.ok) for r in recorded] == [("a", True), ("broken", False), ("boom", False), ("c", True)]
    assert recorded[1].error == "CollectorError: HTTP 503"
    assert recorded[2].error == "RuntimeError: bug in collector"
    assert recorded[1].finished_at is not None


def test_failed_collect_leaves_existing_jobs_open(engine):
    run_collector(engine, Fake("a", [make_job("1")]))
    run_collector(engine, Fake("a", error=CollectorError("timeout")))
    from tests.integration.helpers import row
    assert row(engine, "1").closed_at is None


def test_ingest_failure_is_recorded_and_isolated(engine):
    bad = Fake("bad", [make_job("1", source="lever")])  # source mismatch -> ValueError in ingest
    reports = run_all(engine, [bad, Fake("good", [make_job("2", title="Y")])])
    assert [r.ok for r in reports] == [False, True]
    assert "ValueError" in runs(engine)[0].error


def test_incomplete_collector_does_not_close(engine):
    run_collector(engine, Fake("a", [make_job("1"), make_job("2", title="X")]))
    rep = run_collector(engine, Fake("a", [make_job("1")], complete=False))
    assert rep.result.closed == 0

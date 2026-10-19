"""Continuous workers: durable queues, crash recovery, dedup, retries, end to end."""

import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, insert, select, text, update

from jobradar import matcher
from jobradar import queue as q
from jobradar.collectors.base import CollectorError
from jobradar.db import (board_state, collector_runs, companies, job_matches, jobs as jobs_t,
                         notifications, work_queue)
from jobradar.llm import LLMError
from jobradar.settings import FilterSettings, NotificationSettings, Preferences, Settings
from jobradar.telegram import Telegram
from jobradar.worker import Worker, WorkerConfig
from tests.factories import FakeLLM, assessment, make_job
from tests.fake_telegram import FakeTelegram
from tests.integration.helpers import ingest, row
from tests.integration.test_matcher import add_profile

NOW = datetime.now(UTC)
SETTINGS = Settings(
    filters=FilterSettings(max_posting_age_days=3),
    preferences=Preferences(interests=["backend"]),
    notifications=NotificationSettings(immediate_min_score=70, digest_min_score=60),
)


def fresh(sid, **kw):
    kw.setdefault("posted_at", NOW - timedelta(hours=2))
    kw.setdefault("title", f"Backend Engineer {sid}")
    return make_job(sid, **kw)


def tasks(engine, kind=None):
    with engine.connect() as conn:
        query = select(work_queue).order_by(work_queue.c.id)
        if kind:
            query = query.where(work_queue.c.kind == kind)
        return conn.execute(query).all()


def job_ids(engine, n, board="acme"):
    ingest(engine, [fresh(f"j{i}") for i in range(n)], board=board)
    with engine.connect() as conn:
        return conn.execute(select(jobs_t.c.id).where(jobs_t.c.board == board)
                            .order_by(jobs_t.c.id)).scalars().all()


def expire(engine, task_id):
    with engine.begin() as conn:
        conn.execute(update(work_queue).where(work_queue.c.id == task_id)
                     .values(lease_until=func.now() - timedelta(seconds=1)))


# --- queue primitives ------------------------------------------------------------------

class TestQueue:
    def test_enqueue_is_idempotent(self, engine):
        ids = job_ids(engine, 3)
        with engine.begin() as conn:
            q.enqueue(conn, "filter", ids)
            q.enqueue(conn, "filter", ids + ids)
        assert len(tasks(engine, "filter")) == 3

    def test_concurrent_claims_never_overlap(self, engine):
        ids = job_ids(engine, 300)
        with engine.begin() as conn:
            q.enqueue(conn, "filter", ids)
        seen, lock, start = [], threading.Lock(), threading.Barrier(8)

        def worker(n):
            start.wait()
            while True:
                with engine.begin() as conn:
                    got = q.claim(conn, "filter", f"w{n}", 7)
                if not got:
                    return
                with lock:
                    seen.extend(t.job_id for t in got)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(seen) == sorted(ids)  # every task exactly once

    def test_expired_lease_is_recovered_by_another_worker(self, engine):
        [job] = job_ids(engine, 1)
        with engine.begin() as conn:
            q.enqueue(conn, "match", [job])
            [first] = q.claim(conn, "match", "crashed", 5, lease_seconds=60)
            assert q.claim(conn, "match", "other", 5) == []   # leased: invisible
        expire(engine, first.id)                               # the worker died
        with engine.begin() as conn:
            [second] = q.claim(conn, "match", "other", 5)
            assert (second.id, second.attempts) == (first.id, 2)
            assert q.complete(conn, first, "crashed") is False  # stale owner cannot finish
            assert q.complete(conn, second, "other") is True
        assert tasks(engine) == []

    def test_requeue_during_processing_runs_again(self, engine):
        [job] = job_ids(engine, 1)
        with engine.begin() as conn:
            q.enqueue(conn, "filter", [job])
            [task] = q.claim(conn, "filter", "w", 5)
            q.enqueue(conn, "filter", [job])                   # the posting changed again
            q.complete(conn, task, "w")
        [left] = tasks(engine)
        assert (left.state, left.attempts, left.generation) == ("ready", 0, 1)

    def test_failures_back_off_then_bury_and_explicit_requeue_revives(self, engine):
        [job] = job_ids(engine, 1)
        with engine.begin() as conn:
            q.enqueue(conn, "match", [job], max_attempts=2)
            [t1] = q.claim(conn, "match", "w", 1)
            assert q.fail(conn, t1, "w", "boom") == "ready"
            assert q.claim(conn, "match", "w", 1) == []        # backing off
            conn.execute(update(work_queue).values(available_at=func.now()))
            [t2] = q.claim(conn, "match", "w", 1)
            assert q.fail(conn, t2, "w", "boom again") == "dead"
            q.enqueue(conn, "match", [job])
        [revived] = tasks(engine)
        assert (revived.state, revived.attempts, revived.last_error) == ("ready", 0, None)

    def test_poison_job_that_kills_workers_is_buried(self, engine):
        [job] = job_ids(engine, 1)
        with engine.begin() as conn:
            q.enqueue(conn, "filter", [job], max_attempts=2)
        for _ in range(2):
            with engine.begin() as conn:
                [t] = q.claim(conn, "filter", "w", 1)
            expire(engine, t.id)
        with engine.begin() as conn:
            assert q.bury_exhausted(conn) == 1
            assert q.claim(conn, "filter", "w", 1) == []

    def test_release_owner_returns_everything_without_charging(self, engine):
        ids = job_ids(engine, 2)
        with engine.begin() as conn:
            q.enqueue(conn, "filter", ids)
            q.claim(conn, "filter", "leaving", 5)
            assert q.release_owner(conn, "leaving") == 2
        assert {(t.state, t.attempts) for t in tasks(engine)} == {("ready", 0)}


# --- board schedule -------------------------------------------------------------------

def add_board(engine, name, collector="greenhouse", priority=1, enabled=True):
    with engine.begin() as conn:
        return conn.execute(insert(companies).values(name=name, collector=collector, board=name,
            priority=priority, enabled=enabled).returning(companies.c.id)).scalar_one()


class TestBoards:
    def test_seed_continues_the_legacy_rotation(self, engine):
        old, never = add_board(engine, "old"), add_board(engine, "never")
        add_board(engine, "off", enabled=False)
        with engine.begin() as conn:
            conn.execute(insert(collector_runs).values(source="greenhouse", board="old", ok=True,
                started_at=func.now() - timedelta(days=1)))
            assert q.seed_boards(conn) == 3 and q.seed_boards(conn) == 0
            got = q.claim_boards(conn, "greenhouse", "w", 5)
        assert [b.company_id for b in got] == [never, old]     # never polled first; disabled skipped

    def test_claims_respect_provider_and_leases(self, engine):
        for i in range(4):
            add_board(engine, f"g{i}")
        add_board(engine, "w0", collector="workday")
        with engine.begin() as conn:
            q.seed_boards(conn)
            assert q.due_providers(conn) == {"greenhouse": 4, "workday": 1}
            assert len(q.claim_boards(conn, "greenhouse", "a", 3)) == 3
            assert len(q.claim_boards(conn, "greenhouse", "b", 3)) == 1   # the rest are leased
            assert q.due_providers(conn) == {"workday": 1}

    @pytest.mark.parametrize("ok,error,low,high", [
        (True, None, 1800 * 2 * 0.9, 1800 * 2 * 1.1),                      # priority 2 interval
        (False, "CollectorError: HTTP 404 from x (not retryable)", 6 * 3600 * 0.9, 6 * 3600 * 1.1),
        (False, "CollectorError: GET x failed after 4 attempts: HTTP 503", 540, 660),
    ])
    def test_outcome_schedules_next_poll(self, engine, ok, error, low, high):
        add_board(engine, "b", priority=2)
        with engine.begin() as conn:
            q.seed_boards(conn)
            [b] = q.claim_boards(conn, "greenhouse", "w", 1)
            assert q.finish_board(conn, b, "w", ok=ok, error=error, fetched=1, nbytes=10)
            state = conn.execute(select(board_state, (board_state.c.next_due_at - func.now()).label("in_"))).one()
        assert low <= state.in_.total_seconds() <= high
        assert state.consecutive_failures == (0 if ok else 1) and state.lease_owner is None

    def test_backpressure_retry_does_not_count_a_failure(self, engine):
        add_board(engine, "b")
        with engine.begin() as conn:
            q.seed_boards(conn)
            [b] = q.claim_boards(conn, "greenhouse", "w", 1)
            q.finish_board(conn, b, "w", ok=False, error=None, fetched=None, nbytes=None, retry_in=60)
            assert q.finish_board(conn, b, "w", ok=True, error=None, fetched=1, nbytes=1) is False  # lease gone
            state = conn.execute(select(board_state)).one()
        assert state.consecutive_failures == 0 and state.last_finished_at is None


# --- the worker, end to end -------------------------------------------------------------

class FakeBoards:
    """collector_factory stand-in: board name -> list of Jobs (or an exception)."""

    def __init__(self):
        self.snapshots, self.calls = {}, []

    def __call__(self, collector, board, company=None, client=None):
        outer = self

        class Collector:
            source, complete_snapshot = collector, True

            def __init__(self):
                self.board = board

            def collect(self):
                outer.calls.append(board)
                value = outer.snapshots[board]
                if isinstance(value, Exception):
                    raise value
                return list(value)
        return Collector()


@pytest.fixture
def boards():
    return FakeBoards()


@pytest.fixture
def llm(monkeypatch):
    def install(*answers):
        fake = FakeLLM(*answers)
        monkeypatch.setattr(matcher.llm, "ask", fake)
        return fake
    return install


@pytest.fixture
def telegram():
    server = FakeTelegram()
    return server, (Telegram(server.token, server.client()), "42")


def make_worker(engine, boards, telegram=None, settings=SETTINGS, **cfg):
    return Worker(engine, WorkerConfig(**cfg), settings_loader=lambda: settings,
                  telegram=telegram, collector_factory=boards)


def poll(worker, engine):
    """Claim and collect every due board once."""
    with engine.begin() as conn:
        q.seed_boards(conn)
    outcomes = []
    while (claimed := worker.claim_boards(10)):
        outcomes += [worker.collect_board(b) for b in claimed]
    return outcomes


def make_due(engine):
    with engine.begin() as conn:
        conn.execute(update(board_state).values(next_due_at=func.now()))


class TestWorkerPipeline:
    def test_new_and_changed_jobs_flow_to_one_alert(self, engine, boards, llm, telegram):
        server, tg = telegram
        add_board(engine, "acme")
        add_profile(engine)
        llm(assessment(role_match=10, technical_match=10, experience_match=10))
        boards.snapshots["acme"] = [fresh("a"), fresh("senior", title="Senior Staff Engineer"),
                                    fresh("old", posted_at=NOW - timedelta(days=5))]
        w = make_worker(engine, boards, tg)

        assert poll(w, engine) == ["ok"]
        assert len(tasks(engine, "filter")) == 3                 # only new jobs were queued
        assert w.filter_once() == 3
        assert row(engine, "senior").status == "FILTERED"
        assert row(engine, "old").filter_reason == "posting date: older than 3 days"
        assert [t.job_id for t in tasks(engine, "match")] == [row(engine, "a").id]
        assert w.match_once() == 1
        assert row(engine, "a").status == "MATCHED"
        assert w.notify_once() == 1 and len(server.sent) == 1
        with engine.connect() as conn:
            note = conn.execute(select(notifications)).one()
        assert note.status == "sent" and note.origin_at is not None and note.sent_at >= note.origin_at

        # An unchanged re-poll queues nothing and never re-alerts.
        make_due(engine)
        assert poll(w, engine) == ["ok"] and tasks(engine) == []
        assert w.notify_once() == 0 and len(server.sent) == 1
        with engine.connect() as conn:  # no-op polls do not grow the history table
            assert conn.execute(select(func.count()).select_from(collector_runs)).scalar_one() == 1

        # A changed posting is re-filtered; a delivered alert is still never repeated.
        boards.snapshots["acme"] = [fresh("a", description="Build backend services in Go.")]
        make_due(engine)
        poll(w, engine)
        assert [t.job_id for t in tasks(engine, "filter")] == [row(engine, "a").id]
        w.filter_once(), w.match_once(), w.notify_once()
        assert len(server.sent) == 1

    def test_sponsorship_requirement_is_preserved(self, engine, boards, llm):
        settings = SETTINGS.model_copy(update={
            "filters": FilterSettings(max_posting_age_days=3, require_explicit_sponsorship=True),
            "preferences": Preferences(interests=["backend"], needs_visa_sponsorship=True)})
        add_board(engine, "acme")
        boards.snapshots["acme"] = [fresh("no", description="We are unable to sponsor visas."),
                                    fresh("yes", description="We sponsor H-1B visas for this role.")]
        w = make_worker(engine, boards, settings=settings)
        poll(w, engine)
        w.filter_once()
        assert row(engine, "no").status == "FILTERED"
        assert row(engine, "yes").status == "DISCOVERED"

    def test_llm_failure_is_retried_with_backoff(self, engine, boards, llm):
        add_board(engine, "acme")
        add_profile(engine)
        fake = llm(LLMError("overloaded"))
        boards.snapshots["acme"] = [fresh("a")]
        w = make_worker(engine, boards)
        poll(w, engine)
        w.filter_once()
        assert w.match_once() == 1
        [task] = tasks(engine, "match")
        assert (task.state, task.attempts, task.last_error) == ("ready", 1, "LLM assessment failed")
        assert task.available_at > datetime.now(UTC) and row(engine, "a").status == "DISCOVERED"
        assert w.match_once() == 0 and len(fake.calls) >= 1      # not retried before the backoff

    def test_crashed_matcher_is_recovered_without_a_second_paid_call(self, engine, boards, llm):
        add_board(engine, "acme")
        add_profile(engine)
        fake = llm(assessment(role_match=9, technical_match=9, experience_match=9))
        boards.snapshots["acme"] = [fresh("a")]
        crashed, survivor = make_worker(engine, boards), make_worker(engine, boards)
        poll(crashed, engine)
        crashed.filter_once()
        # The first worker stored the assessment, then died before marking the job.
        with engine.begin() as conn:
            [task] = q.claim(conn, "match", crashed.owner, 1)
        profile = crashed.profile()
        matcher._assess(engine, row(engine, "a"), profile, SETTINGS.preferences,
                        matcher.context_hash(profile.id, SETTINGS.preferences, row(engine, "a"), profile.profile))
        calls = len(fake.calls)
        expire(engine, task.id)
        assert survivor.match_once() == 1
        assert row(engine, "a").status == "MATCHED" and len(fake.calls) == calls  # served from cache
        assert tasks(engine, "match") == []

    def test_failing_board_backs_off_and_others_continue(self, engine, boards):
        add_board(engine, "dead")
        add_board(engine, "live")
        boards.snapshots["dead"] = CollectorError("HTTP 404 from https://x (not retryable)")
        boards.snapshots["live"] = [fresh("x")]
        w = make_worker(engine, boards)
        assert sorted(poll(w, engine)) == ["failed", "ok"]
        with engine.connect() as conn:
            dead = conn.execute(select(board_state).join(companies).where(companies.c.name == "dead")).one()
            failed_runs = conn.execute(select(func.count()).select_from(collector_runs)
                                       .where(collector_runs.c.ok.is_(False))).scalar_one()
        assert dead.consecutive_failures == 1 and "404" in dead.last_error and failed_runs == 1
        assert (dead.next_due_at - datetime.now(UTC)) > timedelta(hours=5)

    def test_two_workers_filter_a_backlog_exactly_once(self, engine, boards):
        ids = job_ids(engine, 400)
        with engine.begin() as conn:
            q.enqueue(conn, "filter", ids)
        workers = [make_worker(engine, boards, filter_batch=25) for _ in range(3)]
        done = []

        def drain(w):
            while (n := w.filter_once()):
                done.append(n)

        threads = [threading.Thread(target=drain, args=(w,)) for w in workers]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sum(done) == 400 and tasks(engine, "filter") == []
        assert len(tasks(engine, "match")) == 400
        with engine.connect() as conn:
            assert conn.execute(select(func.count()).select_from(jobs_t)
                                .where(jobs_t.c.filtered_at.is_(None))).scalar_one() == 0

    def test_run_loop_starts_processes_and_releases_on_stop(self, engine, boards, llm):
        add_board(engine, "acme")
        add_profile(engine)
        fake = llm(assessment(role_match=10, technical_match=10, experience_match=10))
        boards.snapshots["acme"] = [fresh("a"), fresh("b")]
        w = make_worker(engine, boards, idle_sleep=0.2, maintenance_interval=0.5)
        w.run(duration=4)
        assert row(engine, "a").status == "MATCHED" and row(engine, "b").status == "MATCHED"
        with engine.connect() as conn:
            assert conn.execute(select(func.count()).select_from(work_queue)).scalar_one() == 0
            assert conn.execute(select(func.count()).select_from(board_state)
                                .where(board_state.c.lease_owner.is_not(None))).scalar_one() == 0
            metrics = conn.execute(text("SELECT value FROM app_state WHERE key = 'worker_metrics'")).scalar_one()
        assert '"queues"' in metrics and w.counters["collect_ok"] == 1
        # The startup sweep may race the filter worker and queue a repeat match
        # task; it is a no-op, never a second paid assessment.
        with engine.connect() as conn:
            assert conn.execute(select(func.count()).select_from(job_matches)).scalar_one() == 2
        assert fake.calls


def test_metrics_cli(engine, db_env, capsys):
    from jobradar import cli
    add_board(engine, "acme")
    with engine.begin() as conn:
        q.seed_boards(conn)
    assert cli.main(["metrics"]) == 0
    out = capsys.readouterr().out
    assert '"enabled": 1' in out and '"latency_24h_s"' in out


def test_requeue_cli(engine, db_env, capsys):
    from jobradar import cli
    [job] = job_ids(engine, 1)
    assert cli.main(["requeue", "filter", str(job)]) == 0
    assert [t.job_id for t in tasks(engine, "filter")] == [job]
    assert cli.main(["requeue", "filter"]) == 0                 # all unfiltered: still one task
    assert len(tasks(engine, "filter")) == 1

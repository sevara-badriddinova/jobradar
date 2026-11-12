import threading
import time

import pytest
from sqlalchemy import text

from jobradar.runtime import atomic_json, read_json, status_ok
from jobradar.worker import WorkerFailed
from tests.integration.test_workers import FakeBoards, make_worker


def test_essential_thread_exit_fails_worker_and_releases_cycle_resources(engine, monkeypatch):
    worker = make_worker(engine, FakeBoards(), roles=frozenset({"filter"}), supervisor_interval=.01)
    monkeypatch.setattr(worker, "stage_loop", lambda *args: None)
    with pytest.raises(WorkerFailed, match="filter"):
        worker.run(duration=1)
    assert worker.stop.is_set()


def test_stalled_stage_is_detected_and_leases_are_not_released_while_alive(engine, monkeypatch):
    release = threading.Event()
    worker = make_worker(engine, FakeBoards(), roles=frozenset({"filter"}),
                         supervisor_interval=.01, stall_timeout=.1, keeper_interval=.02,
                         shutdown_timeout=.05)
    monkeypatch.setattr(worker, "filter_once", lambda: release.wait(5))
    try:
        with pytest.raises(WorkerFailed, match="filter"):
            worker.run(duration=1)
        assert worker.background_stuck
    finally:
        release.set()
        for thread in worker._threads:
            thread.join(2)


def test_backup_mode_keeps_notify_alive_and_resumes_when_lease_expires(engine, tmp_path, monkeypatch):
    (tmp_path / "status").mkdir()
    now = time.time()
    atomic_json(tmp_path / "maintenance.json", dict(kind="backup", token="test", started_at=now, expires_at=now+30))
    worker = make_worker(engine, FakeBoards(), telegram=(object(), "chat"),
                         roles=frozenset({"filter", "match", "notify"}), control_dir=str(tmp_path),
                         supervisor_interval=.01, idle_sleep=.01, keeper_interval=.02)
    filtered, delivered = threading.Event(), threading.Event()
    monkeypatch.setattr(worker, "filter_once", lambda: filtered.set() or 0)
    monkeypatch.setattr(worker, "match_once", lambda: 0)
    monkeypatch.setattr(worker, "pending_alerts", lambda: True)
    monkeypatch.setattr(worker, "notify_once", lambda **kwargs: delivered.set() or 0)
    failures = []
    def run():
        try:
            worker.run()
        except Exception as exc:
            failures.append(exc)
    runner = threading.Thread(target=run)
    runner.start()
    try:
        end = time.monotonic()+3
        while not read_json(tmp_path / "status/worker.json").get("quiesced") and time.monotonic()<end:
            time.sleep(.01)
        state = read_json(tmp_path / "status/worker.json")
        assert status_ok(state) and state["quiesced"]
        assert not filtered.is_set()
        worker.wake["notify"].set()
        assert delivered.wait(2), "notification delivery must continue during backup"
        atomic_json(tmp_path / "maintenance.json", dict(kind="backup", token="test", started_at=now-20, expires_at=now-1))
        assert filtered.wait(2), "expired lease must resume heavy work without a controller"
    finally:
        worker.stop.set()
        runner.join(5)
    assert not failures


def test_idle_loops_do_not_trigger_stall_restarts(engine):
    worker = make_worker(engine, FakeBoards(), roles=frozenset({"filter", "match"}),
                         supervisor_interval=.01, idle_sleep=.01, keeper_interval=.02,
                         stall_timeout=.15)
    worker.run(duration=.35)
    assert not worker._fatal


def test_worker_db_statement_timeout_is_enforced(database_url):
    from jobradar.db import make_engine
    from sqlalchemy.exc import OperationalError
    engine = make_engine(database_url, connect_args={"options": "-c statement_timeout=20"})
    try:
        with engine.connect() as conn, pytest.raises(OperationalError):
            conn.execute(text("SELECT pg_sleep(1)"))
    finally:
        engine.dispose()

"""Overlapping ingests (e.g. two scheduler ticks) must not create duplicates.

Races are timing-dependent, so these tests make them likely on purpose:
connections are opened *before* the barrier (so workers really start
together) and dedup lookups are slowed down to widen the check-then-insert
window.

What protects what:
- level 1 (same source_job_id) is safe from the UNIQUE constraint alone;
- level 2/3 (lookup, then insert) needs the per-board advisory lock. Remove
  the lock from ingest_snapshot and the level-3 test below fails.
"""

import threading
import time

import pytest

from jobradar import repository
from jobradar.repository import ingest_snapshot
from tests.factories import make_job
from tests.integration.helpers import count


@pytest.fixture
def slow_lookups(monkeypatch):
    real = repository._find_original

    def slow(conn, column, value):
        result = real(conn, column, value)
        time.sleep(0.05)  # widen the window between "not found" and INSERT
        return result

    monkeypatch.setattr(repository, "_find_original", slow)


def run_concurrently(engine, snapshots, boards=None):
    boards = boards or ["acme"] * len(snapshots)
    barrier = threading.Barrier(len(snapshots))
    results, errors = [None] * len(snapshots), []

    def worker(i):
        try:
            with engine.connect() as conn:
                with conn.begin():
                    barrier.wait()
                    results[i] = ingest_snapshot(conn, source="greenhouse", board=boards[i],
                                                 jobs=snapshots[i])
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(snapshots))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    return results


def snapshot(n):
    return [make_job(str(i), title=f"Role {i}") for i in range(n)]


def test_parallel_ingests_of_the_same_board_report_each_job_new_exactly_once(engine, slow_lookups):
    results = run_concurrently(engine, [snapshot(10) for _ in range(4)])
    assert sum(r.new for r in results) == 10
    assert count(engine) == 10


def test_parallel_ingests_do_not_double_count_level3_duplicates(engine, slow_lookups):
    # Two IDs with the same fingerprint, ingested by racing workers in
    # opposite orders: exactly one canonical job and one duplicate.
    same_role = [make_job("a"), make_job("b")]
    results = run_concurrently(engine, [same_role, list(reversed(same_role)), same_role])
    assert sum(r.new for r in results) == 1
    assert sum(r.duplicates for r in results) == 1
    assert count(engine) == 2


def test_different_boards_are_not_serialized(engine, slow_lookups):
    # 2 boards x 5 new jobs x 2 lookups x 50ms = ~0.5s each. Parallel ~0.5s;
    # if boards wrongly shared a lock it would be ~1s.
    snaps = [[make_job(f"{b}-{i}", company=b, title=f"R{i}") for i in range(5)] for b in ("acme", "globex")]
    start = time.monotonic()
    results = run_concurrently(engine, snaps, boards=["acme", "globex"])
    elapsed = time.monotonic() - start
    assert [r.new for r in results] == [5, 5]
    assert elapsed < 0.9, f"boards appear serialized ({elapsed:.2f}s)"

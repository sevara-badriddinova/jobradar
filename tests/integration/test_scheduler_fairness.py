"""Board scheduling must be fair across providers: no provider may starve.

Production regression (Oct 2026): providers were ordered by due *count*, so
with 2 collection slots Greenhouse/Ashby/Workable (thousands due) took every
slot and 3,704 Lever/Personio/Recruitee/Rippling/Workday boards never ran.
"""

from collections import Counter

from sqlalchemy import func, select, update

from jobradar import queue as q
from jobradar.db import board_state, companies
from jobradar.worker import Worker, WorkerConfig
from tests.integration.test_workers import SETTINGS, FakeBoards, add_board

PROVIDERS = ["greenhouse", "ashby", "workable", "workday", "recruitee", "personio", "rippling", "lever"]


def worker(engine, **cfg):
    cfg.setdefault("collect_concurrency", 2)
    return Worker(engine, WorkerConfig(**cfg), settings_loader=lambda: SETTINGS,
                  collector_factory=FakeBoards())


def set_due(engine, collector, minutes_ago):
    with engine.begin() as conn:
        ids = select(companies.c.id).where(companies.c.collector == collector).scalar_subquery()
        conn.execute(update(board_state).where(board_state.c.company_id.in_(ids))
                     .values(next_due_at=func.now() - func.make_interval(0, 0, 0, 0, 0, minutes_ago)))


def finish_all(engine, w, boards):
    with engine.begin() as conn:
        for b in boards:
            assert q.finish_board(conn, b, w.owner, ok=True, error=None, fetched=1, nbytes=1,
                                  base_interval=w.cfg.base_interval)
    for b in boards:
        w._inflight[b.collector] -= 1
        w._boards.pop(b.company_id, None)


def seed(engine, sizes):
    for provider, n in sizes.items():
        for i in range(n):
            add_board(engine, f"{provider}-{i}", collector=provider, priority=3)
    with engine.begin() as conn:
        q.seed_boards(conn)


def test_small_provider_gets_a_slot_despite_a_huge_older_backlog(engine):
    seed(engine, {"greenhouse": 60, "lever": 1})
    set_due(engine, "greenhouse", 120)     # bigger AND more overdue
    set_due(engine, "lever", 5)
    w = worker(engine)
    claimed = w.claim_boards(2)
    # One slot per provider per pass: Lever is served in the very first round.
    assert sorted(b.collector for b in claimed) == ["greenhouse", "lever"]


def test_every_provider_is_served_within_one_rotation(engine):
    sizes = {"greenhouse": 40, "ashby": 30, "workable": 20, "workday": 15,
             "recruitee": 3, "personio": 2, "rippling": 2, "lever": 1}
    seed(engine, sizes)
    for i, p in enumerate(PROVIDERS):
        set_due(engine, p, 100 - i)        # big providers slightly more overdue
    w = worker(engine)
    served = Counter()
    for _ in range(8):                      # rotation slot = every other claim: 8 providers in 8 rounds
        w._due_cache = ({}, -1e18)          # force a fresh view like the 10 s cache expiry
        boards = w.claim_boards(2)
        served.update(b.collector for b in boards)
        finish_all(engine, w, boards)
    assert set(served) == set(PROVIDERS)


def test_never_polled_boards_go_first(engine):
    seed(engine, {"greenhouse": 5, "personio": 1})
    set_due(engine, "greenhouse", 600)      # personio keeps its '-infinity' seed
    with engine.connect() as conn:
        overview = q.due_overview(conn)
    assert overview["personio"][1] == -1e18 and overview["greenhouse"][0] == 5
    w = worker(engine, collect_concurrency=1)
    [first] = w.claim_boards(1)
    assert first.collector == "personio"


def test_provider_concurrency_caps_still_apply(engine):
    seed(engine, {"greenhouse": 10})
    w = worker(engine, collect_concurrency=8)
    claimed = w.claim_boards(8)
    assert len(claimed) == w.provider_limit("greenhouse") == 3


def test_sustained_simulation_has_no_starvation(engine):
    """Many cycles with demand above capacity: every provider keeps getting
    polled in proportion, and none waits more than a few rounds."""
    sizes = {"greenhouse": 30, "ashby": 20, "workable": 15, "workday": 10,
             "recruitee": 8, "personio": 6, "rippling": 6, "lever": 4}
    seed(engine, sizes)
    w = worker(engine)
    last_served, gaps = {}, Counter()
    for rnd in range(40):
        w._due_cache = ({}, -1e18)
        boards = w.claim_boards(2)
        assert boards, "boards are due every round in this simulation"
        for b in boards:
            if b.collector in last_served:
                gaps[b.collector] = max(gaps[b.collector], rnd - last_served[b.collector])
            last_served[b.collector] = rnd
        # Return each board immediately due again: capacity < demand forever.
        with engine.begin() as conn:
            for b in boards:
                q.finish_board(conn, b, w.owner, ok=True, error=None, fetched=1, nbytes=1, retry_in=0)
        for b in boards:
            w._inflight[b.collector] -= 1
    assert set(last_served) == set(PROVIDERS)
    assert max(gaps.values()) <= 8          # every provider served at least once per rotation


def test_default_interval_is_sized_to_capacity():
    cfg = WorkerConfig()
    # 12k priority-3 boards every base*3 seconds must stay under ~60 boards/min.
    assert 12_237 / (cfg.base_interval * 3 / 60) < 60

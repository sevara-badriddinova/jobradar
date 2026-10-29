"""Oct 29: newest postings first, the AI budget spread through the day, and the
boards that produce relevant jobs checked more often. Nothing already stored is
re-filtered or re-scored."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, insert, select, text, update

from jobradar import queue as q
from jobradar.ai_budget import DailyBudgetMeter, pacing_from_env
from jobradar.db import ai_usage, board_state, companies, jobs as jobs_t, work_queue
from jobradar.settings import load_settings
from tests.factories import make_job
from tests.integration.helpers import ingest, row
from tests.integration.test_migrations import scratch  # noqa: F401 - fixture
from tests.integration.test_scheduler_fairness import seed, set_due, worker
from tests.integration.test_workers import FakeBoards, add_board, fresh, make_worker

NOW = datetime.now(UTC)


# --- matching order: newest publication day first --------------------------------------------

def queue_match(engine, sids):
    with engine.begin() as conn:
        q.enqueue(conn, "match", [row(engine, s).id for s in sids])


def claim_sequence(engine):
    out = []
    while True:
        with engine.begin() as conn:
            got = q.claim(conn, "match", "w", 1)
        if not got:
            return out
        out.append(got[0].job_id)


def test_newest_publication_day_is_scored_first(engine):
    ingest(engine, [make_job("old", posted_at=NOW - timedelta(days=2)),
                    make_job("today", posted_at=NOW - timedelta(minutes=30)),
                    make_job("yesterday", posted_at=NOW - timedelta(days=1, hours=1))])
    queue_match(engine, ["old", "today", "yesterday"])           # one batch, one origin time
    assert claim_sequence(engine) == [row(engine, s).id for s in ("today", "yesterday", "old")]


def test_within_a_day_the_newest_discovery_goes_first(engine):
    day = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    ingest(engine, [make_job("a", posted_at=day), make_job("b", posted_at=day)])
    with engine.begin() as conn:
        q.enqueue(conn, "match", [row(engine, "a").id], origin_at=NOW - timedelta(hours=3))
        q.enqueue(conn, "match", [row(engine, "b").id], origin_at=NOW - timedelta(hours=1))
    assert claim_sequence(engine) == [row(engine, "b").id, row(engine, "a").id]


def test_tasks_queued_before_the_change_come_after_dated_ones_untouched(engine):
    ingest(engine, [make_job("legacy", posted_at=NOW - timedelta(hours=1)),
                    make_job("new", posted_at=NOW - timedelta(days=2))])
    queue_match(engine, ["legacy", "new"])
    with engine.begin() as conn:      # as queued by the previous image: no rank
        conn.execute(update(work_queue).where(work_queue.c.job_id == row(engine, "legacy").id)
                     .values(rank_at=None))
    assert claim_sequence(engine) == [row(engine, "new").id, row(engine, "legacy").id]


def test_requeueing_a_changed_posting_takes_its_new_date(engine):
    ingest(engine, [make_job("x", posted_at=NOW - timedelta(days=2))])
    queue_match(engine, ["x"])
    with engine.begin() as conn:
        conn.execute(update(jobs_t).where(jobs_t.c.source_job_id == "x").values(posted_at=NOW))
    queue_match(engine, ["x"])
    with engine.connect() as conn:
        rank = conn.execute(select(work_queue.c.rank_at).where(work_queue.c.kind == "match")).scalar_one()
    assert abs((rank - NOW).total_seconds()) < 1


def test_filter_tasks_stay_first_in_first_out(engine):
    ingest(engine, [make_job("a", posted_at=NOW - timedelta(days=2)), make_job("b", posted_at=NOW)])
    with engine.begin() as conn:
        q.enqueue(conn, "filter", [row(engine, "a").id])
        q.enqueue(conn, "filter", [row(engine, "b").id])
        assert conn.execute(select(func.count()).where(work_queue.c.rank_at.is_not(None))).scalar_one() == 0
        [first] = q.claim(conn, "filter", "w", 1)
    assert first.job_id == row(engine, "a").id


# --- the AI budget, spread through the UTC day -------------------------------------------------

def meter(engine, at, budget=1.0, paced=True):
    return DailyBudgetMeter(engine, budget, paced=paced, clock=lambda: at)


def spend(engine, day, usd):
    with engine.begin() as conn:
        conn.execute(insert(ai_usage).values(day=day, model="m", calls=1, input_tokens=1, output_tokens=1,
                                             cost_usd=usd))


@pytest.mark.parametrize("hour,allowed", [(0, 1 / 24), (11, 0.5), (23, 1.0)])
def test_allowance_grows_one_24th_per_hour(engine, hour, allowed):
    at = datetime(2026, 10, 29, hour, 30, tzinfo=UTC)
    assert meter(engine, at).allowance() == pytest.approx(allowed, abs=1e-6)
    assert meter(engine, at, paced=False).allowance() == 1.0


def test_pacing_pauses_for_the_hour_and_unused_hours_carry_over(engine):
    from jobradar.llm import BudgetExceeded
    spend(engine, date(2026, 10, 29), 0.30)
    assert not meter(engine, datetime(2026, 10, 29, 9, 5, tzinfo=UTC)).exhausted()     # 10/24 = 0.4167
    early = meter(engine, datetime(2026, 10, 29, 5, 59, tzinfo=UTC))                   # 6/24 = 0.25
    assert early.exhausted()
    with pytest.raises(BudgetExceeded, match="for this hour"):
        early.check()


def test_pacing_setting(monkeypatch):
    monkeypatch.delenv("JOBRADAR_AI_BUDGET_PACING", raising=False)
    assert pacing_from_env() is True
    monkeypatch.setenv("JOBRADAR_AI_BUDGET_PACING", "off")
    assert pacing_from_env() is False
    monkeypatch.setenv("JOBRADAR_AI_BUDGET_PACING", "daily")
    with pytest.raises(ValueError):
        pacing_from_env()


def test_worker_uses_a_paced_meter_when_configured(engine):
    w = make_worker(engine, FakeBoards(), ai_daily_budget_usd=1.0, ai_budget_paced=True)
    assert w.meter.paced and w.meter.allowance() <= 1.0
    assert not make_worker(engine, FakeBoards(), ai_daily_budget_usd=1.0).meter.paced


# --- relevant boards: checked more often, never at the others' expense ------------------------

def relevant_at(engine, name):
    with engine.connect() as conn:
        return conn.execute(select(board_state.c.relevant_at).join(companies, companies.c.id == board_state.c.company_id)
                            .where(companies.c.name == name)).scalar_one()


def make_relevant(engine, name, days_ago=0):
    with engine.begin() as conn:
        cid = conn.execute(select(companies.c.id).where(companies.c.name == name)).scalar_one()
        conn.execute(update(board_state).where(board_state.c.company_id == cid)
                     .values(relevant_at=func.now() - func.make_interval(0, 0, 0, days_ago)))


def due_minutes_ago(engine, name, minutes):
    with engine.begin() as conn:
        cid = conn.execute(select(companies.c.id).where(companies.c.name == name)).scalar_one()
        conn.execute(update(board_state).where(board_state.c.company_id == cid)
                     .values(next_due_at=func.now() - func.make_interval(0, 0, 0, 0, 0, minutes)))


def test_a_board_becomes_relevant_when_its_posting_passes_the_filters(engine):
    boards = FakeBoards()
    add_board(engine, "acme")
    add_board(engine, "quiet")
    boards.snapshots = {"acme": [fresh("1")], "quiet": [fresh("2", title="Office Manager")]}
    w = make_worker(engine, boards)
    from tests.integration.test_workers import poll
    poll(w, engine)
    while w.filter_once():
        pass
    assert relevant_at(engine, "acme") is not None
    assert relevant_at(engine, "quiet") is None


def test_claim_prefers_a_due_relevant_board_only_when_asked(engine):
    seed(engine, {"greenhouse": 2})
    due_minutes_ago(engine, "greenhouse-0", 600)          # most overdue, not relevant
    due_minutes_ago(engine, "greenhouse-1", 5)
    make_relevant(engine, "greenhouse-1")
    with engine.begin() as conn:
        [plain] = q.claim_boards(conn, "greenhouse", "a", 1)
        assert plain.name == "greenhouse-0" and not plain.relevant
        conn.execute(update(board_state).values(lease_owner=None, lease_until=None))
        [first] = q.claim_boards(conn, "greenhouse", "b", 1, prefer_relevant=True)
    assert first.name == "greenhouse-1" and first.relevant


def test_relevance_expires_after_the_window(engine):
    seed(engine, {"greenhouse": 1})
    make_relevant(engine, "greenhouse-0", days_ago=20)
    set_due(engine, "greenhouse", 1)
    with engine.begin() as conn:
        [b] = q.claim_boards(conn, "greenhouse", "a", 1, prefer_relevant=True, relevant_days=14)
        assert not b.relevant
        assert q.due_relevant(conn, 14) == {}


def test_a_relevant_board_is_due_again_after_its_interval(engine):
    seed(engine, {"greenhouse": 2})
    make_relevant(engine, "greenhouse-0")
    set_due(engine, "greenhouse", 1)
    with engine.begin() as conn:
        claimed = q.claim_boards(conn, "greenhouse", "w", 2)
        for b in claimed:
            assert q.finish_board(conn, b, "w", ok=True, error=None, fetched=1, nbytes=1,
                                  base_interval=4800, relevant_interval=3600)
        due = dict(conn.execute(text("""
            SELECT c.name, extract(epoch FROM b.next_due_at - now()) FROM board_state b
            JOIN companies c ON c.id = b.company_id""")).all())
    assert 3200 < due["greenhouse-0"] < 4000                 # ~1 h
    assert due["greenhouse-1"] > 4800 * 3 * 0.85             # priority 3: ~4 h


def test_half_the_slots_go_to_relevant_boards_and_the_rest_by_age(engine):
    seed(engine, {"greenhouse": 6, "ashby": 6})
    set_due(engine, "greenhouse", 600)
    set_due(engine, "ashby", 600)
    for i in range(3):
        make_relevant(engine, f"ashby-{i}")
        due_minutes_ago(engine, f"ashby-{i}", 2)               # barely due
    w = worker(engine, collect_concurrency=4, provider_concurrency={"greenhouse": 4, "ashby": 4},
               relevant_interval=3600.0)
    claimed = w.claim_boards(4)
    relevant = [b for b in claimed if b.relevant]
    assert len(relevant) == 2                                  # the two odd slots
    assert len(claimed) - len(relevant) == 2                   # age order keeps its half


def test_workday_relevant_boards_get_two_hours(engine):
    w = worker(engine, relevant_interval=3600.0)
    assert w.relevant_interval("greenhouse") == 3600 and w.relevant_interval("workday") == 7200


def test_fast_lane_is_off_by_default(engine):
    """Oct 29: hourly visits to the busiest boards cut production polling ~4.5x."""
    seed(engine, {"greenhouse": 6, "ashby": 6})
    set_due(engine, "greenhouse", 600)
    set_due(engine, "ashby", 600)
    for i in range(3):
        make_relevant(engine, f"ashby-{i}")
        due_minutes_ago(engine, f"ashby-{i}", 2)
    w = worker(engine, collect_concurrency=4, provider_concurrency={"greenhouse": 4, "ashby": 4})
    assert w.relevant_interval("greenhouse") is None and w.relevant_interval("workday") is None
    claimed = w.claim_boards(4)
    assert len(claimed) == 4 and not any(b.relevant for b in claimed)   # plain age order
    assert w._relevant_due == {}


# --- the deploy: migration 0016 seeds relevant boards from recent AI work ---------------------

def test_migration_seeds_relevant_boards_from_scored_or_queued_jobs(scratch, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from jobradar.db import migrate
    from tests.integration.helpers import ingest_with_an_older_schema
    ingest_with_an_older_schema(monkeypatch)
    config = Config()
    config.set_main_option("script_location", "jobradar:migrations")
    with scratch.connect() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "0015")
    ingest(scratch, [make_job("q", posted_at=NOW)], board="hot")
    ingest(scratch, [make_job("c", posted_at=NOW)], board="cold")
    with scratch.begin() as conn:
        for name in ("hot", "cold"):
            cid = conn.execute(text("INSERT INTO companies (name, collector, board) VALUES (:n, 'greenhouse', :n) "
                                    "RETURNING id"), {"n": name}).scalar_one()
            conn.execute(text("INSERT INTO board_state (company_id) VALUES (:c)"), {"c": cid})
        conn.execute(text("INSERT INTO work_queue (kind, job_id) SELECT 'match', id FROM jobs WHERE board = 'hot'"))
    migrate(scratch)
    with scratch.connect() as conn:
        marked = dict(conn.execute(text("""SELECT c.name, b.relevant_at IS NOT NULL FROM board_state b
                                           JOIN companies c ON c.id = b.company_id""")).all())
        rank = conn.execute(text("SELECT rank_at FROM work_queue")).scalar_one()
    assert marked == {"hot": True, "cold": False}
    assert rank is None                                         # queued tasks are left as they were


# --- alerts: 90+ only ---------------------------------------------------------------------------

def test_profile_alerts_only_on_90_and_above():
    n = load_settings("profile.toml").notifications
    assert n.immediate_min_score == 90 and n.digest_min_score == 90

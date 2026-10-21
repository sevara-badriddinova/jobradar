"""Freshness: employer publication date vs. JobRadar's first sighting.

The strict rule (employer date within 72 h) hid long-open new-grad postings
that boards newly list (8 of 30 benchmark jobs). The discovery rule accepts a
posting first seen within 72 h whose employer date is within 14 days, unless it
came from a board's initial scan, so adding a board never floods alerts.
"""

from datetime import UTC, datetime, timedelta
from itertools import product

import pytest
from sqlalchemy import func, select, text, update

from jobradar.db import jobs as jobs_t, notifications
from jobradar.filters import run_filters
from jobradar.recency import publication_reason, recent_jobs
from jobradar.settings import FilterSettings, IngestionSettings, Preferences, Settings
from tests.factories import assessment, make_job
from tests.integration.helpers import ingest, row
from tests.integration.test_matcher import add_profile
from tests.integration.test_migrations import scratch  # noqa: F401 - fixture
from tests.integration.test_workers import (SETTINGS, add_board, boards, llm, make_due, make_worker,  # noqa: F401
                                            poll, telegram)

NOW = datetime.now(UTC)
POLICY = FilterSettings(max_posting_age_days=3, max_discovery_age_days=14)


def ago(**kw):
    return NOW - timedelta(**kw)


# --- the rule ------------------------------------------------------------------------------

@pytest.mark.parametrize("posted,first_seen,backfill,discovery,expected", [
    (ago(days=1), ago(hours=1), False, 14, None),                        # strict
    (ago(days=1), ago(hours=1), True, 14, None),                         # strict ignores backfill
    (ago(days=10), ago(hours=1), False, 14, None),                       # newly listed, long open
    (ago(days=10), ago(hours=1), True, 14, "older"),                     # initial-scan backlog
    (ago(days=20), ago(hours=1), False, 14, "older"),                    # beyond the discovery window
    (ago(days=10), ago(days=4), False, 14, "older"),                     # we saw it too long ago
    (ago(days=10), ago(hours=1), False, None, "older"),                  # discovery disabled
    (None, ago(hours=1), False, 14, "unavailable"),                      # undated: never fresh
    (NOW + timedelta(days=1), ago(hours=1), False, 14, "future"),
])
def test_publication_rule(posted, first_seen, backfill, discovery, expected):
    reason = publication_reason(posted, 3, NOW, first_seen_at=first_seen, backfill=backfill,
                                discovery_days=discovery)
    if expected is None:
        assert reason is None
    else:
        assert reason and expected in reason


def test_sql_and_python_rules_agree(engine):
    """recent_jobs (SQL) and publication_reason (Python) must never disagree,
    or a job could pass the filter and then never be scored or delivered."""
    posted_opts = [None, ago(hours=5), ago(days=2, hours=23), ago(days=3, hours=1), ago(days=10),
                   ago(days=13, hours=23), ago(days=14, hours=1), NOW + timedelta(hours=2)]
    seen_opts = [ago(minutes=5), ago(days=2), ago(days=3, hours=1), ago(days=9)]
    cases = list(product(posted_opts, seen_opts, [False, True]))
    ingest(engine, [make_job(str(i), title=f"Engineer {i}") for i in range(len(cases))])
    with engine.begin() as conn:
        for i, (posted, seen, backfill) in enumerate(cases):
            conn.execute(update(jobs_t).where(jobs_t.c.source_job_id == str(i))
                         .values(posted_at=posted, first_seen_at=seen, discovered_in_backfill=backfill))
        sql = set(conn.execute(select(jobs_t.c.source_job_id).where(recent_jobs(3, NOW, 14))).scalars())
    py = {str(i) for i, (p, s, b) in enumerate(cases)
          if publication_reason(p, 3, NOW, first_seen_at=s, backfill=b, discovery_days=14) is None}
    assert sql == py and len(py) > 8


def test_settings_validation():
    with pytest.raises(ValueError):
        FilterSettings(max_posting_age_days=3, max_discovery_age_days=2)
    with pytest.raises(ValueError):
        Settings(filters=POLICY, ingestion=IngestionSettings(max_age_days=7))
    Settings(filters=POLICY, ingestion=IngestionSettings(max_age_days=30))


# --- initial scans and later polls ------------------------------------------------------------

def filter_now(engine, policy=POLICY):
    with engine.begin() as conn:
        return run_filters(conn, policy)


def test_initial_scan_backlog_never_floods(engine):
    backlog = [make_job(f"b{i}", title=f"Backend Engineer {i}", posted_at=ago(days=5 + i % 8)) for i in range(50)]
    today = [make_job("t1", title="Platform Engineer", posted_at=ago(hours=6))]
    ingest(engine, backlog + today, board="newco")
    with engine.connect() as conn:
        flags = conn.execute(select(jobs_t.c.discovered_in_backfill).distinct()).scalars().all()
    assert flags == [True]
    result = filter_now(engine)
    assert result.passed == 1                            # only the employer-fresh one
    assert result.rejected["posting date: older than 3 days"] == 50


def test_a_long_open_role_newly_listed_on_a_known_board_is_fresh(engine):
    ingest(engine, [make_job("a", title="Backend Engineer", posted_at=ago(days=20))], board="acme")
    filter_now(engine)
    later = [make_job("a", title="Backend Engineer", posted_at=ago(days=20)),
             make_job("n", title="Software Engineer, New Grad", posted_at=ago(days=8))]
    ingest(engine, later, board="acme")
    assert row(engine, "n").discovered_in_backfill is False
    result = filter_now(engine)
    assert result.passed == 1 and row(engine, "n").status == "DISCOVERED"


def test_discovery_freshness_expires_after_the_window(engine):
    ingest(engine, [make_job("a", title="Backend Engineer", posted_at=ago(days=20))], board="acme")
    ingest(engine, [make_job("a", title="Backend Engineer", posted_at=ago(days=20)),
                    make_job("n", title="Software Engineer", posted_at=ago(days=8))], board="acme")
    assert filter_now(engine).passed == 1
    with engine.begin() as conn:
        conn.execute(update(jobs_t).where(jobs_t.c.source_job_id == "n")
                     .values(first_seen_at=ago(days=4), status="MATCHED", match_score=90))
    result = filter_now(engine)
    assert result.rejected == {"posting date: older than 3 days": 1}


def test_strict_policy_is_unchanged_without_a_discovery_window(engine):
    ingest(engine, [make_job("a", title="Backend Engineer", posted_at=ago(days=20))], board="acme")
    ingest(engine, [make_job("a", title="Backend Engineer", posted_at=ago(days=20)),
                    make_job("n", title="Software Engineer", posted_at=ago(days=8))], board="acme")
    assert filter_now(engine, FilterSettings(max_posting_age_days=3)).passed == 0


# --- end to end: scored and delivered; reposts not re-announced ----------------------------------

def worker_settings():
    return SETTINGS.model_copy(update={"filters": POLICY,
                                       "preferences": Preferences(interests=["backend"])})


def test_discovery_fresh_job_is_scored_and_alerted(engine, boards, llm, telegram):
    server, tg = telegram
    add_board(engine, "acme")
    add_profile(engine)
    llm(assessment(role_match=10, technical_match=10, experience_match=10))
    w = make_worker(engine, boards, telegram=tg, settings=worker_settings())
    boards.snapshots["acme"] = [make_job("old", title="Office Manager", posted_at=ago(days=20))]
    poll(w, engine)                                       # initial scan
    boards.snapshots["acme"] = [make_job("old", title="Office Manager", posted_at=ago(days=20)),
                                make_job("n", title="Backend Engineer", posted_at=ago(days=8))]
    make_due(engine)
    poll(w, engine)
    w.filter_once(), w.match_once(), w.notify_once()
    assert len(server.sent) == 1 and "Backend Engineer" in server.sent[0]["text"]


def test_a_repost_of_an_announced_role_is_not_announced_again(engine, boards, llm, telegram):
    server, tg = telegram
    add_board(engine, "acme")
    add_profile(engine)
    llm(assessment(role_match=10, technical_match=10, experience_match=10))
    w = make_worker(engine, boards, telegram=tg, settings=worker_settings())
    boards.snapshots["acme"] = [make_job("seed", title="Office Manager", posted_at=ago(days=20))]
    poll(w, engine)
    first = make_job("v1", title="Backend Engineer", posted_at=ago(hours=3))
    boards.snapshots["acme"] = [make_job("seed", title="Office Manager", posted_at=ago(days=20)), first]
    make_due(engine)
    poll(w, engine)
    w.filter_once(), w.match_once(), w.notify_once()
    assert len(server.sent) == 1
    # The employer closes it and re-lists the same role under a new ID.
    repost = first.model_copy(update={"source_job_id": "v2", "url": "https://example.com/v2",
                                      "posted_at": ago(hours=1)})
    boards.snapshots["acme"] = [make_job("seed", title="Office Manager", posted_at=ago(days=20)), repost]
    make_due(engine)
    poll(w, engine)
    assert row(engine, "v2").status == "REPOSTED"
    w.filter_once(), w.match_once(), w.notify_once()
    assert len(server.sent) == 1                          # no second alert for the same role
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(notifications)).scalar_one() == 1


def test_a_repost_of_a_never_announced_role_is_announced(engine, boards, llm, telegram):
    server, tg = telegram
    add_board(engine, "acme")
    add_profile(engine)
    llm(assessment(role_match=10, technical_match=10, experience_match=10))
    w = make_worker(engine, boards, telegram=tg, settings=worker_settings())
    stale = make_job("v1", title="Backend Engineer", posted_at=ago(days=30))
    boards.snapshots["acme"] = [stale]
    poll(w, engine)
    w.filter_once()
    assert row(engine, "v1").status == "FILTERED"
    boards.snapshots["acme"] = [stale.model_copy(update={"source_job_id": "v2", "url": "https://example.com/v2",
                                                         "posted_at": ago(hours=2)})]
    make_due(engine)
    poll(w, engine)
    w.filter_once(), w.match_once(), w.notify_once()
    assert row(engine, "v2").status in ("NOTIFIED", "REPOSTED", "MATCHED")
    assert len(server.sent) == 1


def test_same_snapshot_relisting_is_a_repost_but_partial_snapshots_stay_duplicates(engine):
    v1 = make_job("v1", title="Backend Engineer", posted_at=ago(days=1))
    ingest(engine, [v1])
    v2 = v1.model_copy(update={"source_job_id": "v2", "url": "https://example.com/v2"})
    ingest(engine, [v2], complete=False)               # partial page: v1 may still be listed
    assert row(engine, "v2").status == "DUPLICATE" and row(engine, "v1").closed_at is None
    v3 = v1.model_copy(update={"source_job_id": "v3", "url": "https://example.com/v3"})
    ingest(engine, [v3])                               # complete: v1 is gone, v3 replaces it
    assert row(engine, "v3").status == "REPOSTED" and row(engine, "v1").closed_at is not None
    parallel = v1.model_copy(update={"source_job_id": "p", "url": "https://example.com/p"})
    ingest(engine, [parallel], board="acme-eu")         # another board still lists the original
    assert row(engine, "p").status == "DUPLICATE"


def test_discovery_window_does_not_change_the_filter_policy_signature(engine):
    """Adding/enabling the window must not force a full recheck (heap scans of
    jobs in one transaction) on deploy; 0014 re-queues the affected rows."""
    from jobradar.db import app_state
    def signature(policy):
        with engine.begin() as conn:
            run_filters(conn, policy)
            return conn.execute(select(app_state.c.value).where(app_state.c.key == "filter_context")).scalar_one()
    assert signature(FilterSettings(max_posting_age_days=3)) == signature(POLICY)


def test_migration_0014_requeues_only_recent_too_old_rejections(scratch, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from jobradar.db import migrate
    monkeypatch.setattr("jobradar.repository._is_initial_scan", lambda *a: False)
    config = Config()
    config.set_main_option("script_location", "jobradar:migrations")
    with scratch.connect() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "0013")
    ingest(scratch, [make_job(s, title=f"Engineer {s}", posted_at=ago(days=d)) for s, d in
                     [("recent", 8), ("ancient", 60), ("seen_long_ago", 8), ("title", 8)]])
    with scratch.begin() as conn:
        conn.execute(text("UPDATE jobs SET status = 'FILTERED', filtered_at = now(), "
                          "filter_reason = 'posting date: older than 3 days'"))
        conn.execute(text("UPDATE jobs SET first_seen_at = now() - interval '9 days' "
                          "WHERE source_job_id = 'seen_long_ago'"))
        conn.execute(text("UPDATE jobs SET filter_reason = 'title: senior' WHERE source_job_id = 'title'"))
    migrate(scratch)
    with scratch.connect() as conn:
        pending = dict(conn.execute(text("SELECT source_job_id, filtered_at IS NULL FROM jobs")).all())
    assert pending == {"recent": True, "ancient": False, "seen_long_ago": False, "title": False}

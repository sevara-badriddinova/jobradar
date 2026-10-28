"""Oct 28: more title exclusions without a full filter recheck.

Adding exclusions can only reject more jobs, so only jobs that are not filtered
yet are rechecked (by title), and their queued AI tasks are dropped."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text, update

from jobradar import queue as q
from jobradar.db import app_state, jobs as jobs_t
from jobradar.filters import _signature, run_filters
from jobradar.settings import TITLE_EXCLUDE_BEFORE_20261028, FilterSettings
from tests.factories import make_job
from tests.integration.helpers import ingest, row

NOW = datetime.now(UTC)
NEW = FilterSettings(max_posting_age_days=3)
OLD = FilterSettings(max_posting_age_days=3, title_must_exclude=list(TITLE_EXCLUDE_BEFORE_20261028))
EXCLUDED = {"max_discovery_age_days", "require_explicit_sponsorship"}


def posting(sid, title, **kw):
    kw.setdefault("posted_at", NOW - timedelta(hours=2))
    return make_job(sid, title=title, description="Build things in Python.", **kw)


def stored_signature(engine):
    with engine.connect() as conn:
        return conn.execute(select(app_state.c.value).where(app_state.c.key == "filter_context")).scalar_one()


def match_tasks(engine):
    with engine.connect() as conn:
        return set(conn.execute(text("SELECT job_id FROM work_queue WHERE kind = 'match'")).scalars())


def setup_under_old_policy(engine):
    ingest(engine, [posting("fse", "Field Service Engineer II"), posting("swe", "Software Engineer"),
                    posting("coop", "Software Engineer Co-op"), posting("matched", "Process Engineer"),
                    posting("stale", "Software Engineer, Backend")])
    with engine.begin() as conn:
        result = run_filters(conn, OLD, needs_sponsorship=False)
        assert result.passed == 5
        q.enqueue(conn, "match", result.passed_ids)
        conn.execute(update(jobs_t).where(jobs_t.c.source_job_id == "matched").values(status="MATCHED"))
        # A stale content rejection that a full recheck would undo (title passes now).
        conn.execute(update(jobs_t).where(jobs_t.c.source_job_id == "stale").values(
            status="FILTERED", filter_reason="title: senior", filtered_at=func.now()))
    assert stored_signature(engine) == _signature(OLD.model_dump(exclude=EXCLUDED), False, version=6)


def test_added_exclusions_recheck_only_unfiltered_jobs_and_drop_their_ai_tasks(engine):
    setup_under_old_policy(engine)
    with engine.begin() as conn:
        result = run_filters(conn, NEW, needs_sponsorship=False)
    assert row(engine, "fse").status == "FILTERED"
    assert row(engine, "fse").filter_reason == "title: field service"
    assert row(engine, "matched").status == "FILTERED"                  # matched but not sent
    assert row(engine, "matched").filter_reason == "title: process engineer"
    assert row(engine, "swe").status == "DISCOVERED"
    assert row(engine, "coop").status == "DISCOVERED"                   # software co-ops stay wanted
    assert row(engine, "stale").filter_reason == "title: senior"        # no full recheck happened
    assert result.rejected["title: field service"] == 1
    assert match_tasks(engine) == {row(engine, "swe").id, row(engine, "coop").id, row(engine, "stale").id}
    assert stored_signature(engine) == _signature(NEW.model_dump(exclude=EXCLUDED), False, version=6)


def test_the_title_pass_runs_once(engine):
    setup_under_old_policy(engine)
    with engine.begin() as conn:
        run_filters(conn, NEW, needs_sponsorship=False)
        conn.execute(update(jobs_t).where(jobs_t.c.source_job_id == "swe").values(title="Plant Engineer"))
    with engine.begin() as conn:            # signature is current: incremental pass, no title sweep
        run_filters(conn, NEW, needs_sponsorship=False)
    assert row(engine, "swe").status == "DISCOVERED"


def test_leased_tasks_are_left_to_their_worker(engine):
    setup_under_old_policy(engine)
    with engine.begin() as conn:
        claimed = {t.job_id for t in q.claim(conn, "match", "w1", 10)}
    assert row(engine, "fse").id in claimed
    with engine.begin() as conn:
        run_filters(conn, NEW, needs_sponsorship=False)
    assert row(engine, "fse").status == "FILTERED"
    assert row(engine, "fse").id in match_tasks(engine)                 # still leased by w1


def test_other_policy_changes_still_run_a_full_recheck(engine):
    setup_under_old_policy(engine)
    looser = FilterSettings(max_posting_age_days=3, max_years_required=3)
    with engine.begin() as conn:
        run_filters(conn, looser, needs_sponsorship=False)
    assert row(engine, "stale").status == "DISCOVERED"                  # relabelled by the full pass
    assert row(engine, "fse").filter_reason == "title: field service"


def test_a_custom_list_that_removes_a_default_is_a_real_change(engine):
    setup_under_old_policy(engine)
    custom = FilterSettings(max_posting_age_days=3,
                            title_must_exclude=[p for p in NEW.title_must_exclude if p != "senior"])
    with engine.begin() as conn:
        run_filters(conn, custom, needs_sponsorship=False)
    assert row(engine, "stale").status == "DISCOVERED"                  # full recheck

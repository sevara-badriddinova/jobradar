"""prune-content: empty the text of jobs that can never qualify, and keep them
recognisable so ingestion neither rewrites them nor treats them as new."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text, update

from jobradar import dedup
from jobradar.db import jobs as jobs_t
from jobradar.filters import PRUNED_REASON, run_filters
from jobradar.prune import prune_content
from jobradar.settings import FilterSettings
from jobradar.sponsorship_review import UNKNOWN_REASON
from tests.factories import make_job
from tests.integration.helpers import ingest, row

NOW = datetime.now(UTC)
OLD_POST = datetime(2026, 10, 1, tzinfo=UTC)          # make_job's default: long ago
TEXT = "Build backend services. " * 40


def job(sid, **kw):
    kw.setdefault("description", TEXT)
    return make_job(sid, **kw)


def age(engine, sid, *, days, status="FILTERED", reason="posting date: older than 3 days", source="greenhouse",
        **values):
    with engine.begin() as conn:
        conn.execute(update(jobs_t).where(jobs_t.c.source == source, jobs_t.c.source_job_id == sid).values(
            first_seen_at=func.now() - timedelta(days=days), status=status, filter_reason=reason,
            filtered_at=func.now(), **values))


def pruned(engine, sid, source="greenhouse"):
    r = row(engine, sid, source)
    return r.raw_digest is not None and r.raw == {} and r.description == ""


@pytest.fixture
def stored(engine):
    ingest(engine, [job("old"), job("young"), job("recent", posted_at=NOW - timedelta(days=5)),
                    job("passed"), job("held"), job("dup")])
    for sid in ("old", "young", "recent", "passed", "held", "dup"):
        age(engine, sid, days=1 if sid == "young" else 10)
    age(engine, "passed", days=10, status="DISCOVERED", reason=None)
    age(engine, "held", days=10, reason=UNKNOWN_REASON)
    age(engine, "dup", days=10, status="DUPLICATE", reason=None)
    return engine


def test_only_jobs_that_can_never_qualify_are_emptied(stored):
    before = row(stored, "old")
    result = prune_content(stored, batch=2)
    assert result.pruned == 2
    assert pruned(stored, "old") and pruned(stored, "dup")
    for sid in ("young", "recent", "passed", "held"):
        assert row(stored, sid).raw_digest is None and row(stored, sid).description == TEXT
    after = row(stored, "old")
    # Everything that identifies the posting is kept.
    assert (after.title, after.url, after.fingerprint, after.description_hash, after.status, after.filter_reason) == (
        before.title, before.url, before.fingerprint, before.description_hash, before.status, before.filter_reason)
    assert prune_content(stored).pruned == 0                    # idempotent


def test_open_postings_of_detail_reusing_collectors_keep_their_payload(engine):
    ingest(engine, [job("wd-open", source="workday"), job("wd-closed", source="workday")], source="workday")
    for sid in ("wd-open", "wd-closed"):
        age(engine, sid, days=10, source="workday")
    with engine.begin() as conn:
        conn.execute(update(jobs_t).where(jobs_t.c.source_job_id == "wd-closed").values(closed_at=func.now()))
    prune_content(engine)
    assert not pruned(engine, "wd-open", "workday")             # its detail payload is reused
    assert pruned(engine, "wd-closed", "workday")


def test_an_unchanged_pruned_posting_is_not_rewritten(stored):
    prune_content(stored)
    before = row(stored, "old")
    result = ingest(stored, [job("old"), job("young"), job("recent", posted_at=NOW - timedelta(days=5)),
                             job("passed"), job("held"), job("dup")])
    assert result.changed == 0
    after = row(stored, "old")
    assert pruned(stored, "old")
    assert (after.status, after.filter_reason, after.filtered_at) == (before.status, before.filter_reason,
                                                                       before.filtered_at)


def test_a_changed_pruned_posting_gets_its_content_back(stored):
    prune_content(stored)
    ingest(stored, [job("old", description=TEXT + " Now with Go.")])
    r = row(stored, "old")
    assert r.raw_digest is None and r.description == TEXT + " Now with Go." and r.raw != {}
    assert r.status == "DISCOVERED" and r.filtered_at is None    # filtered again, with its text
    assert r.description_hash == dedup.description_hash(TEXT + " Now with Go.")


def test_a_new_date_restores_the_content(stored):
    prune_content(stored)
    ingest(stored, [job("old", posted_at=NOW - timedelta(days=1))])
    r = row(stored, "old")
    assert r.raw_digest is None and r.description == TEXT and r.filtered_at is None


def test_a_reopened_pruned_posting_gets_its_content_back(stored):
    with stored.begin() as conn:
        conn.execute(update(jobs_t).where(jobs_t.c.source_job_id == "old").values(closed_at=func.now()))
    prune_content(stored)
    assert pruned(stored, "old")
    result = ingest(stored, [job("old")], complete=False)
    assert result.reopened == 1
    r = row(stored, "old")
    assert r.closed_at is None and r.raw_digest is None and r.description == TEXT


def test_the_digest_ignores_volatile_workday_keys(engine):
    from jobradar.repository import raw_digest
    from sqlalchemy import cast, literal
    from sqlalchemy.dialects.postgresql import JSONB
    with engine.connect() as conn:
        a, b = (conn.execute(select(raw_digest(cast(literal(payload), JSONB), "workday"))).scalar_one()
                for payload in ('{"id": 1, "postedOn": "Posted Today"}', '{"postedOn": "Posted 3 Days Ago", "id": 1}'))
    assert a == b


def test_filters_never_judge_a_pruned_job(stored):
    prune_content(stored)
    with stored.begin() as conn:      # as if the date rules were widened later
        conn.execute(update(jobs_t).where(jobs_t.c.source_job_id == "old").values(
            posted_at=func.now() - timedelta(hours=1), filtered_at=None, status="DISCOVERED"))
    with stored.begin() as conn:
        result = run_filters(conn, FilterSettings(max_posting_age_days=3), only_ids=[row(stored, "old").id])
    assert result.passed == 0
    assert row(stored, "old").filter_reason == PRUNED_REASON


def test_refuses_thresholds_inside_the_freshness_window(engine):
    with pytest.raises(ValueError):
        prune_content(engine, posted_before_days=7)
    with pytest.raises(ValueError):
        prune_content(engine, min_age_days=1)


def test_cli_estimates_without_changing_anything(stored, db_env, capsys):
    from jobradar.cli import main
    assert main(["prune-content"]) == 0
    assert "estimate" in capsys.readouterr().out
    assert not pruned(stored, "old")
    assert main(["prune-content", "--yes", "--batch", "1"]) == 0
    assert "pruned 2 jobs" in capsys.readouterr().out
    with stored.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM jobs WHERE raw_digest IS NOT NULL")).scalar_one() == 2

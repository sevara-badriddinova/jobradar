"""Guard bandwidth reductions while preserving eligibility and dedup semantics."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select, update

from jobradar import cli
from jobradar.collectors import COLLECTORS
from jobradar.companies import CompanySpec, list_companies, sync_companies
from jobradar.db import jobs
from jobradar.filters import run_filters
from jobradar.matcher import run_matching
from jobradar.query_fields import location_raw
from jobradar.settings import FilterSettings, Preferences
from tests.factories import make_job
from tests.integration.helpers import ingest, row
from tests.integration.test_matcher import add_profile, llm


@pytest.fixture
def statements(engine):
    queries = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        queries.append((statement, cursor.rowcount))
    event.listen(engine, "after_cursor_execute", capture)
    yield queries
    event.remove(engine, "after_cursor_execute", capture)


def test_scan_limit_transfers_only_requested_boards(engine, statements):
    with engine.begin() as conn:
        sync_companies(conn, [CompanySpec(name=f"Company {i:03}", collector="greenhouse", board=f"c{i}") for i in range(100)])
        statements.clear()
        assert len(list_companies(conn, limit=25)) == 25
        assert statements[-1][1] == 25
        assert "LIMIT" in statements[-1][0]
        assert [r.board for r in list_companies(conn, names=["c90"], limit=1)] == ["c90"]


def test_raw_changes_detected_without_downloading_stored_payload(engine, statements):
    job = make_job("a").model_copy(update={"raw": {"id": "a", "html": "x" * 100_000}})
    ingest(engine, [job])
    statements.clear()
    assert ingest(engine, [job]).changed == 0
    comparisons = [s for s, _ in statements if "raw_changed" in s]
    assert comparisons and "IS DISTINCT FROM" in comparisons[0]
    assert "jobs.raw," not in comparisons[0]
    changed = job.model_copy(update={"raw": {**job.raw, "country": "CA"}})
    assert ingest(engine, [changed]).changed == 1
    assert row(engine, "a").raw["country"] == "CA"
    assert ingest(engine, [changed]).changed == 0


@pytest.mark.parametrize("source,raw", [
    ("greenhouse", {"offices": [{"name": "USA"}], "content": "x" * 100_000}),
    ("lever", {"categories": {"allLocations": ["NY, NY"]}, "country": "US", "description": "x" * 100_000}),
    ("ashby", {"address": {"postalAddress": {"addressCountry": "US"}}, "secondaryLocations": [{"location": "Canada"}], "descriptionHtml": "x" * 100_000}),
    ("workday", {"jobPostingInfo": {"country": "US", "additionalLocations": ["Seattle"], "jobDescription": "x" * 100_000}}),
    ("workable", {"locations": ["US"], "description": "x" * 100_000}),
    ("recruitee", {"locations": ["Remote"], "country_code": "US", "description": "x" * 100_000}),
    ("rippling", {"workLocations": ["US"], "publicationMetadata": {"jobLocation": ["CA"], "description": "x" * 100_000}}),
    ("personio", {"additionalOffices": {"office": ["US"]}, "publicationMetadata": {"jobLocation": ["CA"]}, "jobDescriptions": "x" * 100_000}),
])
def test_compact_metadata_preserves_every_collector_location(engine, source, raw):
    ingest(engine, [make_job("a", source=source).model_copy(update={"raw": raw})], source=source)
    with engine.connect() as conn:
        compact = conn.execute(select(location_raw())).scalar_one()
    assert COLLECTORS[source].locations(compact) == COLLECTORS[source].locations(raw)
    assert len(json.dumps(compact)) < len(json.dumps(raw)) / 100


def test_filter_skips_old_titles_and_unchanged_bodies_but_expires_jobs(engine, statements):
    now = datetime.now(UTC)
    ingest(engine, [make_job("recent", title="Backend Engineer", posted_at=now),
                   make_job("title", title="Senior Engineer", posted_at=now),
                   make_job("old", title="Platform Engineer", posted_at=now-timedelta(days=4))])
    policy = FilterSettings(max_posting_age_days=3)
    with engine.begin() as conn:
        statements.clear()
        assert run_filters(conn, policy).passed == 1
        bodies = [(s, n) for s, n in statements if s.startswith("SELECT") and "jobs.description" in s]
        assert sum(n for _, n in bodies) == 1
        statements.clear()
        assert run_filters(conn, policy).passed == 0
        assert not any(s.startswith("SELECT") and "jobs.description" in s for s, _ in statements)
        conn.execute(update(jobs).where(jobs.c.source_job_id == "recent").values(posted_at=now-timedelta(days=4)))
        assert run_filters(conn, policy).rejected.total() == 1
    assert row(engine, "recent").status == "FILTERED"


def test_future_publication_becomes_eligible_without_content_change(engine):
    now = datetime.now(UTC)
    ingest(engine, [make_job("a", posted_at=now+timedelta(days=1))])
    policy = FilterSettings(max_posting_age_days=3)
    with engine.begin() as conn:
        run_filters(conn, policy)
        conn.execute(update(jobs).values(posted_at=now))  # passage of time, no ingest invalidation
        assert run_filters(conn, policy).passed == 1
    assert row(engine, "a").status == "DISCOVERED"


def test_notified_job_is_rechecked_when_its_requirements_change(engine):
    job = make_job("a", posted_at=datetime.now(UTC), description="We provide visa sponsorship.")
    ingest(engine, [job])
    policy = FilterSettings(max_posting_age_days=3, require_explicit_sponsorship=True)
    with engine.begin() as conn:
        run_filters(conn, policy)
        conn.execute(update(jobs).values(status="NOTIFIED"))
    ingest(engine, [job.model_copy(update={"description": "We do not provide visa sponsorship."})])
    with engine.begin() as conn:
        assert run_filters(conn, policy).rejected.total() == 1
    assert row(engine, "a").status == "FILTERED"


def test_matching_budget_limits_returned_candidates(engine, statements, llm):
    from tests.factories import assessment
    ingest(engine, [make_job(str(i), title=f"Backend Engineer {i}") for i in range(30)])
    with engine.begin() as conn:
        run_filters(conn, FilterSettings())
    profile = add_profile(engine)
    llm(assessment())
    statements.clear()
    assert run_matching(engine, profile, Preferences(), limit=2).assessed == 2
    candidates = [(s, n) for s, n in statements if s.startswith("SELECT") and "ORDER BY jobs.status" in s]
    assert len(candidates) == 1 and candidates[0][1] == 2


def test_daily_directory_cache_survives_new_worker_filesystem(engine, db_env, monkeypatch, tmp_path):
    from jobradar import discovery
    from jobradar.discovery import SUPPORTED
    registry = tmp_path / "companies.toml"
    registry.write_text('[[company]]\nname="Acme"\ncollector="greenhouse"\nboard="acme"\n')
    calls = []
    def refresh(*args, **kwargs):
        calls.append(1)
        return dict(registered_boards=1, unsupported_directory_boards={}, collector_platforms=sorted(SUPPORTED))
    monkeypatch.setattr(discovery, "refresh_directory", refresh)
    assert cli.main(["companies", "discover", "--file", str(registry)]) == 0
    registry.unlink()  # the next container has no downloaded registry
    assert cli.main(["companies", "discover", "--file", str(registry)]) == 0
    assert len(calls) == 1

"""30-day ingestion window: provider-specific, conservative, lossless for alerts."""

from datetime import UTC, datetime, timedelta, timezone

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from jobradar.collectors import build_collector
from jobradar.collectors import personio as personio_module
from jobradar.db import collector_runs, jobs as jobs_t, work_queue
from jobradar.pipeline import run_collector
from jobradar.repository import ingest_snapshot
from jobradar.settings import FilterSettings, IngestionSettings, Settings
from tests.ats_fixtures import (WORKDAY_BOARD, linked_data, personio_xml, recruitee_item, rippling_item,
                                workable_item, workday_detail, workday_item)
from tests.factories import ashby_job, greenhouse_job, lever_posting, make_job

NOW = datetime.now(UTC)
NEW, OLD = NOW - timedelta(days=2), NOW - timedelta(days=45)


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def stored(engine, source):
    with engine.connect() as conn:
        return sorted(conn.execute(select(jobs_t.c.source_job_id).where(jobs_t.c.source == source)).scalars())


def client(handler):
    calls = []

    def wrapped(request):
        calls.append(request)
        return handler(request)
    return calls, httpx.Client(transport=httpx.MockTransport(wrapped))


def collect(engine, source, board, handler, window=30):
    calls, c = client(handler)
    collector = build_collector(source, board, company="Acme", client=c)
    return calls, run_collector(engine, collector, ingest_max_age_days=window)


# --- every provider ------------------------------------------------------------------------

def gh(request):
    return httpx.Response(200, json={"jobs": [greenhouse_job(1, first_published=iso(NEW)),
                                              greenhouse_job(2, first_published=iso(OLD))]})


def ashby(request):
    return httpx.Response(200, json={"jobs": [ashby_job("new", publishedAt=iso(NEW)),
                                              ashby_job("old", publishedAt=iso(OLD))]})


def lever(request):
    return httpx.Response(200, json=[lever_posting("new", createdAt=int(NEW.timestamp() * 1000)),
                                     lever_posting("old", createdAt=int(OLD.timestamp() * 1000))])


def workable(request):
    return httpx.Response(200, json={"jobs": [{**workable_item("1"), "published_on": NEW.date().isoformat()},
                                              {**workable_item("2"), "published_on": OLD.date().isoformat()}]})


def recruitee(request):
    fmt = "%Y-%m-%d %H:%M:%S UTC"
    return httpx.Response(200, json={"offers": [{**recruitee_item("1"), "published_at": NEW.strftime(fmt)},
                                                {**recruitee_item("2"), "published_at": OLD.strftime(fmt)}]})


def rippling(request):
    if request.url.path.endswith("/jobs"):
        return httpx.Response(200, json=[{"uuid": "1"}, {"uuid": "2"}])
    i = request.url.path[-1]
    item = rippling_item(i)
    item["jsonLd"]["datePosted"] = iso(NEW if i == "1" else OLD)
    return httpx.Response(200, json=item)


def personio(request):
    if request.url.path == "/xml":
        return httpx.Response(200, text=personio_xml())
    return httpx.Response(200, text=linked_data(iso(NEW if request.url.path.endswith("/1") else OLD)))


def workday_pages(request):
    """Two pages; details exist only for postings Workday labels as fresh/unknown."""
    if request.method == "POST":
        import json
        offset = json.loads(request.content)["offset"]
        items = [workday_item("1", "Posted Today"), workday_item("2", "Posted 30+ Days Ago"),
                 workday_item("3", "Posted 12 Days Ago"), workday_item("4", "Publié il y a 40 jours")]
        page = items[offset:offset + 2]
        return httpx.Response(200, json={"total": 4, "jobPostings": page})
    i = request.url.path.rsplit("_R", 1)[-1]
    detail = workday_detail(i)
    detail["jobPostingInfo"]["startDate"] = iso(NEW if i == "1" else OLD)
    return httpx.Response(200, json=detail)


@pytest.mark.parametrize("source,board,handler,kept", [
    ("greenhouse", "acme", gh, ["1"]),                      # first_published: true first publication
    ("ashby", "acme", ashby, ["new"]),                      # publishedAt: latest publication
    ("workable", "acme", workable, ["1"]),                  # date-only published_on
    ("recruitee", "acme", recruitee, ["1"]),                # "YYYY-mm-dd HH:MM:SS UTC"
    ("rippling", "acme", rippling, ["1"]),                  # JSON-LD datePosted from details
    ("personio", "acme", personio, ["acme:1"]),             # JSON-LD datePosted from detail pages
    ("lever", "acme", lever, ["new", "old"]),               # createdAt may precede publication: keep
])
def test_each_provider_skips_only_trustworthy_old_postings(engine, source, board, handler, kept):
    _, report = collect(engine, source, board, handler)
    assert report.ok and stored(engine, source) == sorted(kept)
    assert report.result.skipped_old == 2 - len(kept)


def test_workday_pagination_labels_and_unknown_dates(engine):
    calls, report = collect(engine, "workday", WORKDAY_BOARD, workday_pages)
    ids = [s.rsplit("_R", 1)[-1] for s in stored(engine, "workday")]
    # 1: fresh, dated. 2: "30+ days" label proves age >= window. 3: undated 12-day label is
    # stored (cannot prove it is old). 4: localized label -> detail fetched -> dated old.
    assert sorted(ids) == ["1", "3"] and report.result.skipped_old == 2
    assert sum(r.method == "POST" for r in calls) == 2                       # both pages read
    assert sorted(r.url.path.rsplit("_R", 1)[-1] for r in calls if r.method == "GET") == ["1", "4"]


def test_wider_window_keeps_workday_thirty_plus_label(engine):
    _, report = collect(engine, "workday", WORKDAY_BOARD, workday_pages, window=60)
    # "30+ days" cannot prove >= 60 days, and the dated posting is 45 days old: all stored.
    assert len(stored(engine, "workday")) == 4 and report.result.skipped_old == 0


def test_no_window_keeps_everything(engine):
    _, report = collect(engine, "greenhouse", "acme", gh, window=None)
    assert stored(engine, "greenhouse") == ["1", "2"] and report.result.skipped_old == 0


# --- boundaries, time zones, unknown and future dates -----------------------------------------

@pytest.mark.parametrize("posted,kept", [
    (NOW - timedelta(days=30, hours=23), True),             # inside the one-day margin
    (NOW - timedelta(days=31, hours=1), False),
    ((NOW - timedelta(days=31, hours=1)).astimezone(timezone(timedelta(hours=-4))), False),
    ((NOW - timedelta(days=30, hours=20)).astimezone(timezone(timedelta(hours=9))), True),
    (None, True),                                            # unknown date: never discarded
    (NOW + timedelta(days=2), True),                         # future date: left to the filters
])
def test_boundaries_and_time_zones(engine, posted, kept):
    collector = build_collector("greenhouse", "acme", company="Acme")
    cutoff = NOW - timedelta(days=30)
    job = make_job("x", posted_at=None).model_copy(update={"posted_at": posted})
    with engine.begin() as conn:
        result = ingest_snapshot(conn, source="greenhouse", board="acme", jobs=[job],
                                 skip_new=lambda j: collector.known_older_than(j, cutoff))
    assert (stored(engine, "greenhouse") == ["x"]) is kept and result.skipped_old == (0 if kept else 1)


# --- history, close detection, reposts, re-enqueueing ----------------------------------------

def test_known_old_jobs_stay_tracked_and_are_never_closed_by_skipping(engine):
    collect(engine, "greenhouse", "acme", gh, window=None)                 # stored before the window
    _, report = collect(engine, "greenhouse", "acme", gh)                  # now with the window
    with engine.connect() as conn:
        rows = conn.execute(select(jobs_t.c.source_job_id, jobs_t.c.closed_at)).all()
    assert sorted(r.source_job_id for r in rows) == ["1", "2"] and all(r.closed_at is None for r in rows)
    assert report.result.skipped_old == 0 and report.result.seen == 2


def test_skipped_postings_never_trigger_close_detection_of_others(engine):
    # Board lists a stored recent job plus an old never-seen one: nothing closes.
    collect(engine, "greenhouse", "acme", lambda r: httpx.Response(200, json={"jobs": [
        greenhouse_job(1, first_published=iso(NEW))]}))
    _, report = collect(engine, "greenhouse", "acme", gh)
    assert report.result.closed == 0 and stored(engine, "greenhouse") == ["1"]


def test_republished_posting_is_new_once_its_publication_date_is_recent(engine):
    collect(engine, "ashby", "acme", ashby)
    assert stored(engine, "ashby") == ["new"]
    republished = lambda r: httpx.Response(200, json={"jobs": [  # noqa: E731
        ashby_job("new", publishedAt=iso(NEW)), ashby_job("old", publishedAt=iso(NOW - timedelta(hours=3)))]})
    _, report = collect(engine, "ashby", "acme", republished)
    assert stored(engine, "ashby") == ["new", "old"] and report.result.new == 1


def test_old_update_timestamps_do_not_make_a_job_new(engine):
    # Greenhouse updated_at changes on edits; only first_published counts.
    edited = lambda r: httpx.Response(200, json={"jobs": [  # noqa: E731
        greenhouse_job(2, first_published=iso(OLD), updated_at=iso(NOW))]})
    _, report = collect(engine, "greenhouse", "acme", edited)
    assert stored(engine, "greenhouse") == [] and report.result.skipped_old == 1


def test_settings_require_the_window_to_cover_the_alert_window():
    with pytest.raises(ValidationError, match="ingestion"):
        Settings(filters=FilterSettings(max_posting_age_days=7), ingestion=IngestionSettings(max_age_days=3))
    assert Settings().ingestion.max_age_days == 30
    assert Settings(ingestion=IngestionSettings(max_age_days=None)).ingestion.max_age_days is None


# --- Personio: reuse stored publication dates instead of re-fetching -------------------------

def test_personio_reuses_known_dates_and_reverifies_a_sample(engine, monkeypatch):
    calls, _ = collect(engine, "personio", "acme", personio, window=None)
    assert sum(r.url.path != "/xml" for r in calls) == 2                    # first poll: both details
    monkeypatch.setattr(personio_module.random, "random", lambda: 0.99)     # no re-verification draw
    calls, report = collect(engine, "personio", "acme", personio, window=None)
    assert [r.url.path for r in calls] == ["/xml"] and report.result.changed == 0
    monkeypatch.setattr(personio_module.random, "random", lambda: 0.0)      # sampled for re-check
    calls, _ = collect(engine, "personio", "acme", personio, window=None)
    assert sum(r.url.path != "/xml" for r in calls) == 2


# --- the worker -------------------------------------------------------------------------------

def test_worker_skips_old_postings_and_queues_only_stored_ones(engine):
    from jobradar import queue as q
    from tests.integration.test_workers import SETTINGS, add_board, make_worker
    add_board(engine, "acme")
    w = make_worker(engine, None, settings=SETTINGS)
    _, w.http = client(gh)
    w.http.event_hooks.setdefault("request", []).append(w.limiter.hook)
    w.collector_factory = build_collector
    with engine.begin() as conn:
        q.seed_boards(conn)
    [board] = w.claim_boards(1)
    assert w.collect_board(board) == "ok"
    assert w.counters["ingest_skipped_old"] == 1 and stored(engine, "greenhouse") == ["1"]
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(work_queue)).scalar_one() == 1
        assert conn.execute(select(func.count()).select_from(collector_runs)).scalar_one() == 1  # one change


# --- Workday: stable change detection across polls ------------------------------------------

def workday_board(labels):
    """One page whose labels the test changes between polls; details count requests."""
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"total": len(labels), "jobPostings": [
                workday_item(i, label) for i, label in labels.items()]})
        return httpx.Response(200, json=workday_detail(request.url.path.rsplit("_R", 1)[-1]))
    return handler


def test_workday_label_drift_is_not_a_change_and_fresh_details_are_kept(engine):
    labels = {"1": "Posted 3 Days Ago", "2": "Posted 10 Days Ago"}
    calls, first = collect(engine, "workday", WORKDAY_BOARD, workday_board(labels))
    assert first.result.new == 2 and sum(r.method == "GET" for r in calls) == 1   # only the fresh one
    before = {r.source_job_id: r for r in _rows(engine)}

    labels.update({"1": "Posted 4 Days Ago", "2": "Posted 11 Days Ago"})          # a day later
    calls, second = collect(engine, "workday", WORKDAY_BOARD, workday_board(labels))
    after = {r.source_job_id: r for r in _rows(engine)}
    assert second.result.changed == 0 and sum(r.method == "GET" for r in calls) == 0
    fresh = next(k for k in after if k.endswith("_R1"))
    # Details fetched while fresh survive: description and publication date are not erased.
    assert after[fresh].description == before[fresh].description != ""
    assert after[fresh].posted_at == before[fresh].posted_at is not None
    assert all(after[k].filtered_at == before[k].filtered_at for k in after)


def test_workday_real_change_is_still_detected(engine):
    labels = {"2": "Posted 10 Days Ago"}
    collect(engine, "workday", WORKDAY_BOARD, workday_board(labels))
    def renamed(request):
        item = workday_item("2", "Posted 11 Days Ago")
        item["title"] = "Senior Software Engineer 2"
        return httpx.Response(200, json={"total": 1, "jobPostings": [item]})
    _, report = collect(engine, "workday", WORKDAY_BOARD, renamed)
    assert report.result.changed == 1


def _rows(engine):
    with engine.connect() as conn:
        return conn.execute(select(jobs_t)).all()

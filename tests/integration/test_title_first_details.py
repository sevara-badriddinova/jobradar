"""Title-first detail fetching and early listing digests (Workday, Rippling, Personio).

Production: big Workday boards (CVS Health, cw) exhausted the shared 64 MB
byte budget fetching details for hundreds of fresh postings, logged "detail
unavailable: byte budget exhausted" and were retried every minute without ever
finishing. ~93% of postings are rejected by title anyway.
"""

import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select, text

from jobradar import queue as q
from jobradar.collectors import build_collector
from jobradar.collectors.base import UnchangedSnapshot
from jobradar.db import board_state, jobs as jobs_t
from jobradar.filters import title_reason
from jobradar.http import ByteBudget, byte_bounded
from jobradar.pipeline import run_collector
from jobradar.settings import FilterSettings
from tests.ats_fixtures import DATE, WORKDAY_BOARD, linked_data, rippling_item, workday_detail
from tests.integration.test_workers import FakeBoards, add_board, make_worker

FILTERS = FilterSettings()


def wants(title):
    return title_reason(title, FILTERS) is None


TITLES = ["Software Engineer", "Account Manager", "Senior Software Engineer", "Payroll Specialist",
          "Backend Developer"]


def workday_listing(n, posted="Posted Today"):
    return [{"title": f"{TITLES[i % len(TITLES)]} {i}", "externalPath": f"/job/USA/Role_R{i}",
             "locationsText": "New York, NY", "postedOn": posted, "bulletFields": [f"R{i}"]}
            for i in range(n)]


def workday_server(n=45, posted="Posted Today", pad=4000, fail_detail=()):
    calls = {"search": 0, "detail": [], "offsets": []}
    listing = workday_listing(n, posted)

    def handle(request):
        if request.method == "POST":
            calls["search"] += 1
            body = json.loads(request.content)
            calls["offsets"].append(body["offset"])
            page = listing[body["offset"]:body["offset"] + body["limit"]]
            return httpx.Response(200, json={"total": n, "jobPostings": page})
        i = request.url.path.rsplit("_R", 1)[-1]
        calls["detail"].append(i)
        if i in fail_detail:
            return httpx.Response(404)
        item = next(x for x in listing if x["externalPath"].endswith(f"_R{i}"))
        detail = workday_detail(i)
        detail["jobPostingInfo"]["title"] = item["title"]
        detail["jobPostingInfo"]["jobDescription"] += "x" * pad
        return httpx.Response(200, json=detail)
    return calls, httpx.Client(transport=httpx.MockTransport(handle))


def workday(client, **attrs):
    c = build_collector("workday", WORKDAY_BOARD, company="Acme", client=client)
    for k, v in attrs.items():
        setattr(c, k, v)
    return c


# --- Workday --------------------------------------------------------------------------

def test_details_only_for_relevant_titles_across_pages_and_no_posting_lost():
    calls, client = workday_server(n=45)                 # 3 pages of 20
    jobs = workday(client, wants_detail=wants).collect()
    assert calls["offsets"] == [0, 20, 40]
    assert len(jobs) == 45                               # every listing posting is still ingested
    relevant = {j.source_job_id for j in jobs if wants(j.title)}
    fetched = {f"{WORKDAY_BOARD}:/job/USA/Role_R{i}" for i in calls["detail"]}
    assert fetched == relevant and len(relevant) == 18   # 2 of every 5 titles pass
    by_id = {j.source_job_id: j for j in jobs}
    assert all(by_id[s].posted_at is not None and by_id[s].description for s in relevant)
    skipped = [j for j in jobs if j.source_job_id not in relevant]
    assert all(j.posted_at is None and j.description == "" for j in skipped)


def test_legacy_callers_without_a_title_policy_fetch_every_fresh_detail():
    calls, client = workday_server(n=10)
    assert len(workday(client).collect()) == 10
    assert len(calls["detail"]) == 10


def test_unchanged_listing_is_skipped_before_any_detail_request():
    calls, client = workday_server(n=45)
    first = workday(client, wants_detail=wants)
    first.collect()
    digest = first.snapshot_digest
    assert digest is not None
    calls["detail"].clear()
    # The daily-changing "Posted N Days Ago" label is volatile.
    calls2, client2 = workday_server(n=45, posted="Posted Yesterday")
    with pytest.raises(UnchangedSnapshot):
        workday(client2, wants_detail=wants, known_digest=digest).collect()
    assert calls2["search"] == 3 and calls2["detail"] == []


def test_a_new_posting_changes_the_listing_digest():
    _, client = workday_server(n=45)
    first = workday(client, wants_detail=wants)
    first.collect()
    calls, client2 = workday_server(n=46)
    jobs = workday(client2, wants_detail=wants, known_digest=first.snapshot_digest).collect()
    assert len(jobs) == 46 and calls["detail"]


def test_partial_hydration_never_stores_a_complete_digest():
    calls, client = workday_server(n=10, fail_detail={"0"})
    c = workday(client, wants_detail=wants)
    jobs = c.collect()
    assert not c.complete_snapshot and c.snapshot_digest is None
    assert all(not j.source_job_id.endswith("_R0") for j in jobs)   # never ingested half-baked


def test_previously_hydrated_details_are_kept_when_a_title_becomes_irrelevant():
    calls, client = workday_server(n=5)
    c = workday(client, wants_detail=lambda t: False)
    sid = f"{WORKDAY_BOARD}:/job/USA/Role_R0"
    c.known = {sid: {"jobPostingInfo": workday_detail("0")["jobPostingInfo"]}}
    jobs = {j.source_job_id: j for j in c.collect()}
    assert calls["detail"] == [] and jobs[sid].posted_at is not None and jobs[sid].description


def test_title_first_fits_a_budget_that_full_hydration_exceeds():
    """A concurrent collection holds 250 KB of a 600 KB budget (as on the VM).
    200 fresh postings with ~2.3 KB details, bytes held for the whole poll:
    full hydration (~500 KB) runs into backpressure; title-first (80 details,
    ~225 KB) finishes."""
    def run(wants_detail):
        calls, client = workday_server(n=200, pad=2000)
        budget = ByteBudget(600_000)
        assert budget.acquire(250_000, 0)
        with byte_bounded(budget, max_response=100_000, wait=0.01) as meter:
            try:
                jobs = workday(client, wants_detail=wants_detail).collect()
            except Exception:
                jobs = None
        return calls, meter, jobs
    calls, meter, jobs = run(wants)
    assert len(jobs) == 200 and not meter.backpressure and len(calls["detail"]) == 80
    _, meter_all, _ = run(None)
    assert meter_all.backpressure


# --- through run_collector and the worker ------------------------------------------------

def test_title_policy_change_rehydrates_newly_relevant_titles(engine):
    calls, client = workday_server(n=10)
    strict = lambda t: "Backend" in t                      # noqa: E731
    r1 = run_collector(engine, workday(client), record_unchanged=False,
                       wants_detail=strict, detail_policy="strict")
    assert r1.ok and r1.snapshot_hash
    assert len(calls["detail"]) == 2
    calls["detail"].clear()
    same = run_collector(engine, workday(client), record_unchanged=False, known_snapshot=r1.snapshot_hash,
                         wants_detail=strict, detail_policy="strict")
    assert same.unchanged and calls["detail"] == []
    wider = run_collector(engine, workday(client), record_unchanged=False, known_snapshot=r1.snapshot_hash,
                          wants_detail=wants, detail_policy="default")
    assert not wider.unchanged and sorted(calls["detail"], key=int) == ["0", "4", "5", "9"]
    with engine.connect() as conn:
        dated = conn.execute(select(func.count()).select_from(jobs_t)
                             .where(jobs_t.c.posted_at.is_not(None))).scalar_one()
    assert dated == 4                                      # 2 strict + 2 newly relevant


def test_collectors_without_title_first_keep_their_digest_salt(engine):
    """Adding the policy to every collector's salt would force a full re-ingest
    of all 12k boards on deploy; only title-first collectors carry it."""
    boards = FakeBoards()
    boards.snapshots["acme"] = []
    plain = boards("greenhouse", "acme")
    run_collector(engine, plain, record_unchanged=False, wants_detail=wants, detail_policy="p")
    assert json.loads(plain.digest_salt) == {"max_age_days": None}
    _, client = workday_server(n=2)
    wd = workday(client)
    run_collector(engine, wd, record_unchanged=False, wants_detail=wants, detail_policy="p")
    assert json.loads(wd.digest_salt) == {"max_age_days": None, "detail_policy": "p"}
    assert wd.wants_detail is wants


def test_worker_collects_a_big_workday_board_within_the_budget(engine):
    calls, client = workday_server(n=200, pad=2000)
    add_board(engine, "acme-wd", collector="workday")
    with engine.begin() as conn:
        conn.execute(text("UPDATE companies SET board = :b"), {"b": WORKDAY_BOARD})
    w = make_worker(engine, FakeBoards(), byte_budget=600_000, byte_wait=0.05)
    w.collector_factory = build_collector
    w.http = client
    w.limiter.rates["myworkdayjobs.com"] = 1000.0
    with engine.begin() as conn:
        q.seed_boards(conn)
    [board] = w.claim_boards(1)
    assert w.budget.acquire(250_000, 0)          # another collection holds part of the budget
    assert w.collect_board(board) == "ok"        # full hydration here would be "backpressure"
    w.budget.release(250_000)
    assert len(calls["detail"]) == 80 and w.budget.used == 0
    with engine.connect() as conn:
        n = conn.execute(select(func.count()).select_from(jobs_t)).scalar_one()
        state = conn.execute(select(board_state)).one()
    assert n == 200 and state.snapshot_hash is not None


# --- Rippling and Personio ---------------------------------------------------------------

def rippling_client(listing, requests):
    def handle(request):
        requests.append(request.url.path)
        if request.url.path.endswith("/jobs"):
            return httpx.Response(200, json=listing)
        return httpx.Response(200, json=rippling_item(request.url.path.rsplit("/", 1)[-1]))
    return httpx.Client(transport=httpx.MockTransport(handle))


def test_rippling_never_skips_details_so_stored_postings_keep_their_details():
    """Rippling has no stored-detail reuse: skipping a detail would overwrite an
    existing posting's description and date with listing-only fields."""
    requests = []
    listing = [{"uuid": "1", "name": "Software Engineer"}, {"uuid": "2", "name": "Account Manager"}]
    c = build_collector("rippling", "acme", company="Acme", client=rippling_client(listing, requests))
    c.wants_detail = wants
    jobs = {j.source_job_id: j for j in c.collect()}
    assert sorted(p.rsplit("/", 1)[-1] for p in requests if not p.endswith("/jobs")) == ["1", "2"]
    assert all(j.posted_at is not None and j.description for j in jobs.values())


def test_rippling_unchanged_listing_skips_every_detail():
    listing = [{"uuid": "1", "name": "Software Engineer"}, {"uuid": "2", "name": "Account Manager"}]
    first = build_collector("rippling", "acme", company="Acme", client=rippling_client(listing, []))
    first.collect()
    requests = []
    again = build_collector("rippling", "acme", company="Acme", client=rippling_client(listing, requests))
    again.known_digest = first.snapshot_digest
    with pytest.raises(UnchangedSnapshot):
        again.collect()
    assert requests == ["/platform/api/ats/v1/board/acme/jobs"]


def test_personio_skips_dating_rejected_titles():
    xml = '<workzag-jobs>' + ''.join(f'''<position><id>{i}</id><name>{name}</name><office>NY</office>
        <jobDescriptions><jobDescription><name>Role</name><value>x</value></jobDescription></jobDescriptions>
        </position>''' for i, name in [("1", "Software Engineer"), ("2", "Payroll Specialist")]) + '</workzag-jobs>'
    pages = []

    def handle(request):
        if request.url.path == "/xml":
            return httpx.Response(200, text=xml)
        pages.append(request.url.path)
        return httpx.Response(200, text=linked_data())
    c = build_collector("personio", "acme", company="Acme",
                        client=httpx.Client(transport=httpx.MockTransport(handle)))
    c.wants_detail = wants
    jobs = {j.title: j for j in c.collect()}
    assert pages == ["/job/1"] and jobs["Payroll Specialist"].posted_at is None

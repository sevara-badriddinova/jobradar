"""Amazon, Oracle Recruiting Cloud (Oracle, JPMorgan) and Eightfold (Microsoft,
Netflix) collectors: pagination, dates, de-duplication, closures, title-first."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from jobradar.collectors import COLLECTORS, CollectorError
from jobradar.collectors.base import UnchangedSnapshot
from jobradar.collectors.eightfold import decode_board as ef_decode, encode_board as ef_encode, publication_time
from jobradar.collectors.oracle_hcm import decode_board as or_decode, encode_board as or_encode
from jobradar.filters import sponsorship_evidence, title_reason
from jobradar.recency import publication_reason
from jobradar.settings import FilterSettings
from tests.ats_fixtures import (EIGHTFOLD_BOARD, EIGHTFOLD_V2_BOARD, ORACLE_BOARD, TS, amazon_item, amazon_page,
                                eightfold_detail, eightfold_item, oracle_detail, oracle_item, oracle_page)

NOW = datetime.now(UTC)
TODAY = NOW.strftime("%Y-%m-%d")
TODAY_LONG = NOW.strftime("%B %d, %Y").replace(" 0", " ")
FILTERS = FilterSettings()


def wants(title):
    return title_reason(title, FILTERS) is None


def make(source, handler, board, **attrs):
    c = COLLECTORS[source](board, company="Acme", client=httpx.Client(transport=httpx.MockTransport(handler)))
    for k, v in attrs.items():
        setattr(c, k, v)
    return c


# --- Amazon ------------------------------------------------------------------------------

def amazon_server(listing, calls=None, fail_offsets=(), hits=None):
    calls = calls if calls is not None else []

    def handle(request):
        p = request.url.params
        assert p["category[]"] == "software-development" and p["normalized_country_code[]"] == "USA"
        assert p["sort"] == "recent" and int(p["result_limit"]) <= 100
        offset = int(p["offset"])
        calls.append(offset)
        if offset in fail_offsets:
            return httpx.Response(404)
        return httpx.Response(200, json=amazon_page(listing[offset:offset + 100],
                                                    hits=len(listing) if hits is None else hits))
    return handle, calls


def test_amazon_paginates_with_overlap_and_deduplicates():
    listing = [amazon_item(str(i)) for i in range(250)]
    handle, calls = amazon_server(listing)
    jobs = make("amazon", handle, "software-development").collect()
    assert calls == [0, 90, 180]                       # 10-entry overlap between pages
    assert len(jobs) == 250 and len({j.source_job_id for j in jobs}) == 250


def test_amazon_removal_between_pages_is_absorbed_by_the_overlap():
    listing = [amazon_item(str(i)) for i in range(150)]
    calls = []

    def handle(request):
        offset = int(request.url.params["offset"])
        calls.append(offset)
        current = listing if offset == 0 else listing[:5] + listing[8:]   # 3 removed after page one
        return httpx.Response(200, json=amazon_page(current[offset:offset + 100], hits=len(current)))
    c = make("amazon", handle, "software-development")
    ids = {j.raw["id_icims"] for j in c.collect()}
    assert ids >= {str(i) for i in range(8, 150)}       # nothing after the removal was skipped
    assert c.complete_snapshot


def test_amazon_dates_description_location_and_policy_inputs():
    handle, _ = amazon_server([amazon_item("7", posted="October 12, 2026")])
    c = make("amazon", handle, "software-development")
    j = c.collect()[0]
    assert j.posted_at == datetime(2026, 10, 12, tzinfo=UTC)
    assert j.url == "https://www.amazon.jobs/en/jobs/7/software-engineer"
    assert j.source_job_id == "software-development:7" and j.company == "Acme"
    assert sponsorship_evidence(j.description) and "Basic qualifications" in j.description
    assert "3+ years" in j.description                  # experience filter sees qualifications
    assert "USA" in c.locations(j.raw) and "New York" in c.locations(j.raw)
    assert publication_reason(j.posted_at, 3, now=j.posted_at + timedelta(days=4, seconds=1))


def test_amazon_volatile_labels_do_not_change_the_snapshot():
    a, b = amazon_item("1"), amazon_item("1")
    b["updated_time"], b["team"] = "about 16 hours", {"label": "other"}
    first = make("amazon", amazon_server([a])[0], "software-development")
    first.collect()
    second = make("amazon", amazon_server([b])[0], "software-development", known_digest=first.snapshot_digest)
    with pytest.raises(UnchangedSnapshot):
        second.collect()
    assert "updated_time" not in first.collect()[0].raw


@pytest.mark.parametrize("posted", [None, "", "12/10/2026", "Octember 1, 2026"])
def test_amazon_unparseable_date_is_undated_never_fresh(posted):
    handle, _ = amazon_server([amazon_item("1", posted=posted)])
    j = make("amazon", handle, "software-development").collect()[0]
    assert j.posted_at is None


def test_amazon_failed_later_page_keeps_jobs_but_never_closes():
    handle, calls = amazon_server([amazon_item(str(i)) for i in range(150)], fail_offsets=(90,))
    c = make("amazon", handle, "software-development")
    assert len(c.collect()) == 100 and not c.complete_snapshot


def test_amazon_fewer_unique_postings_than_reported_is_incomplete():
    handle, _ = amazon_server([amazon_item("1"), amazon_item("2")], hits=3)
    c = make("amazon", handle, "software-development")
    assert len(c.collect()) == 2 and not c.complete_snapshot


def test_amazon_beyond_the_offset_cap_is_incomplete(monkeypatch):
    monkeypatch.setattr(COLLECTORS["amazon"], "max_items", 150)
    handle, calls = amazon_server([amazon_item(str(i)) for i in range(300)])
    c = make("amazon", handle, "software-development")
    c.collect()
    assert not c.complete_snapshot and max(calls) < 150


def test_amazon_error_payload_and_bad_board():
    c = make("amazon", lambda r: httpx.Response(200, json={"error": "Result limit cannot be greater than 100",
                                                           "hits": 0, "jobs": []}), "software-development")
    with pytest.raises(CollectorError):
        c.collect()
    with pytest.raises(CollectorError):
        make("amazon", lambda r: httpx.Response(200), "../x").collect()


def test_amazon_empty_category_is_a_valid_empty_board():
    c = make("amazon", lambda r: httpx.Response(200, json=amazon_page([], hits=0)), "software-development")
    assert c.collect() == [] and c.complete_snapshot


# --- Oracle Recruiting Cloud -------------------------------------------------------------

def oracle_server(listing, details=None, calls=None, fail_detail=()):
    calls = calls if calls is not None else {"offsets": [], "detail": [], "finders": []}
    details = details or {}

    def handle(request):
        finder = request.url.params["finder"]
        calls["finders"].append(finder)
        if request.url.path.endswith("Details"):
            i = finder.split('"')[1]
            calls["detail"].append(i)
            if i in fail_detail:
                return httpx.Response(404)
            return httpx.Response(200, json=details.get(i) or oracle_detail(i))
        assert request.url.params["expand"] == "requisitionList.secondaryLocations"
        fields = dict(f.split("=", 1) for f in finder.split(";", 1)[1].split(","))
        offset, limit = int(fields["offset"]), int(fields["limit"])
        calls["offsets"].append(offset)
        return httpx.Response(200, json=oracle_page(listing[offset:offset + limit], total=len(listing), offset=offset))
    return handle, calls


def test_oracle_board_token_keeps_site_case_and_optional_category():
    board = or_encode("jpmc.fa", "CX_1001", "300000000289738", "300000086152753")
    assert board == "jpmc.fa:43585f31303031:300000000289738:300000086152753"
    assert or_decode(board) == ("jpmc.fa", "CX_1001", "300000000289738", "300000086152753")
    assert or_decode(or_encode("eeho.fa.us2", "CX_45001", "1"))[3] is None
    for bad in ("jpmc.fa:zz:1", "evil.com:43585f31303031:1", "jpmc.fa:43585f31303031:x", "jpmc.fa:43585f31303031"):
        with pytest.raises(CollectorError):
            or_decode(bad)


def test_oracle_finder_carries_site_location_category_and_paginates():
    listing = [oracle_item(str(i), posted=TODAY) for i in range(450)]
    handle, calls = oracle_server(listing)
    board = or_encode("acme.fa.us2", "CX_1001", "300000000149325", "42")
    c = make("oracle", handle, board, wants_detail=lambda t: False)
    jobs = c.collect()
    assert calls["offsets"] == [0, 190, 380]
    assert all("siteNumber=CX_1001,locationId=300000000149325,selectedCategoriesFacet=42" in f
               for f in calls["finders"])
    assert len(jobs) == 450 and c.complete_snapshot


def test_oracle_details_only_for_relevant_recent_titles():
    listing = [oracle_item("1", posted=TODAY), oracle_item("2", posted=TODAY, title="Payroll Specialist"),
               oracle_item("3", posted="2026-01-02")]
    handle, calls = oracle_server(listing)
    jobs = {j.raw["Id"]: j for j in make("oracle", handle, ORACLE_BOARD, wants_detail=wants).collect()}
    assert calls["detail"] == ["1"]
    assert jobs["1"].posted_at == datetime(2026, 10, 12, 12, tzinfo=UTC)     # exact detail time
    assert sponsorship_evidence(jobs["1"].description) and "3 years of Java" in jobs["1"].description
    assert jobs["2"].posted_at == datetime.strptime(TODAY, "%Y-%m-%d").replace(tzinfo=UTC)
    assert jobs["3"].posted_at == datetime(2026, 1, 2, tzinfo=UTC) and not jobs["3"].description
    assert jobs["1"].url == "https://acme.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/1"


def test_oracle_stored_details_are_reused_without_requests():
    handle, calls = oracle_server([oracle_item("1", posted=TODAY)])
    c = make("oracle", handle, ORACLE_BOARD, wants_detail=wants)
    first = c.collect()[0]
    again = make("oracle", handle, ORACLE_BOARD, wants_detail=wants)
    again.known = {first.source_job_id: {"detail": first.raw["detail"]}}
    assert again.collect()[0] == first and calls["detail"] == ["1"]


def test_oracle_expired_postings_are_excluded():
    past = (NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    listing = [oracle_item("1", posted=TODAY), oracle_item("2", posted=TODAY),
               oracle_item("3", posted="2026-01-01", end=(NOW - timedelta(days=2)).strftime("%Y-%m-%d")),
               oracle_item("4", posted="2026-01-01", end=NOW.strftime("%Y-%m-%d"))]
    handle, _ = oracle_server(listing, details={"2": oracle_detail("2", end=past)})
    ids = {j.raw["Id"] for j in make("oracle", handle, ORACLE_BOARD).collect()}
    assert ids == {"1", "4"}            # an end date of today is still open


def test_oracle_failed_detail_keeps_others_and_prevents_closure():
    handle, _ = oracle_server([oracle_item("1", posted=TODAY), oracle_item("2", posted=TODAY)], fail_detail=("2",))
    c = make("oracle", handle, ORACLE_BOARD)
    assert [j.raw["Id"] for j in c.collect()] == ["1"] and not c.complete_snapshot


def test_oracle_unchanged_listing_skips_every_detail():
    handle, calls = oracle_server([oracle_item("1", posted=TODAY)])
    first = make("oracle", handle, ORACLE_BOARD)
    first.collect()
    calls["detail"].clear()
    again = make("oracle", handle, ORACLE_BOARD, known_digest=first.snapshot_digest)
    with pytest.raises(UnchangedSnapshot):
        again.collect()
    assert calls["detail"] == []


@pytest.mark.parametrize("payload", [{"items": []}, {"items": [{"TotalJobsCount": "x", "requisitionList": []}]},
                                     [1, 2]])
def test_oracle_garbage_search_payload_fails(payload):
    with pytest.raises(CollectorError):
        make("oracle", lambda r: httpx.Response(200, json=payload), ORACLE_BOARD).collect()


# --- Eightfold ---------------------------------------------------------------------------

def eightfold_server(listing, mode="pcsx", details=None, fail_detail=()):
    calls = {"starts": [], "detail": [], "params": []}
    details = details or {}

    def handle(request):
        p = request.url.params
        assert p["domain"] == "acme.com"
        if request.url.path == "/api/pcsx/position_details":
            i = int(p["position_id"])
            calls["detail"].append(i)
            if i in fail_detail:
                return httpx.Response(404)
            return httpx.Response(200, json=details.get(i) or eightfold_detail(i))
        assert request.url.path == ("/api/pcsx/search" if mode == "pcsx" else "/api/apply/v2/jobs")
        calls["params"].append(dict(p))
        start = int(p["start"])
        calls["starts"].append(start)
        page = listing[start:start + 10]
        body = {"count": len(listing), "positions": page}
        return httpx.Response(200, json={"data": body} if mode == "pcsx" else body)
    return handle, calls


def test_eightfold_board_token_roundtrip_and_validation():
    board = ef_encode("apply.careers.microsoft.com", "microsoft.com", "pcsx",
                      {"location": "United States", "filter_profession": "software engineering"})
    assert " " not in board and "/" not in board and board == board.lower()
    host, domain, mode, query = ef_decode(board)
    assert (host, domain, mode) == ("apply.careers.microsoft.com", "microsoft.com", "pcsx")
    assert dict(query) == {"location": "United States", "filter_profession": "software engineering"}
    assert ef_decode(ef_encode("explore.jobs.netflix.net", "netflix.com", "v2"))[3] == []
    for bad in ("evil:microsoft.com:pcsx:", "a.com:b.com:xml:", "a.com:b.com:pcsx:" + "start=5".encode().hex()):
        with pytest.raises(CollectorError):
            ef_decode(bad)


def test_eightfold_pcsx_pages_of_ten_with_overlap_and_filters():
    listing = [eightfold_item(i, posted=int(NOW.timestamp())) for i in range(1, 26)]
    handle, calls = eightfold_server(listing)
    c = make("eightfold", handle, EIGHTFOLD_BOARD, wants_detail=lambda t: False)
    jobs = c.collect()
    assert calls["starts"] == [0, 9, 18]
    assert all(p["location"] == "United States" and p["sort_by"] == "timestamp" and p["query"] == ""
               for p in calls["params"])
    assert len(jobs) == 25 and len({j.source_job_id for j in jobs}) == 25 and c.complete_snapshot


def test_eightfold_v2_mode_asks_for_pages_without_a_query():
    listing = [{"id": i, "name": f"Software Engineer {i}", "locations": ["Los Gatos,California,United States"],
                "t_create": TS, "stars": 0} for i in range(1, 13)]
    handle, calls = eightfold_server(listing, mode="v2")
    jobs = make("eightfold", handle, EIGHTFOLD_V2_BOARD).collect()
    assert calls["starts"] == [0, 9] and all(p["num"] == "10" and "query" not in p for p in calls["params"])
    assert len(jobs) == 12 and sorted(calls["detail"]) == list(range(1, 13))
    assert jobs[0].posted_at == datetime.fromtimestamp(TS, UTC)


def test_eightfold_details_dates_and_title_first():
    fresh = int(NOW.timestamp())
    listing = [eightfold_item(1, posted=fresh, created=fresh - 86400),
               eightfold_item(2, posted=fresh, title="Account Executive"),
               eightfold_item(3, posted=fresh - 90 * 86400, created=fresh - 100 * 86400)]
    details = {1: eightfold_detail(1, posted=fresh, created=fresh - 86400)}
    handle, calls = eightfold_server(listing, details=details)
    c = make("eightfold", handle, EIGHTFOLD_BOARD, wants_detail=wants)
    jobs = {j.raw["id"]: j for j in c.collect()}
    assert calls["detail"] == [1]
    assert jobs[1].posted_at == datetime.fromtimestamp(fresh, UTC) and sponsorship_evidence(jobs[1].description)
    assert jobs[1].url == "https://careers.acme.com/careers/job/1"
    assert not jobs[2].description and not jobs[3].description
    assert "New York, NY, US" in c.locations(jobs[1].raw)


def test_eightfold_repost_of_an_old_requisition_is_dated_by_creation():
    fresh = int(NOW.timestamp())
    raw = {"id": 1, "detail": {"postedTs": fresh, "creationTs": fresh - 800 * 86400}}
    assert publication_time(raw) == datetime.fromtimestamp(fresh - 800 * 86400, UTC)
    raw["detail"]["creationTs"] = fresh - 21 * 86400           # normal approval-to-posting gap
    assert publication_time(raw) == datetime.fromtimestamp(fresh, UTC)
    assert publication_time({"id": 1}) is None


def test_eightfold_untrusted_public_url_falls_back_to_the_board_host():
    d = eightfold_detail(1, posted=int(NOW.timestamp()))
    d["data"]["publicUrl"] = "https://evil.example/careers/job/1"
    handle, _ = eightfold_server([eightfold_item(1, posted=int(NOW.timestamp()))], details={1: d})
    assert make("eightfold", handle, EIGHTFOLD_BOARD).collect()[0].url == "https://careers.acme.com/careers/job/1"


def test_eightfold_failed_or_mismatched_detail_prevents_closure():
    fresh = int(NOW.timestamp())
    wrong = eightfold_detail(9, posted=fresh)
    handle, _ = eightfold_server([eightfold_item(i, posted=fresh) for i in (1, 2, 3)],
                                 details={3: wrong}, fail_detail=(2,))
    c = make("eightfold", handle, EIGHTFOLD_BOARD)
    assert [j.raw["id"] for j in c.collect()] == [1] and not c.complete_snapshot


def test_eightfold_relevance_scores_do_not_change_the_snapshot():
    a = eightfold_item(1)
    b = dict(a, solrScore=9.9, stars=0.7)
    first = make("eightfold", eightfold_server([a])[0], EIGHTFOLD_BOARD)
    first.collect()
    second = make("eightfold", eightfold_server([b])[0], EIGHTFOLD_BOARD, known_digest=first.snapshot_digest)
    with pytest.raises(UnchangedSnapshot):
        second.collect()


def test_eightfold_ids_are_namespaced_by_board():
    handle, _ = eightfold_server([eightfold_item(1)])
    a = make("eightfold", handle, EIGHTFOLD_BOARD).collect()[0]
    other = ef_encode("careers.acme.com", "acme.com", "pcsx", {"location": "Canada"})
    b = make("eightfold", handle, other).collect()[0]
    assert a.source_job_id != b.source_job_id

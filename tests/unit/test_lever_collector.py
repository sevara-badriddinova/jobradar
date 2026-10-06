from datetime import datetime, timezone

import httpx
import pytest

from jobradar.collectors import CollectorError, LeverCollector
from tests.factories import RecordingTransport, lever_posting, load_fixture


def collect(*responses, board="acme", company=None, collector_cls=LeverCollector):
    t = RecordingTransport(*responses)
    jobs = collector_cls(board, company=company, client=t.client()).collect()
    return jobs, t


def ok(*postings):
    return httpx.Response(200, json=list(postings))


def params(request):
    return dict(request.url.params)


def test_request_shape():
    _, t = collect(ok(), board=" Acme ")
    req = t.requests[0]
    assert (req.url.host, req.url.path) == ("api.lever.co", "/v0/postings/acme")
    assert params(req) == {"mode": "json", "skip": "0", "limit": "100"}


def test_fixture_is_normalized_and_malformed_record_skipped(caplog):
    jobs, _ = collect(httpx.Response(200, json=load_fixture("lever_acme.json")), company="Acme")
    assert [j.title for j in jobs] == ["Software Engineer, New Grad", "Senior Staff Engineer"]
    assert "skipping malformed job '0b0b0b0b" in caplog.text
    j = jobs[0]
    assert (j.source, j.source_job_id, j.company, j.location) == (
        "lever", "6ed76ce8-4156-4b60-b120-403538bd66cd", "Acme", "New York, NY")
    assert j.url == "https://jobs.lever.co/acme/6ed76ce8-4156-4b60-b120-403538bd66cd"
    assert j.posted_at == datetime(2026, 9, 21, 14, 13, 20, tzinfo=timezone.utc)
    assert j.raw["salaryRange"]["min"] == 130000  # kept for later filters


def test_description_is_rebuilt_from_intro_lists_and_closing():
    (job, _), _ = collect(httpx.Response(200, json=load_fixture("lever_acme.json")))
    assert job.description == (
        "About the role\n\nBuild backend services in Java & Go.\n\n"
        "What you'll do\n- Design APIs\n- Ship to production\n\n"
        "Requirements\n- BS in Computer Science\n- 0-2 years of experience\n\n"
        "We offer great benefits."
    )


def test_plain_text_is_used_when_html_is_empty():
    (_, job), _ = collect(httpx.Response(200, json=load_fixture("lever_acme.json")))
    assert job.description == "Lead the platform team."


class TestFieldMapping:
    def test_company_is_registry_name_else_board(self):
        (a,), _ = collect(ok(lever_posting("1")), company="Acme Corp")
        (b,), _ = collect(ok(lever_posting("1")), board="acmeco")
        assert (a.company, b.company) == ("Acme Corp", "acmeco")

    @pytest.mark.parametrize("value", [None, "1790000000000", True, 10**20])
    def test_bad_created_at_becomes_none(self, value):
        (job,), _ = collect(ok(lever_posting("1", createdAt=value)))
        assert job.posted_at is None

    @pytest.mark.parametrize("categories", [None, {}, {"location": ""}, "nope"])
    def test_missing_location_becomes_none(self, categories):
        (job,), _ = collect(ok(lever_posting("1", categories=categories)))
        assert job.location is None

    def test_malformed_lists_are_ignored(self):
        (job,), _ = collect(ok(lever_posting("1", lists=["junk", {"text": "", "content": ""},
                                                          {"text": "Perks", "content": "<li>Food</li>"}])))
        assert job.description == "Build things.\n\nPerks\n- Food"


class TestPagination:
    def page(self, start, n):
        return ok(*[lever_posting(f"p{i}") for i in range(start, start + n)])

    def test_short_first_page_is_one_request(self):
        jobs, t = collect(self.page(0, 3))
        assert len(jobs) == 3 and len(t.requests) == 1

    def test_full_pages_are_followed_with_overlap_and_deduplicated(self):
        # 230 postings; pages advance by 90 (100 - 10 overlap).
        jobs, t = collect(self.page(0, 100), self.page(90, 100), self.page(180, 50))
        assert [params(r)["skip"] for r in t.requests] == ["0", "90", "180"]
        assert len(jobs) == 230 and len({j.source_job_id for j in jobs}) == 230

    def test_overlap_absorbs_a_removal_between_pages(self):
        # Page 1 = p0..p99. Before page 2 is fetched, p5 is removed, so every
        # later posting shifts left by one. Without overlap p100 would be lost.
        remaining = [f"p{i}" for i in range(150) if i != 5]
        def handler(request):
            skip, limit = int(request.url.params["skip"]), int(request.url.params["limit"])
            if skip == 0:
                return ok(*[lever_posting(f"p{i}") for i in range(100)])
            return ok(*[lever_posting(x) for x in remaining[skip:skip + limit]])
        jobs, _ = collect(handler)
        ids = {j.source_job_id for j in jobs}
        assert "p100" in ids and len(ids) == 150

    def test_exactly_one_full_page_needs_a_confirming_request(self):
        jobs, t = collect(self.page(0, 100), ok())
        assert len(jobs) == 100 and len(t.requests) == 2

    def test_runaway_pagination_is_an_error_not_a_partial_snapshot(self):
        class Small(LeverCollector):
            page_size, page_overlap, max_pages = 2, 0, 3
        n = iter(range(1000))
        with pytest.raises(CollectorError, match="exceeded 3 pages"):
            collect(lambda r: ok(lever_posting(f"x{next(n)}"), lever_posting(f"x{next(n)}")),
                    collector_cls=Small)

    def test_same_client_is_used_for_every_page(self):
        t = RecordingTransport(self.page(0, 100), ok())
        client = t.client()
        LeverCollector("acme", client=client).collect()
        assert len(t.requests) == 2 and not client.is_closed


class TestFailures:
    def test_empty_site_is_valid(self):
        jobs, _ = collect(ok())
        assert jobs == []

    @pytest.mark.parametrize("body", [b'{"postings": []}', b'{"ok": false, "error": "x"}', b"null", b'"text"'])
    def test_non_list_payload_is_an_error(self, body):
        with pytest.raises(CollectorError, match="unexpected Lever payload"):
            collect(httpx.Response(200, content=body, headers={"Content-Type": "application/json"}))

    def test_unknown_site_404(self):
        with pytest.raises(CollectorError, match="404"):
            collect(httpx.Response(404, json={"ok": False, "error": "Document not found"}))

    def test_every_record_malformed_is_an_error(self):
        with pytest.raises(CollectorError, match="could not normalize any of 2"):
            collect(ok({"id": "a"}, {"id": "b"}))

    def test_records_without_id_are_skipped_not_merged(self, caplog):
        jobs, _ = collect(ok({"text": "no id 1"}, {"text": "no id 2"}, lever_posting("ok")))
        assert [j.source_job_id for j in jobs] == ["ok"]
        assert caplog.text.count("skipping malformed job") == 2

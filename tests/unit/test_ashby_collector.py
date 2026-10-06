from datetime import datetime, timezone

import httpx
import pytest

from jobradar.collectors import AshbyCollector, CollectorError
from jobradar.collectors.base import parse_iso_datetime
from tests.factories import RecordingTransport, ashby_job, load_fixture


def collect(*responses, board="acme", company=None):
    t = RecordingTransport(*responses)
    jobs = AshbyCollector(board, company=company, client=t.client()).collect()
    return jobs, t


def ok(*jobs):
    return httpx.Response(200, json={"jobs": list(jobs)})


def test_request_shape():
    _, t = collect(ok(), board=" Ramp ")
    req = t.requests[0]
    assert (req.url.host, req.url.path) == ("api.ashbyhq.com", "/posting-api/job-board/ramp")
    assert dict(req.url.params) == {"includeCompensation": "true"}
    assert len(t.requests) == 1  # no pagination: one response is the whole board


def test_fixture_normalized_unlisted_dropped_malformed_skipped(caplog):
    jobs, _ = collect(httpx.Response(200, json=load_fixture("ashby_acme.json")), company="Acme")
    assert [j.title for j in jobs] == ["Software Engineer, New Grad", "Staff Security Engineer"]
    assert "Referral-only Engineer" not in {j.title for j in jobs}
    assert "skipping malformed job 'broken-" in caplog.text
    j = jobs[0]
    assert (j.source, j.source_job_id, j.company, j.location) == (
        "ashby", "34413f8d-26bf-4bbc-8ade-eb309a0e2245", "Acme", "New York, NY (HQ)")
    assert j.url == "https://jobs.ashbyhq.com/acme/34413f8d-26bf-4bbc-8ade-eb309a0e2245"
    assert j.posted_at == datetime(2026, 9, 30, 17, 12, 35, 753000, tzinfo=timezone.utc)
    assert j.raw["compensation"]["summaryComponents"][0]["minValue"] == 140000
    assert j.raw["secondaryLocations"][0]["location"] == "Remote (US)"


def test_description_from_html():
    (job, _), _ = collect(httpx.Response(200, json=load_fixture("ashby_acme.json")))
    assert job.description == ("About Acme\n\nBuild backend services in Kotlin & Postgres.\n\n"
                               "Requirements\n\n- BS in CS\n- 0-2 years of experience")


def test_plain_description_used_when_html_empty():
    (_, job), _ = collect(httpx.Response(200, json=load_fixture("ashby_acme.json")))
    assert job.description == "Secure all the things."


class TestFieldMapping:
    def test_company_is_registry_name_else_board(self):
        (a,), _ = collect(ok(ashby_job("1")), company="Ramp")
        (b,), _ = collect(ok(ashby_job("1")), board="rampco")
        assert (a.company, b.company) == ("Ramp", "rampco")

    @pytest.mark.parametrize("location", [None, "", 42, {"name": "NYC"}])
    def test_non_string_or_blank_location_becomes_none(self, location):
        (job,), _ = collect(ok(ashby_job("1", location=location)))
        assert job.location is None

    @pytest.mark.parametrize("value", [None, "", "soon", 1790000000])
    def test_bad_published_at_becomes_none(self, value):
        (job,), _ = collect(ok(ashby_job("1", publishedAt=value)))
        assert job.posted_at is None

    def test_is_listed_missing_counts_as_listed(self):
        job = ashby_job("1")
        del job["isListed"]
        jobs, _ = collect(ok(job))
        assert len(jobs) == 1

    @pytest.mark.parametrize("value", [False])
    def test_only_explicit_false_is_unlisted(self, value):
        jobs, _ = collect(ok(ashby_job("1", isListed=value), ashby_job("2", isListed=None)))
        assert [j.source_job_id for j in jobs] == ["2"]


class TestFailures:
    def test_empty_board_is_valid(self):
        jobs, _ = collect(ok())
        assert jobs == []

    def test_board_with_only_unlisted_jobs_is_empty_not_an_error(self):
        jobs, _ = collect(ok(ashby_job("1", isListed=False)))
        assert jobs == []

    @pytest.mark.parametrize("body", [b"[]", b'{"jobs": null}', b'{"apiVersion": "1"}', b"null"])
    def test_unexpected_payload_is_an_error(self, body):
        with pytest.raises(CollectorError, match="unexpected Ashby payload"):
            collect(httpx.Response(200, content=body, headers={"Content-Type": "application/json"}))

    def test_unknown_board_404(self):
        with pytest.raises(CollectorError, match="404"):
            collect(httpx.Response(404))

    def test_every_record_malformed_is_an_error(self):
        with pytest.raises(CollectorError, match="could not normalize any of 2"):
            collect(ok({"id": "a", "isListed": True}, {"id": "b"}))


class TestParseIsoDatetime:
    @pytest.mark.parametrize("value,expected", [
        ("2026-09-30T17:12:35.753+00:00", datetime(2026, 9, 30, 17, 12, 35, 753000, tzinfo=timezone.utc)),
        ("2026-09-30T17:12:35Z", datetime(2026, 9, 30, 17, 12, 35, tzinfo=timezone.utc)),
        ("2026-09-30T13:12:35-04:00", datetime(2026, 9, 30, 17, 12, 35, tzinfo=timezone.utc)),
    ])
    def test_valid(self, value, expected):
        assert parse_iso_datetime(value) == expected

    def test_naive_timestamp_is_treated_as_utc(self):
        parsed = parse_iso_datetime("2026-09-30T17:12:35")
        assert parsed.tzinfo is not None and parsed == datetime(2026, 9, 30, 17, 12, 35, tzinfo=timezone.utc)

    @pytest.mark.parametrize("value", [None, "", "yesterday", 123, ["2026-09-30"]])
    def test_invalid(self, value):
        assert parse_iso_datetime(value) is None

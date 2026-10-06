import httpx
import pytest

from jobradar.collectors import CollectorError, GreenhouseCollector
from tests.factories import RecordingTransport, greenhouse_job, load_fixture


def collect(*responses, board="acme", company=None):
    t = RecordingTransport(*responses)
    jobs = GreenhouseCollector(board, company=company, client=t.client()).collect()
    return jobs, t


def ok(*jobs):
    return httpx.Response(200, json={"jobs": list(jobs), "meta": {"total": len(jobs)}})


def test_calls_the_board_api_with_content():
    _, t = collect(ok(), board="  Stripe ")
    assert str(t.requests[0].url) == "https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true"


def test_fixture_is_normalized_and_malformed_record_skipped(caplog):
    jobs, _ = collect(httpx.Response(200, json=load_fixture("greenhouse_acme.json")))
    assert [j.source_job_id for j in jobs] == ["7001", "7002"]
    assert "skipping malformed job 7003" in caplog.text
    j = jobs[0]
    assert (j.source, j.company, j.title, j.location) == (
        "greenhouse", "Acme", "Software Engineer, New Grad", "New York, NY")
    assert j.url == "https://acme.com/careers?gh_jid=7001&gh_src=linkedin"  # stored as given
    assert "Build backend services in Java." in j.description and "<" not in j.description
    assert j.raw["requisition_id"] == "R-1"


class TestFieldMapping:
    def test_posted_at_prefers_first_published(self):
        (job,), _ = collect(ok(greenhouse_job(1)))
        assert job.posted_at.isoformat() == "2026-09-30T09:00:00-04:00"

    def test_posted_at_falls_back_to_updated_at_and_parses_z(self):
        (job,), _ = collect(ok(greenhouse_job(1, first_published=None, updated_at="2026-10-02T12:00:00Z")))
        assert job.posted_at.isoformat() == "2026-10-02T12:00:00+00:00"

    def test_unparseable_timestamps_become_none(self):
        (job,), _ = collect(ok(greenhouse_job(1, first_published="yesterday", updated_at=12345)))
        assert job.posted_at is None

    @pytest.mark.parametrize("location", [None, {}, {"name": ""}, {"name": None}, "not-a-dict"])
    def test_missing_location_becomes_none(self, location):
        (job,), _ = collect(ok(greenhouse_job(1, location=location)))
        assert job.location is None

    def test_registry_name_wins_over_greenhouse_company_name(self):
        (job,), _ = collect(ok(greenhouse_job(1, company_name="Acme Holdings, Inc.")), company="Acme")
        assert job.company == "Acme"

    def test_company_falls_back_to_greenhouse_name_then_board(self):
        (a,), _ = collect(ok(greenhouse_job(1, company_name="Acme")))
        (b,), _ = collect(ok(greenhouse_job(1, company_name=None)), board="acmeboard")
        assert (a.company, b.company) == ("Acme", "acmeboard")

    def test_numeric_id_becomes_string(self):
        (job,), _ = collect(ok(greenhouse_job(8172508)))
        assert job.source_job_id == "8172508"

    def test_missing_content_gives_empty_description(self):
        (job,), _ = collect(ok(greenhouse_job(1, content=None)))
        assert job.description == ""


class TestFailures:
    def test_empty_board_is_valid(self):
        jobs, _ = collect(ok())
        assert jobs == []

    @pytest.mark.parametrize("payload", [[], {"no_jobs": 1}, {"jobs": "nope"}, {"jobs": None}])
    def test_unexpected_payload_shape_is_an_error(self, payload):
        with pytest.raises(CollectorError, match="unexpected Greenhouse payload"):
            collect(httpx.Response(200, json=payload))

    def test_every_record_malformed_is_an_error_not_an_empty_board(self):
        with pytest.raises(CollectorError, match="could not normalize any of 2"):
            collect(ok({"id": 1}, {"id": 2}))

    def test_non_dict_records_are_skipped(self):
        jobs, _ = collect(ok("garbage", None, greenhouse_job(5)))
        assert [j.source_job_id for j in jobs] == ["5"]

    def test_http_failure_becomes_collector_error(self):
        with pytest.raises(CollectorError, match="404"):
            collect(httpx.Response(404))

    def test_transient_failure_is_retried_inside_collect(self, monkeypatch):
        monkeypatch.setattr("jobradar.http.time.sleep", lambda s: None)
        jobs, t = collect(httpx.Response(503), ok(greenhouse_job(1)))
        assert len(jobs) == 1 and len(t.requests) == 2


class TestClientLifecycle:
    def test_injected_client_is_not_closed(self):
        t = RecordingTransport(ok())
        client = t.client()
        GreenhouseCollector("acme", client=client).collect()
        assert not client.is_closed

    def test_owned_client_is_closed_even_on_error(self, monkeypatch):
        t = RecordingTransport(httpx.Response(404))
        made = []

        def fake_make_client():
            made.append(t.client())
            return made[-1]

        monkeypatch.setattr("jobradar.collectors.base.make_client", fake_make_client)
        with pytest.raises(CollectorError):
            GreenhouseCollector("acme").collect()
        assert made[0].is_closed


def test_collector_contract():
    c = GreenhouseCollector("Acme")
    assert (c.source, c.board, c.complete_snapshot) == ("greenhouse", "acme", True)

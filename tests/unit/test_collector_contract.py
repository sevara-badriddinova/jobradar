"""Behaviour every collector must have, whatever API it talks to.

Adding a collector to COLLECTORS without adding a case here fails
test_every_collector_has_a_contract_case, so new sources can't skip it.
"""

import httpx
import pytest

from jobradar.collectors import COLLECTORS, CollectorError
from jobradar.models import Job
from tests.factories import RecordingTransport, ashby_job, greenhouse_job, lever_posting
from tests.ats_fixtures import WORKDAY_BOARD, public_handler

# source -> (successful response with two jobs, host the collector must call)
CASES = {
    "greenhouse": (lambda: httpx.Response(200, json={"jobs": [greenhouse_job(1), greenhouse_job(2)]}),
                   "boards-api.greenhouse.io"),
    "lever": (lambda: httpx.Response(200, json=[lever_posting("a"), lever_posting("b")]),
              "api.lever.co"),
    "ashby": (lambda: httpx.Response(200, json={"jobs": [ashby_job("a"), ashby_job("b")]}),
              "api.ashbyhq.com"),
}

CASES.update({source: (lambda source=source: public_handler(source), host) for source, host in {
    "workday": "acme.wd1.myworkdayjobs.com", "workable": "apply.workable.com",
    "recruitee": "acme.recruitee.com", "rippling": "api.rippling.com",
    "personio": "acme.jobs.personio.de",
}.items()})

collectors = pytest.mark.parametrize("source", sorted(COLLECTORS))


def make(source, *responses, board="Acme", company="Acme Inc"):
    if source == "workday":
        board = WORKDAY_BOARD
    t = RecordingTransport(*responses)
    return COLLECTORS[source](board, company=company, client=t.client()), t


def test_every_collector_has_a_contract_case():
    assert set(COLLECTORS) == set(CASES), "add a contract case for each new collector"


@collectors
def test_identity(source):
    c, _ = make(source, CASES[source][0]())
    assert c.source == source and c.board == (WORKDAY_BOARD if source == "workday" else "acme") and isinstance(c.complete_snapshot, bool)


@collectors
def test_returns_normalized_jobs_from_the_right_host(source):
    ok, host = CASES[source]
    c, t = make(source, ok())
    jobs = c.collect()
    assert len(jobs) == 2 and all(isinstance(j, Job) for j in jobs)
    assert all(j.source == source and j.company == "Acme Inc" for j in jobs)
    assert all(isinstance(j.source_job_id, str) and j.source_job_id for j in jobs)
    assert all(j.raw for j in jobs)  # original payload kept
    assert {r.url.host for r in t.requests} == {host}


@collectors
def test_collect_is_deterministic(source):
    ok, _ = CASES[source]
    first, _ = make(source, ok())
    second, _ = make(source, ok())
    assert first.collect() == second.collect()


@collectors
def test_missing_board_is_a_collector_error(source):
    c, t = make(source, httpx.Response(404))
    with pytest.raises(CollectorError, match="404"):
        c.collect()
    assert len(t.requests) == 1


@collectors
def test_transient_errors_are_retried(source, monkeypatch):
    monkeypatch.setattr("jobradar.http.time.sleep", lambda s: None)
    c, _ = make(source, httpx.Response(503), httpx.ConnectError("blip"), CASES[source][0]())
    assert len(c.collect()) == 2


@collectors
def test_garbage_payload_is_a_collector_error(source):
    c, _ = make(source, httpx.Response(200, json={"unexpected": True} if source == "lever" else [1, 2]))
    with pytest.raises(CollectorError):
        c.collect()

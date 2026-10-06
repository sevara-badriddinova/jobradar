"""Whole slice: Greenhouse-shaped HTTP -> collector -> pipeline/CLI -> PostgreSQL.

Only the network is faked; everything else is the real code and a real database.
"""

import httpx
import pytest
from sqlalchemy import select

from jobradar import cli
from jobradar.collectors import GreenhouseCollector
from jobradar.db import collector_runs, jobs as jobs_t
from jobradar.pipeline import run_all
from tests.factories import RecordingTransport, greenhouse_job, load_fixture


class FakeGreenhouse:
    """A tiny in-memory Greenhouse: boards can be edited between polls."""

    def __init__(self):
        self.boards: dict[str, list[dict]] = {}
        self.down: set[str] = set()
        self.transport = RecordingTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        board = request.url.path.split("/")[3]
        if board in self.down:
            return httpx.Response(503)
        if board not in self.boards:
            return httpx.Response(404, json={"status": 404, "error": "Job not found"})
        return httpx.Response(200, json={"jobs": self.boards[board], "meta": {"total": len(self.boards[board])}})


@pytest.fixture
def greenhouse(monkeypatch):
    gh = FakeGreenhouse()
    monkeypatch.setattr(cli, "make_client", gh.transport.client)
    monkeypatch.setattr("jobradar.http.time.sleep", lambda s: None)
    return gh


def all_jobs(engine):
    with engine.connect() as conn:
        return conn.execute(select(jobs_t).order_by(jobs_t.c.id)).all()


def test_fixture_board_through_the_pipeline(engine):
    t = RecordingTransport(httpx.Response(200, json=load_fixture("greenhouse_acme.json")))
    (rep,) = run_all(engine, [GreenhouseCollector("acme", client=t.client())])
    assert rep.ok and rep.fetched == 2 and rep.result.new == 2
    rows = {r.source_job_id: r for r in all_jobs(engine)}
    ng = rows["7001"]
    assert ng.canonical_url == "https://acme.com/careers?gh_jid=7001"
    assert ng.description.startswith("About the role") and "<" not in ng.description
    assert ng.raw["requisition_id"] == "R-1"


def test_job_lifecycle_across_polls(engine, greenhouse):
    gh = greenhouse
    pipeline = lambda: run_all(engine, [GreenhouseCollector("acme", client=gh.transport.client())])[0]

    gh.boards["acme"] = [greenhouse_job(1), greenhouse_job(2)]
    assert pipeline().result.new == 2                              # discovered

    assert pipeline().result.new == 0                              # re-poll: nothing new

    gh.boards["acme"] = [greenhouse_job(1, content="&lt;p&gt;Now with Kafka&lt;/p&gt;"), greenhouse_job(2)]
    r = pipeline().result
    assert (r.new, r.changed) == (0, 1)                            # edited in place

    gh.boards["acme"] = [greenhouse_job(2)]
    assert pipeline().result.closed == 1                           # job 1 taken down

    gh.down.add("acme")
    rep = pipeline()
    assert not rep.ok                                              # outage...
    assert all_jobs(engine)[1].closed_at is None                   # ...closes nothing
    gh.down.clear()

    gh.boards["acme"] = [greenhouse_job(2), greenhouse_job(3, title="Software Engineer 1")]
    r = pipeline().result
    assert (r.new, r.duplicates) == (0, 1)                         # job 1 reposted as id 3
    rows = {x.source_job_id: x for x in all_jobs(engine)}
    assert rows["3"].status == "REPOSTED" and rows["3"].duplicate_of_id == rows["1"].id


def write_companies(tmp_path, *entries):
    f = tmp_path / "companies.toml"
    f.write_text("".join(
        f'[[company]]\nname = "{name}"\ncollector = "greenhouse"\nboard = "{board}"\npriority = {prio}\n'
        for name, board, prio in entries))
    return str(f)


def test_cli_full_flow(engine, db_env, greenhouse, tmp_path, capsys):
    greenhouse.boards["stripe"] = [greenhouse_job(10, company_name="Stripe, Inc.", title="Software Engineer, New Grad")]
    greenhouse.boards["figma"] = [greenhouse_job(20, company_name="Figma", title="Backend Engineer")]
    companies = write_companies(tmp_path, ("Stripe", "stripe", 1), ("Figma", "figma", 2), ("Nope", "nope", 3))

    assert cli.main(["init-db"]) == 0   # idempotent against an existing schema
    assert cli.main(["companies", "sync", companies]) == 0
    assert cli.main(["collect"]) == 1   # nope -> 404, the others still succeed
    out = capsys.readouterr().out
    assert "nope" in out and "HTTP 404" in out

    assert cli.main(["collect", "--priority", "2"]) == 0   # skips nope
    assert cli.main(["collect", "stripe"]) == 0
    capsys.readouterr()

    assert cli.main(["new", "--hours", "1"]) == 0
    out = capsys.readouterr().out
    # Registry name is used, not Greenhouse's "Stripe, Inc."
    assert "Stripe | Software Engineer, New Grad" in out and "Figma | Backend Engineer" in out
    assert "2 new job(s)" in out

    assert cli.main(["runs"]) == 0
    out = capsys.readouterr().out
    assert out.count("ok ") == 5 and "ERR greenhouse/nope" in out

    with engine.connect() as conn:
        assert len(conn.execute(select(collector_runs)).all()) == 6
    assert len(all_jobs(engine)) == 2


def test_disabled_company_is_not_collected(engine, db_env, greenhouse, tmp_path, capsys):
    greenhouse.boards["stripe"] = [greenhouse_job(1)]
    greenhouse.boards["figma"] = [greenhouse_job(2)]
    cli.main(["companies", "sync", write_companies(tmp_path, ("Stripe", "stripe", 1), ("Figma", "figma", 1))])
    cli.main(["companies", "sync", write_companies(tmp_path, ("Stripe", "stripe", 1))])  # figma removed
    greenhouse.transport.requests.clear()
    assert cli.main(["collect"]) == 0
    assert [r.url.path for r in greenhouse.transport.requests] == ["/v1/boards/stripe/jobs"]


def test_cli_new_hides_duplicates_and_closed(engine, db_env, greenhouse, tmp_path, capsys):
    cli.main(["companies", "sync", write_companies(tmp_path, ("Acme", "acme", 1))])
    greenhouse.boards["acme"] = [greenhouse_job(1), greenhouse_job(2, title="Software Engineer 1"),
                                 greenhouse_job(3, title="Data Engineer")]
    cli.main(["collect"])
    greenhouse.boards["acme"] = [greenhouse_job(1), greenhouse_job(2, title="Software Engineer 1")]
    cli.main(["collect"])
    capsys.readouterr()

    cli.main(["new"])
    out = capsys.readouterr().out
    assert "1 new job(s)" in out and "Software Engineer 1" in out and "Data Engineer" not in out

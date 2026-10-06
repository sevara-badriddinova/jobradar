"""Lever and Ashby end to end, and all platforms together in one run.

Only HTTP is faked (one fake server per platform, routed by host); the
registry, pipeline, dedup and CLI are the real code on a real database.
"""

import httpx
import pytest
from sqlalchemy import select

from jobradar import cli
from jobradar.db import jobs as jobs_t
from tests.factories import RecordingTransport, ashby_job, greenhouse_job, lever_posting


class FakeATS:
    def __init__(self):
        self.greenhouse: dict[str, list[dict]] = {}
        self.lever: dict[str, list[dict]] = {}
        self.ashby: dict[str, list[dict]] = {}
        self.transport = RecordingTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.split("/")
        if request.url.host == "boards-api.greenhouse.io":
            board = parts[3]  # /v1/boards/{board}/jobs
            if board not in self.greenhouse:
                return httpx.Response(404)
            return httpx.Response(200, json={"jobs": self.greenhouse[board]})
        if request.url.host == "api.lever.co":
            site = parts[3]   # /v0/postings/{site}
            if site not in self.lever:
                return httpx.Response(404, json={"ok": False, "error": "Document not found"})
            skip, limit = int(request.url.params["skip"]), int(request.url.params["limit"])
            return httpx.Response(200, json=self.lever[site][skip:skip + limit])
        if request.url.host == "api.ashbyhq.com":
            board = parts[3]  # /posting-api/job-board/{board}
            if board not in self.ashby:
                return httpx.Response(404)
            return httpx.Response(200, json={"jobs": self.ashby[board]})
        return httpx.Response(599)

    def hosts(self):
        return [r.url.host for r in self.transport.requests]


@pytest.fixture
def ats(monkeypatch):
    fake = FakeATS()
    monkeypatch.setattr(cli, "make_client", fake.transport.client)
    monkeypatch.setattr("jobradar.http.time.sleep", lambda s: None)
    return fake


def registry(tmp_path, *entries):
    f = tmp_path / "companies.toml"
    f.write_text("".join(
        f'[[company]]\nname = "{n}"\ncollector = "{c}"\nboard = "{b}"\n' for n, c, b in entries))
    assert cli.main(["companies", "sync", str(f)]) == 0


def jobs(engine):
    with engine.connect() as conn:
        return {(r.source, r.source_job_id): r for r in conn.execute(select(jobs_t))}


def test_lever_lifecycle(engine, db_env, ats, tmp_path):
    registry(tmp_path, ("Spotify", "lever", "spotify"))
    ats.lever["spotify"] = [lever_posting("a", text="Backend Engineer"), lever_posting("b", text="iOS Engineer")]

    assert cli.main(["collect"]) == 0
    stored = jobs(engine)
    assert set(stored) == {("lever", "a"), ("lever", "b")}
    assert stored[("lever", "a")].company == "Spotify" and stored[("lever", "a")].board == "spotify"

    assert cli.main(["collect"]) == 0                     # idempotent
    assert len(jobs(engine)) == 2

    ats.lever["spotify"] = [lever_posting("a", text="Backend Engineer")]
    cli.main(["collect"])
    assert jobs(engine)[("lever", "b")].closed_at is not None


def test_large_lever_site_is_paginated_and_nothing_is_wrongly_closed(engine, db_env, ats, tmp_path):
    registry(tmp_path, ("Big", "lever", "big"))
    ats.lever["big"] = [lever_posting(f"p{i}", text=f"Role {i}") for i in range(250)]
    cli.main(["collect"])
    cli.main(["collect"])
    stored = jobs(engine)
    assert len(stored) == 250
    assert all(r.closed_at is None for r in stored.values())
    assert sum(1 for h in ats.hosts() if h == "api.lever.co") == 6  # 3 pages x 2 runs


def test_greenhouse_and_lever_in_one_run(engine, db_env, ats, tmp_path, capsys):
    registry(tmp_path, ("Stripe", "greenhouse", "stripe"), ("Spotify", "lever", "spotify"),
             ("Gone", "lever", "gone"))
    ats.greenhouse["stripe"] = [greenhouse_job(1, title="Software Engineer, New Grad")]
    ats.lever["spotify"] = [lever_posting("a", text="Backend Engineer")]

    assert cli.main(["collect"]) == 1  # "gone" left Lever -> 404; the others still succeed
    out = capsys.readouterr().out
    assert "lever/gone" in out and "HTTP 404" in out and "greenhouse/stripe" in out
    assert set(jobs(engine)) == {("greenhouse", "1"), ("lever", "a")}
    assert set(ats.hosts()) == {"boards-api.greenhouse.io", "api.lever.co"}


def test_same_role_on_both_platforms_is_one_job(engine, db_env, ats, tmp_path):
    # A company migrating ATS often lists a role on both for a while. The
    # registry name gives both the same company key, so level-3 dedup links them.
    registry(tmp_path, ("Acme", "greenhouse", "acme"), ("Acme", "lever", "acme"))
    ats.greenhouse["acme"] = [greenhouse_job(1, company_name="Acme, Inc.", title="Software Engineer, New Grad",
                                             location={"name": "New York, NY"})]
    ats.lever["acme"] = [lever_posting("x", text="Software Engineer - New Grad")]

    cli.main(["collect"])
    stored = jobs(engine)
    gh, lv = stored[("greenhouse", "1")], stored[("lever", "x")]
    assert gh.status == "DISCOVERED"
    assert (lv.status, lv.dedup_reason, lv.duplicate_of_id) == ("DUPLICATE", "fingerprint", gh.id)


def test_same_board_slug_on_two_platforms_closes_independently(engine, db_env, ats, tmp_path):
    registry(tmp_path, ("Acme", "greenhouse", "acme"), ("Acme", "lever", "acme"))
    ats.greenhouse["acme"] = [greenhouse_job(1, title="Data Engineer")]
    ats.lever["acme"] = [lever_posting("x", text="Backend Engineer")]
    cli.main(["collect"])

    ats.lever["acme"] = [lever_posting("y", text="Frontend Engineer")]  # x removed from Lever only
    cli.main(["collect"])
    stored = jobs(engine)
    assert stored[("lever", "x")].closed_at is not None
    assert stored[("greenhouse", "1")].closed_at is None


def test_ashby_lifecycle_including_unlisting(engine, db_env, ats, tmp_path):
    registry(tmp_path, ("Ramp", "ashby", "ramp"))
    ats.ashby["ramp"] = [ashby_job("a", title="Backend Engineer"), ashby_job("b", title="Data Engineer"),
                         ashby_job("hidden", title="Referral Engineer", isListed=False)]

    assert cli.main(["collect"]) == 0
    stored = jobs(engine)
    assert set(stored) == {("ashby", "a"), ("ashby", "b")}  # unlisted never stored
    assert stored[("ashby", "a")].company == "Ramp"

    assert cli.main(["collect"]) == 0
    assert len(jobs(engine)) == 2                            # idempotent

    ats.ashby["ramp"] = [ashby_job("a", title="Backend Engineer"),
                         ashby_job("b", title="Data Engineer", isListed=False)]
    cli.main(["collect"])
    assert jobs(engine)[("ashby", "b")].closed_at is not None   # unlisted -> closed

    ats.ashby["ramp"][1]["isListed"] = True
    cli.main(["collect"])
    assert jobs(engine)[("ashby", "b")].closed_at is None       # relisted -> reopened, not new


def test_all_three_platforms_in_one_run(engine, db_env, ats, tmp_path, capsys):
    registry(tmp_path, ("Stripe", "greenhouse", "stripe"), ("Spotify", "lever", "spotify"),
             ("Ramp", "ashby", "ramp"))
    ats.greenhouse["stripe"] = [greenhouse_job(1, title="Software Engineer, New Grad")]
    ats.lever["spotify"] = [lever_posting("a", text="Backend Engineer")]
    ats.ashby["ramp"] = [ashby_job("r1", title="Platform Engineer")]

    assert cli.main(["collect"]) == 0
    assert set(jobs(engine)) == {("greenhouse", "1"), ("lever", "a"), ("ashby", "r1")}
    assert set(ats.hosts()) == {"boards-api.greenhouse.io", "api.lever.co", "api.ashbyhq.com"}
    out = capsys.readouterr().out
    assert all(label in out for label in ("greenhouse/stripe", "lever/spotify", "ashby/ramp"))


def test_role_on_ashby_after_moving_from_greenhouse_is_a_repost(engine, db_env, ats, tmp_path):
    # Company migrates Greenhouse -> Ashby: the old listing disappears, the
    # same role appears on Ashby. It's a repost, not a new job.
    registry(tmp_path, ("Acme", "greenhouse", "acme"), ("Acme", "ashby", "acme"))
    ats.greenhouse["acme"] = [greenhouse_job(1, title="Software Engineer, New Grad"),
                              greenhouse_job(2, title="Data Engineer")]
    ats.ashby["acme"] = []
    cli.main(["collect"])

    ats.greenhouse["acme"] = [greenhouse_job(2, title="Data Engineer")]  # role 1 leaves Greenhouse
    ats.ashby["acme"] = [ashby_job("new", title="Software Engineer - New Grad")]
    cli.main(["collect"])
    stored = jobs(engine)
    moved = stored[("ashby", "new")]
    assert (moved.status, moved.dedup_reason, moved.duplicate_of_id) == (
        "REPOSTED", "fingerprint", stored[("greenhouse", "1")].id)

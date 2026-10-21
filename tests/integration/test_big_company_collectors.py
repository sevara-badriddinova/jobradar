"""Amazon, Oracle Recruiting Cloud and Eightfold through ingestion, filters, AI
matching and Telegram: closures, detail reuse and the strict sponsorship policy."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from jobradar import cli, matcher
from jobradar.collectors import COLLECTORS
from jobradar.db import jobs, notifications
from jobradar.pipeline import run_collector
from jobradar.settings import FilterSettings, Preferences, Settings
from tests.ats_fixtures import (DESCRIPTION, EIGHTFOLD_BOARD, ORACLE_BOARD, TS, amazon_item, amazon_page,
                                eightfold_detail, eightfold_item, oracle_detail, oracle_item, oracle_page)
from tests.factories import FakeLLM, RecordingTransport, assessment
from tests.fake_telegram import FakeTelegram
from tests.integration.test_matcher import add_profile

BOARDS = {"amazon": "software-development", "oracle": ORACLE_BOARD, "eightfold": EIGHTFOLD_BOARD}


class Board:
    """A mutable fake career site: tests remove postings or fail requests between polls."""

    def __init__(self, source, ids=("1", "2"), when=None, description=DESCRIPTION):
        self.source, self.ids, self.fail, self.details = source, list(ids), set(), []
        self.when = when or datetime.now(UTC) - timedelta(hours=2)
        self.description = description

    def __call__(self, request):
        p, path = request.url.params, request.url.path
        if self.source == "amazon":
            if int(p["offset"]) in self.fail:
                return httpx.Response(503)
            items = [amazon_item(i, posted=self.when.strftime("%B %d, %Y").replace(" 0", " ")) for i in self.ids]
            for item in items:
                item["description"] = self.description
                item["basic_qualifications"] = "Experience with Python"
            return httpx.Response(200, json=amazon_page(items[int(p["offset"]):][:100], hits=len(items)))
        if self.source == "oracle":
            if path.endswith("Details"):
                i = p["finder"].split('"')[1]
                self.details.append(i)
                if i in self.fail:
                    return httpx.Response(404)
                d = oracle_detail(i)
                d["items"][0].update(ExternalPostedStartDate=self.when.isoformat(), ExternalDescriptionStr=self.description,
                                     ExternalQualificationsStr="Experience with Java")
                return httpx.Response(200, json=d)
            reqs = [oracle_item(i, posted=self.when.strftime("%Y-%m-%d")) for i in self.ids]
            return httpx.Response(200, json=oracle_page(reqs, total=len(reqs)))
        if path.endswith("position_details"):
            i = int(p["position_id"])
            self.details.append(str(i))
            if str(i) in self.fail:
                return httpx.Response(404)
            d = eightfold_detail(i, posted=int(self.when.timestamp()), created=int(self.when.timestamp()) - 86400)
            d["data"]["jobDescription"] = self.description
            return httpx.Response(200, json=d)
        items = [eightfold_item(int(i), posted=int(self.when.timestamp())) for i in self.ids]
        return httpx.Response(200, json={"data": {"count": len(items), "positions": items[int(p["start"]):][:10]}})

    def collector(self):
        return COLLECTORS[self.source](BOARDS[self.source], company="Acme", client=RecordingTransport(self).client())


sources = pytest.mark.parametrize("source", sorted(BOARDS))


def deliver(engine, monkeypatch, llm):
    policy = Settings(filters=FilterSettings(max_posting_age_days=3, require_explicit_sponsorship=True),
                      preferences=Preferences(needs_visa_sponsorship=True))
    monkeypatch.setattr(cli, "load_settings", lambda path: policy)
    monkeypatch.setattr(matcher.llm, "ask", llm)
    add_profile(engine)
    telegram = FakeTelegram()
    monkeypatch.setattr(cli, "make_client", telegram.client)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", telegram.token)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    for args in (["filter"], ["match"], ["notify", "--digest"], ["notify", "--digest"]):
        assert cli.main(args) == 0


@sources
def test_fresh_sponsored_postings_reach_telegram_exactly_once(engine, db_env, monkeypatch, source):
    assert run_collector(engine, Board(source).collector()).result.new == 2
    deliver(engine, monkeypatch, FakeLLM(assessment(role_match=10, technical_match=10)))
    with engine.connect() as conn:
        assert {r.status for r in conn.execute(select(jobs))} == {"NOTIFIED"}
        sent = conn.execute(select(notifications)).all()
        assert len(sent) == 2 and all(n.status == "sent" and n.attempts == 1 for n in sent)


@sources
def test_unstated_sponsorship_and_stale_postings_never_reach_ai(engine, db_env, monkeypatch, source):
    run_collector(engine, Board(source, ids=("1",), description="<p>Build backend services.</p>").collector())
    run_collector(engine, Board(source, ids=("1", "2"), when=datetime(2026, 1, 5, tzinfo=UTC)).collector())
    llm = FakeLLM(assessment(role_match=10, technical_match=10))
    deliver(engine, monkeypatch, llm)
    with engine.connect() as conn:
        rows = {r.source_job_id.rsplit(":", 1)[-1]: r for r in conn.execute(select(jobs))}
        assert all(r.status == "FILTERED" for r in rows.values())
        assert conn.execute(select(notifications)).all() == []
    assert not llm.calls


@sources
def test_removed_posting_closes_only_on_a_complete_snapshot(engine, source):
    board = Board(source, ids=("1", "2", "3"))
    assert run_collector(engine, board.collector()).result.new == 3
    board.ids = ["1", "2", "4"]               # 3 removed, 4 new
    # A failed detail (new posting 4) or failed listing page makes the
    # snapshot partial: posting 3 must stay open.
    board.fail = {"4"} if source != "amazon" else {0}
    partial = run_collector(engine, board.collector())
    assert partial.result is None or partial.result.closed == 0
    board.fail = set()
    assert run_collector(engine, board.collector()).result.closed == 1
    with engine.connect() as conn:
        open_ids = {r.source_job_id.rsplit(":", 1)[-1] for r in conn.execute(select(jobs)) if r.closed_at is None}
    assert open_ids == {"1", "2", "4"}


@pytest.mark.parametrize("source", ["oracle", "eightfold"])
def test_stored_details_are_not_fetched_again(engine, source):
    board = Board(source, ids=("1",))
    first = run_collector(engine, board.collector(), record_unchanged=False)
    board.ids, board.details = ["1", "2"], []
    second = run_collector(engine, board.collector(), record_unchanged=False, known_snapshot=first.snapshot_hash)
    assert second.result.new == 1 and board.details == ["2"]
    with engine.connect() as conn:
        row = conn.execute(select(jobs).where(jobs.c.source_job_id.like("%:1"))).one()
        assert row.posted_at is not None and "sponsorship" in row.description and row.content_changed_at is None   # not seen as changed


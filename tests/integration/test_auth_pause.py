"""A rejected API key must not bury the match queue (Nov 11, 2026).

From Nov 9 the VM's Anthropic key returned 401 on every call. Each match task
used up its attempts and died, nothing was scored, nothing was sent, and no
message said why. Now: matching pauses with every task kept queued, one
Telegram alert says scoring is down, one says when it works again, and dead
tasks that are still fresh can be revived with `requeue match --dead`.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select, update

from jobradar import cli, llm, matcher
from jobradar import queue as q
from jobradar.db import app_state, jobs as jobs_t, work_queue
from jobradar.telegram import Telegram
from jobradar.worker import SCORING_ALERT_KEY
from tests.factories import FakeLLM, assessment
from tests.fake_telegram import FakeTelegram
from tests.integration.helpers import ingest, row
from tests.integration.test_matcher import add_profile
from tests.integration.test_workers import FakeBoards, add_board, fresh, make_worker, poll, tasks

REJECTED = llm.CredentialsRejected("401 invalid or revoked API key", "AuthenticationError: invalid x-api-key")
GOOD = assessment(role_match=10, technical_match=10, experience_match=10)


@pytest.fixture(autouse=True)
def no_global_meter():
    yield
    llm.set_meter(None)


@pytest.fixture
def model(monkeypatch):
    fake = FakeLLM(REJECTED)
    monkeypatch.setattr(matcher.llm, "ask", fake)
    return fake


@pytest.fixture
def server():
    return FakeTelegram()


def bot(server):
    return Telegram(server.token, server.client(), sleep=lambda s: None), "42"


def queued(engine, boards, worker, n=3):
    add_board(engine, "acme")
    add_profile(engine)
    boards.snapshots["acme"] = [fresh(f"m{i}") for i in range(n)]
    poll(worker, engine)
    worker.filter_once()
    return tasks(engine, "match")


def make_available(engine):
    with engine.begin() as conn:
        conn.execute(update(work_queue).values(available_at=func.now()))


def alert_open(engine):
    with engine.connect() as conn:
        return conn.execute(select(app_state.c.value).where(app_state.c.key == SCORING_ALERT_KEY)
                            ).scalar_one_or_none()


def test_rejected_key_keeps_every_task_and_alerts_exactly_once(engine, model, server):
    boards = FakeBoards()
    w = make_worker(engine, boards, telegram=bot(server))
    assert len(queued(engine, boards, w)) == 3

    assert w.match_once() == 0
    assert len(model.calls) == 1                                   # stops at the first rejection
    assert {(t.state, t.attempts) for t in tasks(engine, "match")} == {("ready", 0)}  # nothing charged
    with engine.begin() as conn:
        assert q.claim(conn, "match", "someone-else", 10) == []    # held back until the recheck
    assert w.auth_paused() and w.runtime_status()["matching_auth_paused"]
    assert w.log_metrics()["ai"]["auth_error"] == "401 invalid or revoked API key"
    [alert] = server.sent
    assert "AI scoring is down" in alert["text"] and "401 invalid or revoked API key" in alert["text"]

    make_available(engine)
    assert w.match_once() == 0 and len(model.calls) == 1           # paused: no calls at all

    restarted = make_worker(engine, boards, telegram=bot(server))  # e.g. systemd restart, key still dead
    assert restarted.match_once() == 0 and len(model.calls) == 2
    assert len(server.sent) == 1 and alert_open(engine) is not None
    assert {(t.state, t.attempts) for t in tasks(engine, "match")} == {("ready", 0)}


def test_working_key_sends_one_recovery_message_even_after_a_restart(engine, model, server):
    boards = FakeBoards()
    w = make_worker(engine, boards, telegram=bot(server))
    queued(engine, boards, w)
    w.match_once()
    assert len(server.sent) == 1

    model.answers = [GOOD]                                         # the key was rotated
    make_available(engine)
    fixed = make_worker(engine, boards, telegram=bot(server))      # new process, new env
    assert fixed.match_once() == 3
    assert len(server.sent) == 2 and "working again" in server.sent[1]["text"]
    assert alert_open(engine) is None and not fixed.auth_paused()
    assert {r.status for r in (row(engine, "m0"), row(engine, "m1"), row(engine, "m2"))} == {"MATCHED"}
    assert tasks(engine, "match") == []

    with engine.begin() as conn:                                   # later work: no more messages
        q.enqueue(conn, "match", [row(engine, "m0").id])
    fixed.match_once()
    assert len(server.sent) == 2


def test_no_recovery_message_when_nothing_was_down(engine, model, server):
    model.answers = [GOOD]
    boards = FakeBoards()
    w = make_worker(engine, boards, telegram=bot(server))
    queued(engine, boards, w)
    assert w.match_once() == 3
    assert server.sent == []


def test_an_alert_that_certainly_failed_is_retried_by_the_next_worker(engine, model, server):
    boards = FakeBoards()
    server.fail("sendMessage", *[httpx.ConnectError("offline")] * 3)  # never reached Telegram
    w = make_worker(engine, boards, telegram=bot(server))
    queued(engine, boards, w)
    w.match_once()
    assert server.sent == [] and alert_open(engine) is None        # claim released for a retry

    make_available(engine)
    make_worker(engine, boards, telegram=bot(server)).match_once()
    assert len(server.sent) == 1 and alert_open(engine) is not None


def test_requeue_dead_revives_only_fresh_eligible_jobs(engine, db_env, tmp_path, capsys):
    now = datetime.now(UTC)
    ingest(engine, [fresh(sid) for sid in ("new", "old", "closed", "scored")])
    ids = {sid: row(engine, sid).id for sid in ("new", "old", "closed", "scored")}
    with engine.begin() as conn:
        conn.execute(update(jobs_t).values(filtered_at=func.now()))
        conn.execute(update(jobs_t).where(jobs_t.c.id == ids["old"]).values(posted_at=now - timedelta(days=6)))
        conn.execute(update(jobs_t).where(jobs_t.c.id == ids["closed"]).values(closed_at=func.now()))
        conn.execute(update(jobs_t).where(jobs_t.c.id == ids["scored"]).values(status="MATCHED"))
        q.enqueue(conn, "match", list(ids.values()))
        conn.execute(update(work_queue).values(state="dead", attempts=12, last_error="LLM assessment failed"))
    profile = tmp_path / "profile.toml"
    profile.write_text("[filters]\nmax_posting_age_days = 3\n")

    assert cli.main(["--profile", str(profile), "requeue", "match", "--dead", "--dry-run"]) == 0
    assert "would requeue 1 of 4" in capsys.readouterr().out
    assert {t.state for t in tasks(engine, "match")} == {"dead"}

    assert cli.main(["--profile", str(profile), "requeue", "match", "--dead"]) == 0
    assert "requeued 1 of 4" in capsys.readouterr().out
    by_job = {t.job_id: (t.state, t.attempts, t.last_error) for t in tasks(engine, "match")}
    assert by_job[ids["new"]] == ("ready", 0, None)
    assert {by_job[ids[s]][0] for s in ("old", "closed", "scored")} == {"dead"}

    assert cli.main(["requeue", "filter", "--dead"]) == 2
    assert cli.main(["requeue", "match", "--dry-run"]) == 2

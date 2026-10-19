"""Trial-readiness: AI budget cap, shutdown, lease recovery, queue-mode delivery,
Workday pagination under the byte budget, and poll/worker mutual exclusion."""

import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select, text, update

from jobradar import cli, llm, matcher
from jobradar import queue as q
from jobradar.ai_budget import DailyBudgetMeter
from jobradar.collectors import build_collector
from jobradar.cycle import cycle_lock
from jobradar.db import ai_usage, board_state, jobs as jobs_t, notifications
from jobradar.http import ByteBudget, byte_bounded
from jobradar.settings import NotificationSettings
from tests.ats_fixtures import WORKDAY_BOARD, workday_detail, workday_item
from tests.factories import FakeLLM, assessment
from tests.integration.helpers import row
from tests.integration.test_matcher import add_profile
from tests.integration.test_workers import (SETTINGS, FakeBoards, add_board, fresh, make_worker, poll,
                                             tasks)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def llm_fake(monkeypatch):
    def install(*answers):
        fake = FakeLLM(*answers)
        monkeypatch.setattr(matcher.llm, "ask", fake)
        return fake
    return install


@pytest.fixture(autouse=True)
def no_global_meter():
    yield
    llm.set_meter(None)


def queue_matches(engine, boards, worker, n=2):
    add_board(engine, "acme")
    add_profile(engine)
    boards.snapshots["acme"] = [fresh(f"m{i}") for i in range(n)]
    poll(worker, engine)
    worker.filter_once()
    return tasks(engine, "match")


# --- AI spending cap ---------------------------------------------------------------------

class TestBudget:
    def test_pricing_and_ledger(self, engine):
        meter = DailyBudgetMeter(engine, 1.0)
        assert meter.record("claude-haiku-4-5-20251001", 2000, 400) == pytest.approx(0.004)
        assert meter.record("claude-haiku-4-5-20251001", 1000, 0) == pytest.approx(0.001)
        assert meter.record("some-new-model", 1_000_000, 0) == pytest.approx(15.0)  # most expensive rate
        assert llm.price("claude-opus-4-20250514") == (15.0, 75.0)
        assert llm.price("claude-opus-4-5-20251101") == (5.0, 25.0)
        with engine.connect() as conn:
            haiku = conn.execute(select(ai_usage).where(ai_usage.c.model.like("claude-haiku%"))).one()
        assert (haiku.calls, haiku.input_tokens, haiku.output_tokens) == (2, 3000, 400)
        with pytest.raises(llm.BudgetExceeded):
            meter.check()

    def test_llm_ask_records_actual_usage_and_refuses_over_cap(self, engine, monkeypatch):
        usage = SimpleNamespace(input_tokens=1500, output_tokens=300, cache_read_input_tokens=0,
                                cache_creation_input_tokens=0)
        response = SimpleNamespace(usage=usage, parsed_output="ok", stop_reason="end_turn")
        client = SimpleNamespace(messages=SimpleNamespace(parse=lambda **kw: response))
        monkeypatch.setattr(llm, "_client", lambda: client)
        llm.set_meter(DailyBudgetMeter(engine, 0.003))
        assert llm.ask(object, system="s", prompt="p", model="claude-haiku-4-5") == "ok"  # $0.003
        with pytest.raises(llm.BudgetExceeded):
            llm.ask(object, system="s", prompt="p", model="claude-haiku-4-5")
        with engine.connect() as conn:
            assert conn.execute(select(ai_usage.c.calls)).scalar_one() == 1

    def test_exhausted_budget_keeps_tasks_queued_untouched(self, engine, llm_fake):
        boards = FakeBoards()
        fake = llm_fake(assessment(role_match=9, technical_match=9, experience_match=9))
        w = make_worker(engine, boards, ai_daily_budget_usd=0.50)
        before = queue_matches(engine, boards, w)
        DailyBudgetMeter(engine, None).record("claude-haiku-4-5", 600_000, 0)  # $0.60 spent today
        assert w.match_once() == 0 and fake.calls == []
        after = tasks(engine, "match")
        assert [(t.id, t.state, t.attempts) for t in after] == [(t.id, "ready", 0) for t in before]
        assert w.counters["match_budget_pauses"] == 1

    def test_cap_reached_mid_batch_releases_without_charging(self, engine, llm_fake):
        boards = FakeBoards()
        calls = []

        def answer(output, prompt):
            calls.append(prompt)
            if len(calls) > 1:
                raise llm.BudgetExceeded("daily AI budget reached")
            return assessment(role_match=9, technical_match=9, experience_match=9)
        llm_fake(answer)
        w = make_worker(engine, boards, ai_daily_budget_usd=5.0)
        queue_matches(engine, boards, w)
        assert w.match_once() == 0
        left = tasks(engine, "match")
        assert {(t.state, t.attempts) for t in left} == {("ready", 0)} and len(left) == 2
        assert sorted(r.status for r in (row(engine, "m0"), row(engine, "m1"))) == ["DISCOVERED", "MATCHED"]

    def test_legacy_match_command_stops_cleanly_at_the_cap(self, engine, db_env, monkeypatch, llm_fake):
        from tests.integration.test_matcher import seed
        seed(engine)
        add_profile(engine)
        DailyBudgetMeter(engine, None).record("claude-haiku-4-5", 2_000_000, 0)
        monkeypatch.setenv("JOBRADAR_AI_DAILY_BUDGET_USD", "1")
        # The fake replaces ask entirely, so emulate the meter check the real one performs.
        monkeypatch.setattr(matcher.llm, "ask", lambda *a, **k: (_ for _ in ()).throw(
            llm.BudgetExceeded("daily AI budget reached")))
        assert cli.main(["match"]) == 0
        assert row(engine, "j0").status == "DISCOVERED"


# --- shutdown and lease recovery -----------------------------------------------------------

class SlowBoards(FakeBoards):
    def __init__(self, seconds):
        super().__init__()
        self.seconds = seconds

    def __call__(self, collector, board, company=None, client=None):
        inner = super().__call__(collector, board, company, client)
        original, seconds = inner.collect, self.seconds

        def slow():
            time.sleep(seconds)
            return original()
        inner.collect = slow
        return inner


def test_stop_returns_within_the_deadline_and_releases_leases(engine):
    boards = SlowBoards(6)
    add_board(engine, "slow")
    boards.snapshots["slow"] = [fresh("s")]
    w = make_worker(engine, boards, shutdown_timeout=1.5, idle_sleep=0.1)
    runner = threading.Thread(target=w.run)
    runner.start()
    time.sleep(1.0)                               # the slow collection is in flight
    started = time.monotonic()
    w.stop.set()
    runner.join(10)
    assert not runner.is_alive() and time.monotonic() - started < 4
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(board_state)
                            .where(board_state.c.lease_owner.is_not(None))).scalar_one() == 0


def test_crashed_collector_board_is_reclaimed_and_live_leases_are_extended(engine):
    boards = FakeBoards()
    add_board(engine, "acme")
    boards.snapshots["acme"] = [fresh("a")]
    crashed, survivor = make_worker(engine, boards), make_worker(engine, boards, board_lease=300)
    with engine.begin() as conn:
        q.seed_boards(conn)
    [board] = crashed.claim_boards(1)              # ...then the process died
    assert survivor.claim_boards(1) == []
    with engine.begin() as conn:
        conn.execute(update(board_state).values(lease_until=func.now() - timedelta(seconds=1)))
    [again] = survivor.claim_boards(1)
    assert again.company_id == board.company_id
    with engine.begin() as conn:
        conn.execute(update(board_state).values(lease_until=func.now() + timedelta(seconds=5)))
    survivor.keep_leases()                         # the keeper extends work still in progress
    with engine.connect() as conn:
        left = conn.execute(select(board_state.c.lease_until - func.now())).scalar_one()
    assert left > timedelta(seconds=200)
    assert survivor.collect_board(again) == "ok" and crashed.collect_board(board) in ("lost", "ok")
    assert row(engine, "a").status == "DISCOVERED"


def test_sigterm_stops_the_cli_worker_gracefully(engine, database_url, tmp_path):
    profile = tmp_path / "profile.toml"
    profile.write_text("")
    env = {**os.environ, "JOBRADAR_DATABASE_URL": database_url, "PYTHONPATH": str(ROOT / "src")}
    env.pop("JOBRADAR_HEALTH_BUCKET", None)
    proc = subprocess.Popen([sys.executable, "-m", "jobradar", "--profile", str(profile), "worker",
                             "--roles", "schedule,filter,match"], env=env, cwd=tmp_path,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline, seen = time.monotonic() + 30, []
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        seen.append(line)
        if "starting roles" in line:
            break
    assert any("starting roles" in s for s in seen), "".join(seen)
    proc.send_signal(signal.SIGTERM)
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 0, out
    assert "stopped; released" in out


# --- mutual exclusion ----------------------------------------------------------------------

def test_worker_refuses_to_start_while_a_legacy_cycle_runs(engine, db_env, tmp_path):
    profile = tmp_path / "p.toml"
    profile.write_text("")
    with cycle_lock(engine) as acquired:
        assert acquired
        assert cli.main(["--profile", str(profile), "worker", "--roles", "filter"]) == 75


def test_legacy_cycle_skips_while_a_worker_runs(engine, db_env, tmp_path, capsys):
    profile = tmp_path / "p.toml"
    profile.write_text("")
    w = make_worker(engine, FakeBoards(), roles=frozenset({"filter"}), idle_sleep=0.1)
    with cycle_lock(engine) as acquired:   # exactly what cmd_worker holds for its lifetime
        assert acquired
        runner = threading.Thread(target=w.run, kwargs={"duration": 1})
        runner.start()
        assert cli.main(["--profile", str(profile), "cycle"]) == 0
        runner.join()
    assert "another cycle is running; skipped" in capsys.readouterr().out


@pytest.mark.skipif(not shutil.which("flock"), reason="util-linux flock not installed")
def test_poll_unit_lock_skips_while_workers_hold_the_shared_lock(tmp_path):
    lock = tmp_path / "maintenance.lock"
    worker = subprocess.Popen(["flock", "--shared", str(lock), "sleep", "5"])
    time.sleep(0.5)
    # The exact poll ExecStart wrapper from the live unit's drop-in.
    poll = subprocess.run(["flock", "--exclusive", "--nonblock", "--conflict-exit-code", "0", str(lock),
                           "echo", "POLL RAN"], capture_output=True, text=True)
    # Backups block (no --nonblock) until the workers are stopped.
    backup = subprocess.run(["flock", "--exclusive", "--wait", "1", str(lock), "true"])
    worker.terminate()
    worker.wait()
    assert poll.returncode == 0 and "POLL RAN" not in poll.stdout
    assert backup.returncode == 1


# --- Telegram queue (Mini App) delivery --------------------------------------------------

def test_queue_mode_publishes_one_queue_update_and_never_per_job_alerts(engine, llm_fake):
    from tests.fake_telegram import FakeTelegram
    from jobradar.telegram import Telegram
    server = FakeTelegram()
    settings = SETTINGS.model_copy(update={"notifications": NotificationSettings(
        delivery="queue", immediate_min_score=70, digest_min_score=60)})
    boards = FakeBoards()
    llm_fake(assessment(role_match=10, technical_match=10, experience_match=10))
    w = make_worker(engine, boards, telegram=(Telegram(server.token, server.client()), "42"), settings=settings)
    queue_matches(engine, boards, w)
    w.match_once()
    w.notify_once()
    sent_after_first = len(server.sent)
    w.notify_once()
    with engine.connect() as conn:
        statuses = conn.execute(select(notifications.c.status)).scalars().all()
    # Exactly one queue message lists both jobs (the legacy queue-mode behaviour);
    # it marks them delivered, and a repeated pass sends nothing new.
    assert statuses == ["sent", "sent"] and sent_after_first == 1 and len(server.sent) == 1
    text_ = server.sent[0]["text"]
    assert "Your jobs" in text_ and "Backend Engineer m0" in text_ and "Backend Engineer m1" in text_


# --- Workday pagination under the byte budget -------------------------------------------

def workday_server(pages=10, size=20, pad=4000):
    calls = {"search": 0, "detail": 0}

    def handle(request):
        if request.method == "POST":
            calls["search"] += 1
            body = __import__("json").loads(request.content)
            start = body["offset"]
            items = [workday_item(str(i)) for i in range(start, min(start + size, pages * size))]
            return httpx.Response(200, json={"total": pages * size, "jobPostings": items})
        calls["detail"] += 1
        i = request.url.path.rsplit("_R", 1)[-1]
        detail = workday_detail(i)
        detail["jobPostingInfo"]["jobDescription"] += "x" * pad
        return httpx.Response(200, json=detail)
    return calls, httpx.Client(transport=httpx.MockTransport(handle))


def test_workday_pages_and_details_are_all_charged_and_released():
    calls, client = workday_server()
    budget = ByteBudget(10 * 1024 * 1024)
    collector = build_collector("workday", WORKDAY_BOARD, company="Acme", client=client)
    with byte_bounded(budget, max_response=1024 * 1024) as meter:
        jobs = collector.collect()
        held = budget.used
    assert len(jobs) == 200 and collector.complete_snapshot
    assert calls == {"search": 10, "detail": 200}
    assert meter.total > 200 * 4000 and held == meter.total     # detail threads were metered too
    assert budget.used == 0 and not meter.backpressure


def test_workday_board_over_budget_is_retried_without_failure_or_closing(engine):
    calls, client = workday_server()
    add_board(engine, "acme-wd", collector="workday")
    with engine.begin() as conn:
        conn.execute(text("UPDATE companies SET board = :b"), {"b": WORKDAY_BOARD})
    w = make_worker(engine, FakeBoards(), byte_budget=200_000, byte_wait=0.05)
    w.collector_factory = build_collector
    w.http = client
    w.limiter.rates["myworkdayjobs.com"] = 1000.0
    with engine.begin() as conn:
        q.seed_boards(conn)
    [board] = w.claim_boards(1)
    assert w.budget.acquire(150_000, 0)          # another collection holds most of the budget
    assert w.collect_board(board) == "backpressure"
    w.budget.release(150_000)
    with engine.connect() as conn:
        state = conn.execute(select(board_state, (board_state.c.next_due_at - func.now()).label("in_"))).one()
        closed = conn.execute(select(func.count()).select_from(jobs_t)
                              .where(jobs_t.c.closed_at.is_not(None))).scalar_one()
    assert state.consecutive_failures == 0 and state.in_ < timedelta(seconds=90) and closed == 0
    assert w.budget.used == 0

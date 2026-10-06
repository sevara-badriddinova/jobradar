import httpx
import pytest
from sqlalchemy import func, select, update

from jobradar import cli
from jobradar.db import app_state, feedback, job_matches, jobs as jobs_t, notifications
from jobradar.notifications import (
    MAX_ATTEMPTS, MAX_FAILURES_IN_A_ROW, enqueue, process_updates, resend_stuck, send_digest, send_immediate,
)
from jobradar.settings import NotificationSettings
from jobradar.telegram import Telegram
from tests.factories import make_job
from tests.fake_telegram import FakeTelegram
from tests.integration.helpers import ingest, row
from tests.integration.test_matcher import add_profile

CHAT = "42"
SETTINGS = NotificationSettings()


def matched(engine, scores: dict[str, float]):
    """Jobs in status MATCHED with the given scores (and an assessment each)."""
    ingest(engine, [make_job(sid, title=f"Engineer {sid}", location=f"City {sid}") for sid in scores])
    profile = add_profile(engine)
    with engine.begin() as conn:
        for sid, score in scores.items():
            job_id = row(engine, sid).id
            match_id = conn.execute(job_matches.insert().values(
                job_id=job_id, profile_id=profile.id, context_hash="c", model="m", prompt_version="1",
                assessment={"reasons": [f"why {sid}"], "concerns": [], "missing_skills": []},
                score=score).returning(job_matches.c.id)).scalar_one()
            conn.execute(update(jobs_t).where(jobs_t.c.id == job_id).values(
                status="MATCHED", match_score=score, match_id=match_id, filtered_at=func.now()))


@pytest.fixture
def server():
    return FakeTelegram()


@pytest.fixture
def tg(server):
    return Telegram(server.token, server.client(), sleep=lambda s: None)


def queue(engine, settings=SETTINGS):
    with engine.begin() as conn:
        return enqueue(conn, settings)


def statuses(engine):
    with engine.connect() as conn:
        return {r.source_job_id: r.status for r in conn.execute(
            select(jobs_t.c.source_job_id, notifications.c.status)
            .join(notifications, notifications.c.job_id == jobs_t.c.id))}


class TestQueue:
    def test_thresholds_decide_immediate_digest_or_nothing(self, engine):
        matched(engine, {"hi": 92, "edge": 80, "mid": 75, "low": 69.9})
        assert queue(engine) == {"immediate": 2, "digest": 1}
        with engine.connect() as conn:
            kinds = dict(conn.execute(select(jobs_t.c.source_job_id, notifications.c.kind)
                                      .join(notifications, notifications.c.job_id == jobs_t.c.id)).all())
        assert kinds == {"hi": "immediate", "edge": "immediate", "mid": "digest"}

    def test_queueing_twice_adds_nothing(self, engine):
        matched(engine, {"a": 90})
        queue(engine)
        assert queue(engine) == {"immediate": 0, "digest": 0}

    def test_closed_and_already_applied_jobs_are_not_queued(self, engine):
        matched(engine, {"closed": 90, "applied": 90, "ok": 90})
        with engine.begin() as conn:
            conn.execute(update(jobs_t).where(jobs_t.c.source_job_id == "closed").values(closed_at=func.now()))
            conn.execute(update(jobs_t).where(jobs_t.c.source_job_id == "applied").values(status="APPLIED"))
        assert queue(engine) == {"immediate": 1, "digest": 0}


class TestImmediate:
    def test_sends_once_and_marks_everything(self, engine, server, tg):
        matched(engine, {"a": 92, "b": 85})
        queue(engine)
        result = send_immediate(engine, tg, CHAT)
        assert (result.sent, result.failed, result.stuck) == (2, 0, 0)
        assert [m["text"].splitlines()[0] for m in server.sent] == ["🎯 <b>92% match · Engineer a</b>", "🎯 <b>85% match · Engineer b</b>"]
        assert server.sent[0]["reply_markup"]["inline_keyboard"][1][0]["callback_data"] == f"applied:{row(engine, 'a').id}"
        a = row(engine, "a")
        assert (a.status, a.notified_at is not None) == ("NOTIFIED", True)
        with engine.connect() as conn:
            n = conn.execute(select(notifications).where(notifications.c.job_id == a.id)).one()
        assert (n.status, n.attempts, n.message_id, n.sent_at is not None) == ("sent", 1, 101, True)

        assert send_immediate(engine, tg, CHAT).sent == 0  # re-running never re-sends
        queue(engine)
        assert send_immediate(engine, tg, CHAT).sent == 0 and len(server.sent) == 2

    def test_definite_failure_is_retried_next_run(self, engine, server, tg):
        matched(engine, {"a": 92})
        queue(engine)
        server.fail("sendMessage", (400, {"ok": False, "description": "Bad Request: chat not found"}))
        result = send_immediate(engine, tg, CHAT)
        assert (result.sent, result.failed) == (0, 1)
        assert statuses(engine) == {"a": "pending"} and row(engine, "a").status == "MATCHED"
        assert send_immediate(engine, tg, CHAT).sent == 1 and len(server.sent) == 1

    def test_gives_up_after_max_attempts(self, engine, server, tg):
        matched(engine, {"a": 92})
        queue(engine)
        server.fail("sendMessage", *[(400, {"ok": False, "description": "nope"})] * MAX_ATTEMPTS)
        for _ in range(MAX_ATTEMPTS):
            send_immediate(engine, tg, CHAT)
        assert statuses(engine) == {"a": "failed"}
        assert send_immediate(engine, tg, CHAT).sent == 0  # failed rows are left alone

    def test_unknown_delivery_is_not_resent(self, engine, server, tg):
        matched(engine, {"a": 92, "b": 91})
        queue(engine)
        server.fail("sendMessage", httpx.ReadTimeout("no answer"))
        result = send_immediate(engine, tg, CHAT)
        assert (result.sent, result.stuck) == (1, 1)
        assert statuses(engine) == {"a": "sending", "b": "sent"}
        assert send_immediate(engine, tg, CHAT).sent == 0  # still not resent: no duplicates

        with engine.begin() as conn:  # you decide to resend it
            assert resend_stuck(conn) == 1
        assert send_immediate(engine, tg, CHAT).sent == 1 and statuses(engine) == {"a": "sent", "b": "sent"}

    def test_stops_after_repeated_failures(self, engine, server, tg):
        matched(engine, {str(i): 90 for i in range(6)})
        queue(engine)
        server.fail("sendMessage", *[(403, {"ok": False, "description": "Forbidden: bot was blocked"})] * 6)
        result = send_immediate(engine, tg, CHAT)
        assert result.failed == MAX_FAILURES_IN_A_ROW
        assert sum(1 for m, _ in server.calls if m == "sendMessage") == MAX_FAILURES_IN_A_ROW

    def test_job_closed_before_sending_is_cancelled(self, engine, server, tg):
        matched(engine, {"a": 92})
        queue(engine)
        with engine.begin() as conn:
            conn.execute(update(jobs_t).where(jobs_t.c.source_job_id == "a").values(closed_at=func.now()))
        assert send_immediate(engine, tg, CHAT).cancelled == 1
        assert server.sent == [] and statuses(engine) == {"a": "cancelled"}

    def test_digest_items_are_not_sent_as_alerts(self, engine, server, tg):
        matched(engine, {"mid": 75})
        queue(engine)
        assert send_immediate(engine, tg, CHAT).sent == 0 and server.sent == []


class TestDigest:
    def test_groups_pending_digest_jobs_best_first(self, engine, server, tg):
        matched(engine, {"x": 72, "y": 78, "z": 92})
        queue(engine)
        result = send_digest(engine, tg, CHAT)
        assert result.sent == 2 and len(server.sent) == 1
        text = server.sent[0]["text"]
        assert text.index("Engineer y") < text.index("Engineer x") and "Engineer z" not in text
        assert statuses(engine) == {"x": "sent", "y": "sent", "z": "pending"}
        assert row(engine, "x").status == "NOTIFIED"
        assert send_digest(engine, tg, CHAT).sent == 0 and len(server.sent) == 1

    def test_failed_digest_goes_back_to_the_queue(self, engine, server, tg):
        matched(engine, {"x": 72})
        queue(engine)
        server.fail("sendMessage", (400, {"ok": False, "description": "bad"}))
        assert send_digest(engine, tg, CHAT).failed == 1 and statuses(engine) == {"x": "pending"}
        assert send_digest(engine, tg, CHAT).sent == 1


class TestButtons:
    def test_applied_marks_the_job_and_records_feedback(self, engine, server, tg):
        matched(engine, {"a": 92})
        queue(engine)
        send_immediate(engine, tg, CHAT)
        job_id = row(engine, "a").id
        server.press(f"applied:{job_id}", chat_id=CHAT)

        assert process_updates(engine, tg, CHAT) == 1
        a = row(engine, "a")
        assert (a.status, a.applied_at is not None) == ("APPLIED", True)
        with engine.connect() as conn:
            assert conn.execute(select(feedback.c.kind, feedback.c.source)).all() == [("APPLIED", "telegram")]
        methods = [m for m, _ in server.calls]
        assert "answerCallbackQuery" in methods and "editMessageReplyMarkup" in methods
        edited = [p for m, p in server.calls if m == "editMessageReplyMarkup"][0]
        assert edited["reply_markup"]["inline_keyboard"][1][0]["text"] == "✅ Marked as applied"
        assert edited["reply_markup"]["inline_keyboard"][1][1]["callback_data"] == f"ignored:{job_id}"

    def test_each_update_is_processed_once(self, engine, server, tg):
        matched(engine, {"a": 92})
        server.press(f"ignored:{row(engine, 'a').id}", chat_id=CHAT)
        assert process_updates(engine, tg, CHAT) == 1
        assert process_updates(engine, tg, CHAT) == 0  # stored offset skips it
        with engine.connect() as conn:
            assert conn.execute(select(feedback.c.kind)).scalars().all() == ["IGNORED"]
            assert int(conn.execute(select(app_state.c.value)).scalar_one()) == server.updates[-1]["update_id"] + 1
        assert row(engine, "a").status == "MATCHED"  # not interested doesn't change the pipeline state

    def test_pressing_twice_is_harmless(self, engine, server, tg):
        matched(engine, {"a": 92})
        job_id = row(engine, "a").id
        server.press(f"applied:{job_id}", chat_id=CHAT)
        server.press(f"applied:{job_id}", chat_id=CHAT)
        assert process_updates(engine, tg, CHAT) == 2
        with engine.connect() as conn:
            assert conn.execute(select(func.count()).select_from(feedback)).scalar_one() == 1

    def test_presses_from_other_chats_and_junk_are_ignored(self, engine, server, tg):
        matched(engine, {"a": 92})
        job_id = row(engine, "a").id
        server.press(f"applied:{job_id}", chat_id=999)       # someone else found the bot
        server.press("applied:not-a-number", chat_id=CHAT)
        server.press("applied:999999", chat_id=CHAT)          # no such job
        server.press("noop", chat_id=CHAT)
        server.message_from(42, "Me")
        assert process_updates(engine, tg, CHAT) == 0
        assert row(engine, "a").status == "MATCHED"
        with engine.connect() as conn:
            assert conn.execute(select(func.count()).select_from(feedback)).scalar_one() == 0

    def test_telegram_side_effects_failing_does_not_lose_the_press(self, engine, server, tg):
        matched(engine, {"a": 92})
        server.press(f"applied:{row(engine, 'a').id}", chat_id=CHAT)
        server.fail("answerCallbackQuery", (400, {"ok": False, "description": "query is too old"}))
        server.fail("editMessageReplyMarkup", (400, {"ok": False, "description": "message not found"}))
        assert process_updates(engine, tg, CHAT) == 1 and row(engine, "a").status == "APPLIED"

    def test_confirmed_or_stale_buttons_stop_loading_without_changing_feedback(self, engine, server, tg):
        server.press("noop", chat_id=CHAT)
        server.press("applied:999999", chat_id=CHAT)
        server.press("ignored:invalid", chat_id=CHAT)
        server.press("noop", chat_id=999)

        assert process_updates(engine, tg, CHAT) == 0
        answers = [p for method, p in server.calls if method == "answerCallbackQuery"]
        assert [p["text"] for p in answers] == [
            "Already recorded", "This button is no longer available", "This button is no longer available",
        ]
        assert not any(method == "editMessageReplyMarkup" for method, _ in server.calls)
        with engine.connect() as conn:
            assert conn.execute(select(func.count()).select_from(feedback)).scalar_one() == 0
        assert process_updates(engine, tg, CHAT) == 0
        assert len([p for method, p in server.calls if method == "answerCallbackQuery"]) == 3


class TestCli:
    @pytest.fixture
    def env(self, monkeypatch, db_env, server):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", server.token)
        monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
        monkeypatch.setattr(cli, "make_client", server.client)

    def test_missing_config_explains_setup(self, engine, db_env, monkeypatch, capsys):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        assert cli.main(["notify"]) == 2
        err = capsys.readouterr().err
        assert "TELEGRAM_BOT_TOKEN is not set" in err and "@BotFather" in err

    def test_notify_and_digest(self, engine, env, server, capsys):
        matched(engine, {"hi": 90, "mid": 75})
        assert cli.main(["notify"]) == 0
        out = capsys.readouterr().out
        assert "queued: 1 immediate, 1 for the digest" in out and "alerts: 1 sent" in out
        assert len(server.sent) == 1

        assert cli.main(["notify", "--digest"]) == 0
        assert "digest: 1 job(s) sent" in capsys.readouterr().out and len(server.sent) == 2

    def test_notify_applies_button_presses_first(self, engine, env, server, capsys):
        matched(engine, {"a": 90})
        server.press(f"applied:{row(engine, 'a').id}", chat_id=CHAT)
        cli.main(["notify"])
        assert "button presses applied: 1" in capsys.readouterr().out
        assert row(engine, "a").status == "APPLIED" and server.sent == []  # applied: no alert needed

    def test_stuck_notifications_are_reported(self, engine, env, server, capsys):
        matched(engine, {"a": 90})
        server.fail("sendMessage", httpx.ReadTimeout("x"))
        assert cli.main(["notify"]) == 0
        assert "may or may not have reached Telegram" in capsys.readouterr().err
        assert cli.main(["notify", "--resend-stuck"]) == 0
        assert "re-queued 1" in capsys.readouterr().out and len(server.sent) == 1

    def test_setup_lists_chats_and_sends_a_test(self, engine, env, server, capsys):
        assert cli.main(["telegram", "setup"]) == 0
        assert "no messages yet: send any message to @jobradar_test_bot" in capsys.readouterr().out
        server.message_from(4242, "Sevara")
        assert cli.main(["telegram", "setup", "--test"]) == 0
        out = capsys.readouterr().out
        assert "4242  Sevara" in out and "export TELEGRAM_CHAT_ID=4242" in out and "test message sent" in out
        assert server.sent[0]["chat_id"] == CHAT

    def test_bad_token(self, engine, env, monkeypatch, capsys):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "wrong")
        assert cli.main(["telegram", "setup"]) == 2
        assert "Unauthorized" in capsys.readouterr().err

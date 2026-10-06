import httpx
from sqlalchemy import func, select, text, update

from jobradar.db import applications, job_reviews, jobs, review_responses
from jobradar.notifications import CONSUMER_LOCK, process_updates, refresh_buttons, send_digest, send_immediate
from jobradar.review import queue_messages, save_summary, set_choice
from tests.integration.test_notifications import CHAT, matched, queue, server, tg
from tests.integration.helpers import row


def press(engine, server, tg, action, sid):
    server.press(f"{action}:{row(engine, sid).id}", chat_id=CHAT)
    return process_updates(engine, tg, CHAT)


def test_reviews_build_ranked_queue_then_automatically_present_it(engine, server, tg):
    matched(engine, {"a": 83, "b": 96, "c": 91})
    queue(engine)
    send_immediate(engine, tg, CHAT)
    assert press(engine, server, tg, "queue", "a") == 1
    assert press(engine, server, tg, "queue", "a") == 1
    assert press(engine, server, tg, "queue", "b") == 1
    assert len(server.sent) == 3  # still reviewing: no premature summary
    assert press(engine, server, tg, "ignored", "c") == 1
    summary = server.sent[-1]["text"]
    assert "0 role(s) left" in summary
    assert summary.index("Engineer b") < summary.index("Engineer a")
    assert "96%" in summary and "83%" in summary and "Engineer c" not in summary
    assert row(engine, "a").status == "NOTIFIED"  # queued does not mean applied
    assert press(engine, server, tg, "ignored", "c") == 1
    assert len(server.sent) == 4  # duplicate decision does not resend summary
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(job_reviews)).scalar_one() == 3
        assert conn.execute(select(func.count()).select_from(applications)).scalar_one() == 0


def test_can_remove_and_restore_selection_and_skip_for_sponsorship(engine, server, tg):
    matched(engine, {"a": 90})
    press(engine, server, tg, "queue", "a")
    press(engine, server, tg, "sponsorship", "a")
    with engine.connect() as conn:
        assert "No open, unapplied roles selected" in queue_messages(conn)[0]
        assert conn.execute(select(job_reviews.c.choice)).scalar_one() == "SPONSORSHIP"
    assert queue(engine) == {"immediate": 0, "digest": 0}
    press(engine, server, tg, "queue", "a")
    with engine.connect() as conn:
        assert "Engineer a" in queue_messages(conn)[0]
    assert queue(engine) == {"immediate": 1, "digest": 0}


def test_applied_removes_from_queue_and_records_application_even_if_later_ignored(engine, server, tg):
    matched(engine, {"a": 90})
    press(engine, server, tg, "queue", "a")
    press(engine, server, tg, "applied", "a")
    press(engine, server, tg, "ignored", "a")
    press(engine, server, tg, "queue", "a")
    with engine.connect() as conn:
        assert "No open, unapplied roles selected" in queue_messages(conn)[0]
        assert conn.execute(select(applications.c.source, applications.c.kind)).one() == ("telegram", "APPLIED")
    assert row(engine, "a").status == "APPLIED"
    assert any("no longer eligible" in p["text"] for m, p in server.calls if m == "answerCallbackQuery")


def test_closed_applied_or_filtered_queued_jobs_are_hidden_and_not_sent(engine, server, tg):
    matched(engine, {"closed": 90, "applied": 90, "filtered": 75})
    queue(engine)
    with engine.begin() as conn:
        for sid in ("closed", "applied", "filtered"):
            set_choice(conn, row(engine, sid).id, "QUEUED")
        conn.execute(update(jobs).where(jobs.c.source_job_id == "closed").values(closed_at=func.now()))
        conn.execute(update(jobs).where(jobs.c.source_job_id == "applied").values(status="APPLIED"))
        conn.execute(update(jobs).where(jobs.c.source_job_id == "filtered").values(status="FILTERED"))
        assert "No open, unapplied roles selected" in queue_messages(conn)[0]
    assert send_immediate(engine, tg, CHAT).cancelled == 2
    assert send_digest(engine, tg, CHAT).cancelled == 1
    assert server.sent == []


def test_queue_commands_are_private_and_repeat_updates_are_idempotent(engine, server, tg):
    matched(engine, {"a": 90})
    press(engine, server, tg, "queue", "a")
    server.message_from(999, "Stranger", "/queue")
    server.press("show_queue", chat_id=999)
    process_updates(engine, tg, CHAT)
    assert server.sent == []
    for command in ("/queue", "/done", "/queue@jobradar_test_bot"):
        server.message_from(int(CHAT), "User", command)
        process_updates(engine, tg, CHAT)
        assert "Engineer a" in server.sent[-1]["text"]
    assert len(server.sent) == 3
    process_updates(engine, tg, CHAT)
    assert len(server.sent) == 3


def test_persisted_summary_sends_after_restart_and_unknown_delivery_is_not_retried(engine, server, tg):
    with engine.begin() as conn:
        save_summary(conn, 23)
    server.fail("sendMessage", httpx.ReadTimeout("unknown delivery"))
    process_updates(engine, tg, CHAT)
    process_updates(engine, tg, CHAT)
    assert len([m for m, _ in server.calls if m == "sendMessage"]) == 1
    with engine.connect() as conn:
        assert conn.execute(select(review_responses.c.status)).scalar_one() == "sending"
    server.message_from(int(CHAT), "User", "/queue")
    process_updates(engine, tg, CHAT)
    assert len(server.sent) == 1  # explicit recovery


def test_digest_buttons_preserve_other_roles_and_finish_the_list(engine, server, tg):
    matched(engine, {"a": 73, "b": 78})
    queue(engine)
    send_digest(engine, tg, CHAT)
    message_id = server.sent[0]["message_id"]
    for action, sid in (("queue", "a"), ("sponsorship", "b")):
        server.press(f"{action}:{row(engine, sid).id}", chat_id=CHAT, message_id=message_id)
        process_updates(engine, tg, CHAT)
    buttons = [p for m, p in server.calls if m == "editMessageReplyMarkup"][-1]["reply_markup"]["inline_keyboard"]
    callbacks = {b["callback_data"] for r in buttons for b in r}
    assert {f"queue:{row(engine, 'a').id}", f"queue:{row(engine, 'b').id}"} <= callbacks
    assert "73%" in server.sent[-1]["text"] and "Engineer b" not in server.sent[-1]["text"]
    assert refresh_buttons(engine, tg, CHAT) == 1


def test_update_consumers_do_not_race(engine, server, tg):
    with engine.connect() as owner:
        owner.execute(text("SELECT pg_advisory_lock(:key)"), {"key": CONSUMER_LOCK})
        try:
            server.message_from(int(CHAT), "User", "/queue")
            assert process_updates(engine, tg, CHAT) == 0
            assert server.calls == []
        finally:
            owner.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": CONSUMER_LOCK})
    process_updates(engine, tg, CHAT)
    assert len(server.sent) == 1


def test_huge_callback_id_is_acknowledged_without_crashing_listener(engine, server, tg):
    server.press("queue:" + "9" * 50, chat_id=CHAT)
    assert process_updates(engine, tg, CHAT) == 0
    assert any(m == "answerCallbackQuery" for m, _ in server.calls)


def test_large_queue_is_split_and_html_is_escaped(engine):
    matched(engine, {str(i): 90 for i in range(100)})
    with engine.begin() as conn:
        for job_id in conn.execute(select(jobs.c.id)).scalars():
            set_choice(conn, job_id, "QUEUED")
        conn.execute(update(jobs).values(company="A&B <test>", url="javascript:alert(1)"))
        messages = queue_messages(conn)
    assert len(messages) > 1
    assert sum(m.count("90%") for m in messages) == 100
    assert all(len(m.encode("utf-16-le")) // 2 <= 4096 for m in messages)
    assert "A&amp;B &lt;test&gt;" in messages[0] and "javascript:" not in "".join(messages)


def test_unsent_daily_digest_does_not_block_current_review_completion(engine, server, tg):
    matched(engine, {"a": 90, "later": 75})
    queue(engine)
    send_immediate(engine, tg, CHAT)
    press(engine, server, tg, "queue", "a")
    assert len(server.sent) == 2
    assert "0 role(s) left" in server.sent[-1]["text"]

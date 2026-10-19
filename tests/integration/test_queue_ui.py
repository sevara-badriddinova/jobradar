import httpx
import pytest
from sqlalchemy import select, update

from jobradar.db import applications, job_reviews, jobs, notifications, review_responses
from jobradar.notifications import process_updates
from jobradar.queue_ui import parse_callback, read_state, render_queue, request_queue
from tests.integration.helpers import row
from tests.integration.test_notifications import CHAT, matched, queue, server, tg


def click(engine, server, tg, data, message_id=101):
    server.press(data, chat_id=CHAT, message_id=message_id)
    return process_updates(engine, tg, CHAT)


def test_one_message_for_browsing_liking_skipping_and_applying_across_restarts(engine, server, tg):
    matched(engine, {"a": 90, "b": 75})
    click(engine, server, tg, "show_queue")
    assert len(server.sent) == 1
    job_id = row(engine, "a").id
    click(engine, server, tg, f"q:open:{job_id}:new:0")
    assert "why a" in server.edited[-1]["text"]
    assert any(b.get("url") for r in server.edited[-1]["reply_markup"]["inline_keyboard"] for b in r)
    click(engine, server, tg, f"q:like:{job_id}:new:0")
    assert "Liked" in server.edited[-1]["text"]
    click(engine, server, tg, "q:list:liked:0")
    assert "Engineer a" in server.edited[-1]["text"]
    # Every call rereads persistent state; no process-local message cache.
    click(engine, server, tg, f"q:ignored:{job_id}:liked:0")
    click(engine, server, tg, "q:list:skipped:0")
    assert "Engineer a" in server.edited[-1]["text"]
    click(engine, server, tg, f"q:like:{job_id}:skipped:0")
    click(engine, server, tg, f"q:applied:{job_id}:liked:0")
    click(engine, server, tg, "q:list:applied:0")
    assert "Engineer a" in server.edited[-1]["text"]
    assert len(server.sent) == 1
    assert {e["message_id"] for e in server.edited} == {101}
    with engine.connect() as conn:
        assert conn.execute(select(applications.c.kind)).scalar_one() == "APPLIED"
        assert conn.execute(select(job_reviews.c.choice)).scalar_one() == "APPLIED"


def test_queue_includes_unreviewed_jobs_and_paginates_without_new_messages(engine, server, tg):
    matched(engine, {str(i): 90 - i for i in range(12)})
    click(engine, server, tg, "show_queue")
    assert "New · 12 jobs" in server.sent[0]["text"]
    assert "Engineer 0" in server.sent[0]["text"] and "Engineer 5" not in server.sent[0]["text"]
    click(engine, server, tg, "q:list:new:1")
    assert "Engineer 5" in server.edited[-1]["text"] and "Page 2 of 3" in server.edited[-1]["text"]
    click(engine, server, tg, "q:list:new:999999")
    assert "Page 3 of 3" in server.edited[-1]["text"]
    assert len(server.sent) == 1


def test_existing_summary_is_upgraded_in_place_and_identical_refresh_is_noop(engine, server, tg):
    matched(engine, {"a": 90})
    with engine.begin() as conn:
        conn.execute(review_responses.insert().values(update_id=10, part=0, body="old", status="sent", message_id=45))
    click(engine, server, tg, "show_queue", message_id=45)
    assert server.sent == [] and server.edited[-1]["message_id"] == 45
    click(engine, server, tg, "show_queue", message_id=45)
    assert len(server.edited) == 1


def test_unknown_edit_delivery_retries_same_message_and_preserves_choice(engine, server, tg):
    matched(engine, {"a": 90})
    click(engine, server, tg, "show_queue")
    server.fail("editMessageText", httpx.ReadTimeout("unknown"))
    click(engine, server, tg, f"q:like:{row(engine, 'a').id}:new:0")
    with engine.connect() as conn:
        assert read_state(conn, CHAT)["dirty"] is True
        assert conn.execute(select(job_reviews.c.choice)).scalar_one() == "QUEUED"
    process_updates(engine, tg, CHAT)
    assert server.edited[-1]["message_id"] == 101
    assert len(server.sent) == 1


def test_deleted_queue_is_replaced_once(engine, server, tg):
    matched(engine, {"a": 90})
    click(engine, server, tg, "show_queue")
    server.fail("editMessageText", (400, {"ok": False, "description": "Bad Request: message to edit not found"}))
    click(engine, server, tg, "q:list:liked:0")
    process_updates(engine, tg, CHAT)
    process_updates(engine, tg, CHAT)
    assert len(server.sent) == 2
    with engine.connect() as conn:
        assert read_state(conn, CHAT)["message_id"] == 102


def test_new_matches_update_existing_queue_without_more_alerts(engine, server, tg):
    matched(engine, {"a": 90, "b": 75})
    queue(engine)
    with engine.begin() as conn:
        request_queue(conn, CHAT, publish=True)
    process_updates(engine, tg, CHAT)
    assert len(server.sent) == 1
    with engine.connect() as conn:
        assert set(conn.execute(select(notifications.c.status)).scalars()) == {"sent"}
    matched(engine, {"c": 85})
    with engine.begin() as conn:
        conn.execute(update(jobs).values(closed_at=None))  # earlier jobs remain on this board
    queue(engine)
    with engine.begin() as conn:
        request_queue(conn, CHAT, publish=True)
    process_updates(engine, tg, CHAT)
    assert len(server.sent) == 1 and "New · 3 jobs" in server.edited[-1]["text"]
    with engine.connect() as conn:
        assert set(conn.execute(select(notifications.c.message_id)).scalars()) == {101}


def test_markup_and_callback_limits_and_private_actions(engine, server, tg):
    matched(engine, {str(i): 90 for i in range(6)})
    with engine.begin() as conn:
        conn.execute(update(jobs).values(title="<&😀" * 150, company="<&" * 100, location="😀" * 100))
        request_queue(conn, CHAT)
        body, buttons = render_queue(conn, read_state(conn, CHAT))
    assert "&lt;" in body and "&amp;" in body
    assert len(body.encode("utf-16-le")) // 2 <= 4096
    assert all(len(b["callback_data"].encode()) <= 64 for r in buttons for b in r)
    server.press(f"q:applied:{row(engine, '0').id}:new:0", chat_id=999)
    process_updates(engine, tg, CHAT)
    assert row(engine, '0').status == "MATCHED"


def test_queue_delivery_cli_uses_one_message_for_immediate_and_digest(engine, db_env, server, tg, monkeypatch):
    from jobradar import cli
    from jobradar.settings import NotificationSettings, Settings
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", server.token)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    monkeypatch.setattr(cli, "make_client", server.client)
    monkeypatch.setattr(cli, "load_settings", lambda path: Settings(notifications=NotificationSettings(delivery="queue")))
    matched(engine, {"a": 90, "b": 75})
    assert cli.main(["notify", "--digest"]) == 0
    assert cli.main(["notify", "--digest"]) == 0
    assert len(server.sent) == 1
    assert "New · 2 jobs" in server.sent[0]["text"]
    with engine.connect() as conn:
        assert set(conn.execute(select(notifications.c.status)).scalars()) == {"sent"}


def test_newer_navigation_during_edit_is_not_lost(engine, server, tg, monkeypatch):
    matched(engine, {"a": 90})
    click(engine, server, tg, "show_queue")
    original = tg.edit_message

    def edit(*args):
        with engine.begin() as conn:
            request_queue(conn, CHAT, view="skipped", page=0)
        return original(*args)

    monkeypatch.setattr(tg, "edit_message", edit)
    click(engine, server, tg, "q:list:liked:0")
    with engine.connect() as conn:
        state = read_state(conn, CHAT)
        assert state["view"] == "skipped" and state["dirty"] is True
    monkeypatch.setattr(tg, "edit_message", original)
    process_updates(engine, tg, CHAT)
    assert "Skipped · 0 jobs" in server.edited[-1]["text"]


def test_unknown_create_delivery_does_not_duplicate_or_mark_jobs_sent(engine, server, tg):
    matched(engine, {"a": 90})
    queue(engine)
    with engine.begin() as conn:
        request_queue(conn, CHAT, publish=True)
    server.fail("sendMessage", httpx.ReadTimeout("unknown"))
    process_updates(engine, tg, CHAT)
    with engine.begin() as conn:
        request_queue(conn, CHAT, publish=True)  # even a scheduled refresh cannot resend
    process_updates(engine, tg, CHAT)
    assert sum(method == "sendMessage" for method, _ in server.calls) == 1
    with engine.connect() as conn:
        assert conn.execute(select(notifications.c.status)).scalar_one() == "pending"


def test_main_menu_commands_and_back_navigation_edit_same_message(engine, server, tg):
    matched(engine, {"a": 90})
    click(engine, server, tg, "show_queue")
    for command, view in (("/menu", "menu"), ("/new", "new"), ("/liked", "liked"),
                          ("/applied", "applied"), ("/skipped@jobradar_test_bot", "skipped")):
        server.message_from(int(CHAT), "User", command)
        process_updates(engine, tg, CHAT)
        with engine.connect() as conn:
            assert read_state(conn, CHAT)["view"] == view
    click(engine, server, tg, "q:menu")
    assert "JobRadar" in server.edited[-1]["text"]
    assert len(server.edited[-1]["reply_markup"]["inline_keyboard"]) == 2
    click(engine, server, tg, f"q:open:{row(engine, 'a').id}:new:0")
    buttons = [b for r in server.edited[-1]["reply_markup"]["inline_keyboard"] for b in r]
    assert any(b["text"] == "🏠 Main menu" for b in buttons)
    assert not any("q:list:liked" in b.get("callback_data", "") for b in buttons)
    assert "🕒" in server.edited[-1]["text"]
    assert len(server.sent) == 1


def test_job_list_has_no_visible_ids_or_numbering_and_has_age(engine, server, tg):
    matched(engine, {"a": 90})
    click(engine, server, tg, "show_queue")
    message = server.sent[0]
    assert "1. Engineer" not in message["text"] and "#" not in message["text"]
    assert "🕒" in message["text"]
    button = message["reply_markup"]["inline_keyboard"][0][0]
    assert button["text"] == "Acme — Engineer a"


@pytest.mark.parametrize("data", ["q:list:nope:0", "q:list:new:-1", "q:list:new:9999999",
                                  "q:open:99999999999999999999:new:0", "q:applied:1:nope:0",
                                  "q:open:１２:new:0", "q:like:0:new:0"])
def test_invalid_navigation_is_rejected(data):
    assert parse_callback(data) is None

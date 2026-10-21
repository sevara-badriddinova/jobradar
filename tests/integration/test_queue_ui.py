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


def test_every_menu_command_answers_at_the_bottom_and_navigation_edits_in_place(engine, server, tg):
    """Production: commands edited the long-lived queue message (#18, far up the
    chat) or, when nothing changed, made no Telegram call at all, so every menu
    command looked ignored. Each command must now produce a visible answer."""
    matched(engine, {"a": 90})
    click(engine, server, tg, "show_queue")
    first = server.sent[-1]["message_id"]
    previous = first
    for command, view, marker in (("/menu", "menu", "JobRadar"), ("/new", "new", "Engineer a"),
                                  ("/liked", "liked", None), ("/applied", "applied", None),
                                  ("/skipped@jobradar_test_bot", "skipped", None), ("/start", "menu", "JobRadar"),
                                  ("/queue", "new", "Engineer a"), ("/new", "new", "Engineer a")):
        sent_before = len(server.sent)
        server.message_from(int(CHAT), "User", command)
        process_updates(engine, tg, CHAT)
        assert len(server.sent) == sent_before + 1, command      # a new answer, even if unchanged
        answer = server.sent[-1]
        if marker:
            assert marker in answer["text"], command
        retired = [e for e in server.edited if e["message_id"] == previous]
        assert retired and retired[-1]["reply_markup"] == {"inline_keyboard": []}
        assert "Moved" in retired[-1]["text"]
        with engine.connect() as conn:
            state = read_state(conn, CHAT)
        assert state["view"] == view and state["message_id"] == answer["message_id"]
        assert "retired_message_id" not in state
        previous = answer["message_id"]
    # Inline navigation keeps editing the current message in place.
    sent_before = len(server.sent)
    click(engine, server, tg, "q:menu", message_id=previous)
    assert "JobRadar" in server.edited[-1]["text"] and server.edited[-1]["message_id"] == previous
    assert len(server.edited[-1]["reply_markup"]["inline_keyboard"]) == 2
    click(engine, server, tg, f"q:open:{row(engine, 'a').id}:new:0", message_id=previous)
    buttons = [b for r in server.edited[-1]["reply_markup"]["inline_keyboard"] for b in r]
    assert any(b["text"] == "🏠 Main menu" for b in buttons)
    assert not any("q:list:liked" in b.get("callback_data", "") for b in buttons)
    assert "🕒" in server.edited[-1]["text"]
    assert len(server.sent) == sent_before


def test_command_answer_survives_a_failed_retire_and_a_failed_send(engine, server, tg):
    matched(engine, {"a": 90})
    server.message_from(int(CHAT), "User", "/new")
    process_updates(engine, tg, CHAT)
    first = server.sent[-1]["message_id"]
    # The old message can no longer be edited: the answer still arrives.
    server.fail("editMessageText", (400, {"ok": False, "error_code": 400,
                                          "description": "Bad Request: message can't be edited"}))
    server.message_from(int(CHAT), "User", "/liked")
    process_updates(engine, tg, CHAT)
    assert server.sent[-1]["message_id"] != first
    with engine.connect() as conn:
        assert read_state(conn, CHAT)["message_id"] == server.sent[-1]["message_id"]
    # A send that definitely failed is retried on the next command, never lost.
    server.fail("sendMessage", (500, {"ok": False, "error_code": 500, "description": "Internal"}))
    sent_before = len(server.sent)
    server.message_from(int(CHAT), "User", "/applied")
    process_updates(engine, tg, CHAT)
    server.message_from(int(CHAT), "User", "/applied")
    process_updates(engine, tg, CHAT)
    assert len(server.sent) == sent_before + 1
    with engine.connect() as conn:
        assert read_state(conn, CHAT)["view"] == "applied"


def test_alert_buttons_still_record_choices_without_moving_the_queue(engine, server, tg):
    matched(engine, {"a": 90})
    click(engine, server, tg, "show_queue")
    sent_before = len(server.sent)
    job_id = row(engine, "a").id
    click(engine, server, tg, f"applied:{job_id}", message_id=555)
    with engine.connect() as conn:
        assert conn.execute(select(job_reviews.c.choice).where(job_reviews.c.job_id == job_id)).scalar_one() \
            == "APPLIED"
    assert len(server.sent) == sent_before                       # no extra queue message


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


def test_mini_app_uses_the_same_freshness_rule_as_alerts(engine):
    """A job alerted under the discovery rule must also be listed in the Mini
    App; a newly added board's backlog must not be."""
    from datetime import UTC, datetime, timedelta
    from tests.factories import make_job
    from tests.integration.helpers import ingest
    now = datetime.now(UTC)
    ingest(engine, [make_job("seed", title="Office Manager", posted_at=now - timedelta(days=20))])
    ingest(engine, [make_job("seed", title="Office Manager", posted_at=now - timedelta(days=20)),
                    make_job("listed", title="Backend Engineer", posted_at=now - timedelta(days=8))])
    ingest(engine, [make_job("backlog", title="Platform Engineer", posted_at=now - timedelta(days=8))],
           board="newco")
    with engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.source_job_id.in_(["listed", "backlog"]))
                     .values(status="MATCHED", match_score=90, filtered_at=now))
        request_queue(conn, CHAT, min_score=60, max_posting_age_days=3, discovery_days=14)
        body, _ = render_queue(conn, read_state(conn, CHAT))
        strict_state = {**read_state(conn, CHAT), "max_discovery_age_days": None}
        strict_body, _ = render_queue(conn, strict_state)
    assert "Backend Engineer" in body and "Platform Engineer" not in body
    assert "newly listed and posted within 14 days" in body
    assert "Backend Engineer" not in strict_body


def test_show_queue_on_a_new_alert_moves_the_queue_down(engine, server, tg):
    matched(engine, {"a": 90})
    click(engine, server, tg, "show_queue")
    queue_message = server.sent[-1]["message_id"]
    click(engine, server, tg, "show_queue", message_id=queue_message)       # on itself: in place
    assert server.sent[-1]["message_id"] == queue_message
    click(engine, server, tg, "show_queue", message_id=777)                 # on a later alert: moves down
    assert server.sent[-1]["message_id"] != queue_message
    assert any(e["message_id"] == queue_message and e["reply_markup"] == {"inline_keyboard": []}
               for e in server.edited)

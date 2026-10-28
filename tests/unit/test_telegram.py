import httpx
import pytest

from jobradar.telegram import Telegram, TelegramError
from tests.fake_telegram import FakeTelegram


@pytest.fixture
def server():
    return FakeTelegram()


def bot(server, **kw):
    sleeps = []
    return Telegram(server.token, server.client(), sleep=sleeps.append, **kw), sleeps


def test_send_message_payload(server):
    tg, _ = bot(server)
    message_id = tg.send_message(42, "<b>hi</b>", [[{"text": "Apply", "url": "https://x"}]])
    assert message_id == 101
    (sent,) = server.sent
    assert sent["chat_id"] == 42 and sent["parse_mode"] == "HTML"
    assert sent["link_preview_options"] == {"is_disabled": True}
    assert sent["reply_markup"] == {"inline_keyboard": [[{"text": "Apply", "url": "https://x"}]]}


def test_get_updates_long_polls_with_a_longer_http_timeout(server, monkeypatch):
    tg, _ = bot(server)
    timeouts, real_post = [], tg._client.post

    def post(url, json, timeout):
        timeouts.append(timeout)
        return real_post(url, json=json, timeout=timeout)

    monkeypatch.setattr(tg._client, "post", post)
    tg.get_updates(5, timeout=25)
    assert server.calls[-1] == ("getUpdates", {"offset": 5, "timeout": 25,
                                               "allowed_updates": ["message", "callback_query"]})
    assert timeouts == [30]  # the HTTP request must outlive Telegram's long poll


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("slow"),
    (502, {"ok": False, "description": "Bad Gateway"})])
def test_scan_update_read_retries_same_offset_once(server, failure):
    server.fail("getUpdates", failure)
    tg, sleeps = bot(server)
    assert tg.get_updates(42) == []
    assert sleeps == [0.5]
    assert len(server.calls) == 2
    assert all(params["offset"] == 42 for _, params in server.calls)


def test_scan_update_read_stops_after_second_failure(server):
    server.fail("getUpdates", httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow again"))
    tg, sleeps = bot(server)
    with pytest.raises(TelegramError):
        tg.get_updates(42)
    assert len(server.calls) == 2 and sleeps == [0.5]


def test_edit_message_updates_text_and_controls_in_place(server):
    tg, _ = bot(server)
    buttons = [[{"text": "Like", "callback_data": "q:like:7:new:0"}]]
    tg.edit_message(42, 101, "<b>Your jobs</b>", buttons)
    assert server.sent == []
    assert server.edited == [{"chat_id": 42, "message_id": 101, "text": "<b>Your jobs</b>",
                             "parse_mode": "HTML", "link_preview_options": {"is_disabled": True},
                             "reply_markup": {"inline_keyboard": buttons}}]


def test_job_navigation_menu_is_chat_scoped_and_sends_no_message(server):
    tg, _ = bot(server)
    tg.setup_menu("42")
    method, payload = server.calls[0]
    assert method == "setMyCommands" and payload["scope"] == {"type": "chat", "chat_id": 42}
    assert [c["command"] for c in payload["commands"]] == ["new", "liked", "applied", "skipped", "menu"]
    assert server.calls[1] == ("setChatMenuButton", {"chat_id": 42, "menu_button": {"type": "commands"}})
    assert server.sent == []


@pytest.mark.parametrize("method", ["answerCallbackQuery", "editMessageReplyMarkup", "getUpdates", "sendMessage"])
def test_interactive_calls_fail_fast_instead_of_blocking_next_click(server, method, monkeypatch):
    server.fail(method, httpx.ConnectError("offline"))
    tg, sleeps = bot(server)
    timeouts, real_post = [], tg._client.post

    def post(url, json, timeout):
        timeouts.append(timeout)
        return real_post(url, json=json, timeout=timeout)

    monkeypatch.setattr(tg._client, "post", post)
    with pytest.raises(TelegramError):
        if method == "answerCallbackQuery":
            tg.answer_callback("cb1", "Saved")
        elif method == "editMessageReplyMarkup":
            tg.edit_buttons(42, 101, [])
        elif method == "sendMessage":
            tg.send_message(42, "Queue", http_timeout=3, max_attempts=1)
        else:
            tg.get_updates(0, timeout=5)
    assert sleeps == []
    assert len(server.calls) == 1
    assert timeouts == [10 if method == "getUpdates" else 3]


def test_rate_limit_waits_retry_after_then_succeeds(server):
    server.fail("sendMessage", (429, {"ok": False, "error_code": 429, "description": "Too Many Requests",
                                      "parameters": {"retry_after": 7}}))
    tg, sleeps = bot(server)
    assert tg.send_message(1, "x") == 101
    assert sleeps == [7] and len(server.sent) == 1


def test_connection_refused_is_retried_because_nothing_was_sent(server):
    server.fail("sendMessage", httpx.ConnectError("refused"))
    tg, sleeps = bot(server)
    tg.send_message(1, "x")
    assert len(sleeps) == 1 and len(server.sent) == 1


def test_gives_up_connecting_without_claiming_delivery(server):
    server.fail("sendMessage", *[httpx.ConnectError("refused")] * 3)
    tg, _ = bot(server)
    with pytest.raises(TelegramError, match="cannot connect") as exc:
        tg.send_message(1, "x")
    assert exc.value.maybe_delivered is False


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("reset")])
def test_failure_after_sending_is_never_retried(server, failure):
    server.fail("sendMessage", failure)
    tg, sleeps = bot(server)
    with pytest.raises(TelegramError) as exc:
        tg.send_message(1, "x")
    assert exc.value.maybe_delivered is True and sleeps == []
    assert sum(1 for m, _ in server.calls if m == "sendMessage") == 1


def test_api_error_is_definite_and_has_no_token(server):
    server.fail("sendMessage", (400, {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}))
    tg, _ = bot(server)
    with pytest.raises(TelegramError, match="chat not found") as exc:
        tg.send_message(1, "x")
    assert exc.value.maybe_delivered is False and server.token not in str(exc.value)


def test_server_error_might_have_been_delivered(server):
    server.fail("sendMessage", (502, {"ok": False, "description": "Bad Gateway"}))
    tg, _ = bot(server)
    with pytest.raises(TelegramError) as exc:
        tg.send_message(1, "x")
    assert exc.value.maybe_delivered is True


def test_wrong_token(server):
    tg = Telegram("bad-token", server.client())
    with pytest.raises(TelegramError, match="Unauthorized"):
        tg.get_me()


def test_delete_message(server):
    tg = Telegram(server.token, server.client())
    tg.delete_message(42, 7)
    assert server.deleted == [7] and server.calls[-1] == ("deleteMessage", {"chat_id": 42, "message_id": 7})

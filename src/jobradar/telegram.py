"""Minimal Telegram Bot API client (https://core.telegram.org/bots/api).

Retries are deliberately conservative. Telegram has no idempotency key, so
resending a message whose fate is unknown could deliver it twice. A request is
retried only when it certainly wasn't processed: the connection was never
made, or Telegram answered 429 "too many requests". Anything else that fails
mid-flight raises TelegramError(maybe_delivered=True).
"""

import time
from collections.abc import Callable
from typing import Any

import httpx

API_URL = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE_CHARS = 4096

Buttons = list[list[dict[str, str]]]  # rows of InlineKeyboardButton
MENU_COMMANDS = [
    {"command": "new", "description": "🆕 New jobs"},
    {"command": "liked", "description": "❤️ Liked jobs"},
    {"command": "applied", "description": "✅ Applied jobs"},
    {"command": "skipped", "description": "⏭️ Skipped jobs"},
    {"command": "menu", "description": "🏠 Main menu"},
]


class TelegramError(Exception):
    def __init__(self, message: str, *, maybe_delivered: bool = False) -> None:
        super().__init__(message)
        self.maybe_delivered = maybe_delivered


class Telegram:
    def __init__(self, token: str, client: httpx.Client,
                 sleep: Callable[[float], None] = time.sleep, max_attempts: int = 3) -> None:
        self._token, self._client, self._sleep, self._max_attempts = token, client, sleep, max_attempts

    def call(self, method: str, http_timeout: float = 20.0,
             max_attempts: int | None = None, **params: Any) -> Any:
        # Errors never include the URL: it contains the bot token.
        url = API_URL.format(token=self._token, method=method)
        attempts = self._max_attempts if max_attempts is None else max_attempts
        for attempt in range(1, attempts + 1):
            try:
                resp = self._client.post(url, json=params, timeout=http_timeout)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:  # never reached Telegram
                if attempt == attempts:
                    raise TelegramError(f"{method}: cannot connect ({type(exc).__name__})") from None
                self._sleep(2 ** attempt)
                continue
            except httpx.TransportError as exc:  # sent, but no answer: may have been processed
                if method == "getUpdates" and attempt < attempts:
                    # Repeating the same offset only rereads updates. Message
                    # sends keep their conservative uncertain-delivery rule.
                    self._sleep(0.5)
                    continue
                raise TelegramError(f"{method}: {type(exc).__name__}", maybe_delivered=True) from None
            if method == "getUpdates" and resp.status_code >= 500 and attempt < attempts:
                self._sleep(0.5)
                continue
            try:
                data = resp.json()
            except ValueError:
                raise TelegramError(f"{method}: HTTP {resp.status_code} with a non-JSON body",
                                    maybe_delivered=resp.status_code >= 500) from None
            if data.get("ok"):
                return data["result"]
            retry_after = (data.get("parameters") or {}).get("retry_after")
            if resp.status_code == 429 and retry_after is not None and attempt < attempts:
                self._sleep(min(retry_after, 5) if method == "getUpdates" else retry_after)
                continue
            raise TelegramError(f"{method}: {data.get('description') or f'HTTP {resp.status_code}'}",
                                maybe_delivered=resp.status_code >= 500)
        raise TelegramError(f"{method}: still rate limited after {attempts} attempts")

    def send_message(self, chat_id: str | int, text: str, buttons: Buttons | None = None, *,
                     http_timeout: float = 20.0, max_attempts: int | None = None) -> int:
        params: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                  "link_preview_options": {"is_disabled": True}}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        return self.call("sendMessage", http_timeout=http_timeout, max_attempts=max_attempts,
                         **params)["message_id"]

    def get_updates(self, offset: int, timeout: int = 0) -> list[dict[str, Any]]:
        """Updates from `offset` on. timeout>0 long-polls: Telegram holds the request open."""
        # The interactive long-poll loop already retries on its next iteration.
        # A one-shot scan gets one bounded retry for its safe update read.
        return self.call("getUpdates", http_timeout=timeout + 5, max_attempts=1 if timeout else 2,
                         offset=offset, timeout=timeout,
                         allowed_updates=["message", "callback_query"])

    def answer_callback(self, callback_query_id: str, text: str) -> None:
        self.call("answerCallbackQuery", http_timeout=3, max_attempts=1,
                  callback_query_id=callback_query_id, text=text)

    def edit_buttons(self, chat_id: str | int, message_id: int, buttons: Buttons) -> None:
        self.call("editMessageReplyMarkup", http_timeout=3, max_attempts=1,
                  chat_id=chat_id, message_id=message_id,
                  reply_markup={"inline_keyboard": buttons})

    def delete_message(self, chat_id: str | int, message_id: int) -> None:
        """Telegram lets a bot delete its own messages for 48 hours."""
        self.call("deleteMessage", http_timeout=3, max_attempts=1, chat_id=chat_id, message_id=message_id)

    def get_me(self) -> dict[str, Any]:
        return self.call("getMe")

    def setup_menu(self, chat_id: str | int) -> None:
        self.call("setMyCommands", commands=MENU_COMMANDS,
                  scope={"type": "chat", "chat_id": int(chat_id)})
        self.call("setChatMenuButton", chat_id=int(chat_id), menu_button={"type": "commands"})

    def edit_message(self, chat_id: str | int, message_id: int, text: str, buttons: Buttons) -> None:
        self.call("editMessageText", http_timeout=3, max_attempts=1, chat_id=chat_id,
                  message_id=message_id, text=text, parse_mode="HTML",
                  link_preview_options={"is_disabled": True},
                  reply_markup={"inline_keyboard": buttons})

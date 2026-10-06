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


class TelegramError(Exception):
    def __init__(self, message: str, *, maybe_delivered: bool = False) -> None:
        super().__init__(message)
        self.maybe_delivered = maybe_delivered


class Telegram:
    def __init__(self, token: str, client: httpx.Client,
                 sleep: Callable[[float], None] = time.sleep, max_attempts: int = 3) -> None:
        self._token, self._client, self._sleep, self._max_attempts = token, client, sleep, max_attempts

    def call(self, method: str, http_timeout: float = 20.0, **params: Any) -> Any:
        # Errors never include the URL: it contains the bot token.
        url = API_URL.format(token=self._token, method=method)
        for attempt in range(1, self._max_attempts + 1):
            try:
                resp = self._client.post(url, json=params, timeout=http_timeout)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:  # never reached Telegram
                if attempt == self._max_attempts:
                    raise TelegramError(f"{method}: cannot connect ({type(exc).__name__})") from None
                self._sleep(2 ** attempt)
                continue
            except httpx.TransportError as exc:  # sent, but no answer: may have been processed
                raise TelegramError(f"{method}: {type(exc).__name__}", maybe_delivered=True) from None
            try:
                data = resp.json()
            except ValueError:
                raise TelegramError(f"{method}: HTTP {resp.status_code} with a non-JSON body",
                                    maybe_delivered=resp.status_code >= 500) from None
            if data.get("ok"):
                return data["result"]
            retry_after = (data.get("parameters") or {}).get("retry_after")
            if resp.status_code == 429 and retry_after is not None and attempt < self._max_attempts:
                self._sleep(retry_after)  # rate limited: nothing was processed
                continue
            raise TelegramError(f"{method}: {data.get('description') or f'HTTP {resp.status_code}'}",
                                maybe_delivered=resp.status_code >= 500)
        raise TelegramError(f"{method}: still rate limited after {self._max_attempts} attempts")

    def send_message(self, chat_id: str | int, text: str, buttons: Buttons | None = None) -> int:
        params: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                  "link_preview_options": {"is_disabled": True}}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        return self.call("sendMessage", **params)["message_id"]

    def get_updates(self, offset: int, timeout: int = 0) -> list[dict[str, Any]]:
        """Updates from `offset` on. timeout>0 long-polls: Telegram holds the request open."""
        return self.call("getUpdates", http_timeout=timeout + 10, offset=offset, timeout=timeout,
                         allowed_updates=["message", "callback_query"])

    def answer_callback(self, callback_query_id: str, text: str) -> None:
        self.call("answerCallbackQuery", callback_query_id=callback_query_id, text=text)

    def edit_buttons(self, chat_id: str | int, message_id: int, buttons: Buttons) -> None:
        self.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                  reply_markup={"inline_keyboard": buttons})

    def get_me(self) -> dict[str, Any]:
        return self.call("getMe")

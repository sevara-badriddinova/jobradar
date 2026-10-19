"""An in-memory Telegram Bot API, served through httpx.MockTransport."""

import json
from typing import Any

import httpx


class FakeTelegram:
    def __init__(self, token: str = "123:SECRET") -> None:
        self.token = token
        self.sent: list[dict[str, Any]] = []          # sendMessage payloads, in order
        self.edited: list[dict[str, Any]] = []
        self.calls: list[tuple[str, dict]] = []       # (method, payload) for every request
        self.updates: list[dict[str, Any]] = []
        self.failures: dict[str, list[Any]] = {}      # method -> queued failures
        self._next_message_id = 100
        self._next_update_id = 1000

    # --- test controls ---------------------------------------------------------
    def fail(self, method: str, *failures: Any) -> None:
        """Queue failures: an httpx exception, or (status, json body)."""
        self.failures.setdefault(method, []).extend(failures)

    def press(self, data: str, chat_id: int | str = 42, message_id: int = 100) -> None:
        self._next_update_id += 1
        self.updates.append({"update_id": self._next_update_id, "callback_query": {
            "id": f"cb{self._next_update_id}", "data": data,
            "message": {"message_id": message_id, "chat": {"id": int(chat_id)}}}})

    def message_from(self, chat_id: int, name: str, text: str = "hi") -> None:
        self._next_update_id += 1
        self.updates.append({"update_id": self._next_update_id, "message": {
            "message_id": 1, "text": text, "chat": {"id": chat_id, "first_name": name}}})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self._handle))

    # --- the API -----------------------------------------------------------------
    def _handle(self, request: httpx.Request) -> httpx.Response:
        prefix = f"/bot{self.token}/"
        if not request.url.path.startswith(prefix):
            return httpx.Response(401, json={"ok": False, "error_code": 401, "description": "Unauthorized"})
        method = request.url.path.removeprefix(prefix)
        payload = json.loads(request.content or b"{}")
        self.calls.append((method, payload))
        if self.failures.get(method):
            failure = self.failures[method].pop(0)
            if isinstance(failure, Exception):
                raise failure
            status, body = failure
            return httpx.Response(status, json=body)
        return httpx.Response(200, json={"ok": True, "result": getattr(self, f"_{method}")(payload)})

    def _getMe(self, payload):
        return {"id": 1, "is_bot": True, "username": "jobradar_test_bot"}

    def _setMyCommands(self, payload):
        return True

    def _setChatMenuButton(self, payload):
        return True

    def _sendMessage(self, payload):
        self._next_message_id += 1
        self.sent.append(payload | {"message_id": self._next_message_id})
        return {"message_id": self._next_message_id, "chat": {"id": payload["chat_id"]}, "text": payload["text"]}

    def _getUpdates(self, payload):
        return [u for u in self.updates if u["update_id"] >= payload.get("offset", 0)]

    def _answerCallbackQuery(self, payload):
        return True

    def _editMessageReplyMarkup(self, payload):
        return True

    def _editMessageText(self, payload):
        self.edited.append(payload)
        return {"message_id": payload["message_id"]}

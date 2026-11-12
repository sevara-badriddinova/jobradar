from types import SimpleNamespace

import anthropic
import pytest
from pydantic import BaseModel

from jobradar import llm


class Answer(BaseModel):
    value: int


class FakeMessages:
    def __init__(self, result):
        self.result, self.kwargs = result, None

    def parse(self, **kwargs):
        self.kwargs = kwargs
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def client(monkeypatch):
    def install(result):
        messages = FakeMessages(result)
        monkeypatch.setattr(llm, "_client", lambda: SimpleNamespace(messages=messages))
        return messages
    return install


def test_returns_the_parsed_model_and_sends_the_right_request(client):
    messages = client(SimpleNamespace(parsed_output=Answer(value=7), stop_reason="end_turn"))
    assert llm.ask(Answer, system="sys", prompt="hi", model="m", max_tokens=100) == Answer(value=7)
    assert messages.kwargs == {"model": "m", "max_tokens": 100, "system": "sys",
                               "messages": [{"role": "user", "content": "hi"}], "output_format": Answer}


def test_missing_structured_output_is_an_error(client):
    client(SimpleNamespace(parsed_output=None, stop_reason="max_tokens"))
    with pytest.raises(llm.LLMError, match="stop_reason=max_tokens"):
        llm.ask(Answer, system="s", prompt="p", model="m")


def test_sdk_errors_become_llm_errors(client):
    client(anthropic.APIConnectionError(request=None))
    with pytest.raises(llm.LLMError, match="APIConnectionError"):
        llm.ask(Answer, system="s", prompt="p", model="m")


def test_missing_api_key_is_a_clear_llm_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    llm._client.cache_clear()
    try:
        with pytest.raises(llm.LLMError, match="ANTHROPIC_API_KEY is not set"):
            llm.ask(Answer, system="s", prompt="p", model="m")
    finally:
        llm._client.cache_clear()


def _api_error(cls, status, message):
    import httpx
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls(message=message, response=httpx.Response(status, request=request), body=None)


@pytest.mark.parametrize("cls,status,message,reason", [
    (anthropic.AuthenticationError, 401, "invalid x-api-key", "401 invalid or revoked API key"),
    (anthropic.PermissionDeniedError, 403, "permission_error", "403 key lacks permission"),
    (anthropic.BadRequestError, 400, "Your credit balance is too low to access the Anthropic API.",
     "account credit balance too low"),
])
def test_rejected_credentials_are_their_own_llm_error(client, cls, status, message, reason):
    client(_api_error(cls, status, message))
    with pytest.raises(llm.CredentialsRejected) as caught:
        llm.ask(Answer, system="s", prompt="p", model="m")
    assert caught.value.reason == reason
    assert isinstance(caught.value, llm.LLMError)   # callers that catch LLMError keep working


def test_other_bad_requests_stay_ordinary_llm_errors(client):
    client(_api_error(anthropic.BadRequestError, 400, "prompt is too long"))
    with pytest.raises(llm.LLMError) as caught:
        llm.ask(Answer, system="s", prompt="p", model="m")
    assert not isinstance(caught.value, llm.CredentialsRejected)


def test_missing_api_key_counts_as_rejected_credentials(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    llm._client.cache_clear()
    try:
        with pytest.raises(llm.CredentialsRejected):
            llm.ask(Answer, system="s", prompt="p", model="m")
    finally:
        llm._client.cache_clear()

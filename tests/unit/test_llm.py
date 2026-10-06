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

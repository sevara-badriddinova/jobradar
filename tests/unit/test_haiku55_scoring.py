"""Nov 14: Haiku 5.5 scores every job in one call, at low effort by default."""
from types import SimpleNamespace

import pytest

from jobradar import llm, matcher
from jobradar.settings import Preferences
from tests.factories import FakeLLM, assessment, candidate_profile
from tests.unit.test_matcher import job

GOOD = assessment(role_match=9, technical_match=9, experience_match=9)


@pytest.fixture
def defaults(monkeypatch):
    """Production defaults: no model, screen or effort overrides."""
    for name in ("JOBRADAR_SCREEN_MODEL", "JOBRADAR_MATCH_MODEL", "JOBRADAR_MATCH_EFFORT"):
        monkeypatch.delenv(name, raising=False)


def test_one_haiku55_call_per_job_at_low_effort(monkeypatch, defaults):
    fake = FakeLLM(GOOD)
    monkeypatch.setattr(matcher.llm, "ask", fake)
    a, model = matcher.assess(job(), candidate_profile().model_dump(), Preferences())
    assert model == "claude-haiku-5-5" and matcher.score(a) == 90.0
    assert [(c["model"], c["effort"], c["max_tokens"]) for c in fake.calls] == [
        ("claude-haiku-5-5", "low", matcher.MATCH_MAX_TOKENS)]


@pytest.mark.parametrize("setting,sent", [("medium", "medium"), ("LOW", "low"), ("default", None), ("", None)])
def test_effort_can_be_overridden(monkeypatch, defaults, setting, sent):
    monkeypatch.setenv("JOBRADAR_MATCH_EFFORT", setting)
    assert matcher.match_effort("claude-haiku-5-5") == sent


def test_older_models_get_no_effort_parameter(monkeypatch, defaults):
    monkeypatch.setenv("JOBRADAR_MATCH_MODEL", "claude-haiku-4-5-20251001")
    fake = FakeLLM(GOOD)
    monkeypatch.setattr(matcher.llm, "ask", fake)
    matcher.assess(job(), candidate_profile().model_dump(), Preferences())
    # Back to the old two-stage setup: the Haiku 5.5 screen, then Haiku 4.5 with no effort.
    assert [(c["model"], c["effort"]) for c in fake.calls] == [
        ("claude-haiku-5-5", "low"), ("claude-haiku-4-5-20251001", None)]


def test_the_audit_call_uses_the_same_effort(monkeypatch, defaults):
    rejected = assessment(eligible=False, ineligible_reason="unclear")
    fake = FakeLLM(rejected, GOOD)
    monkeypatch.setattr(matcher.llm, "ask", fake)
    matcher.assess(job(), candidate_profile().model_dump(), Preferences())
    assert [c["effort"] for c in fake.calls] == ["low", "low"]


class _Client:
    def __init__(self):
        self.kwargs = None
        self.messages = SimpleNamespace(parse=self.parse)

    def parse(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(usage=None, stop_reason="end_turn", parsed_output="ok")


@pytest.mark.parametrize("effort,config", [("low", {"effort": "low"}), (None, None)])
def test_ask_sends_effort_only_when_given(monkeypatch, effort, config):
    client = _Client()
    monkeypatch.setattr(llm, "_client", lambda: client)
    assert llm.ask(object, system="s", prompt="p", model="claude-haiku-5-5", effort=effort) == "ok"
    assert client.kwargs.get("output_config") == config

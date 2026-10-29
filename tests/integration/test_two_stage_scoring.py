"""Oct 29: a cheap screen model first; the main model only above the gate."""
import pytest
from sqlalchemy import select

from jobradar import matcher
from jobradar.db import job_matches
from jobradar.matcher import context_hash, run_matching
from jobradar.settings import Preferences
from tests.factories import FakeLLM, assessment, candidate_profile
from tests.integration.test_matcher import PREFS, add_profile, seed
from tests.integration.helpers import row
from tests.unit.test_matcher import job

LOW = assessment(role_match=5, technical_match=5, experience_match=5)    # 50
HIGH = assessment(role_match=7, technical_match=7, experience_match=7)   # 70
MAIN = assessment(role_match=9, technical_match=9, experience_match=9)   # 90


@pytest.fixture
def screened(monkeypatch):
    monkeypatch.setenv("JOBRADAR_SCREEN_MODEL", "screen-model")
    monkeypatch.delenv("JOBRADAR_SCREEN_MIN_SCORE", raising=False)


def by_model(answers):
    """FakeLLM answer per model: {model: assessment}."""
    def ask(output, *, system, prompt, model, max_tokens=4096):
        fake.calls.append({"model": model, "max_tokens": max_tokens, "system": system})
        return answers[model]
    fake = FakeLLM()
    return fake, ask


def test_screened_out_job_keeps_the_screen_assessment(monkeypatch, screened):
    fake, ask = by_model({"screen-model": LOW})
    monkeypatch.setattr(matcher.llm, "ask", ask)
    a, model = matcher.assess(job(), candidate_profile().model_dump(), Preferences())
    assert (matcher.score(a), model) == (50.0, "screen-model")
    assert [c["model"] for c in fake.calls] == ["screen-model"]
    assert fake.calls[0]["max_tokens"] == matcher.SCREEN_MAX_TOKENS


def test_job_above_the_gate_gets_the_main_model(monkeypatch, screened):
    fake, ask = by_model({"screen-model": HIGH, matcher.DEFAULT_MODEL: MAIN})
    monkeypatch.setattr(matcher.llm, "ask", ask)
    a, model = matcher.assess(job(), candidate_profile().model_dump(), Preferences())
    assert (matcher.score(a), model) == (90.0, matcher.DEFAULT_MODEL)
    assert [c["model"] for c in fake.calls] == ["screen-model", matcher.DEFAULT_MODEL]
    assert fake.calls[1]["max_tokens"] == 2048


def test_gate_is_configurable(monkeypatch, screened):
    monkeypatch.setenv("JOBRADAR_SCREEN_MIN_SCORE", "45")
    fake, ask = by_model({"screen-model": LOW, matcher.DEFAULT_MODEL: MAIN})
    monkeypatch.setattr(matcher.llm, "ask", ask)
    assert matcher.assess(job(), candidate_profile().model_dump(), Preferences())[1] == matcher.DEFAULT_MODEL


def test_screen_rejection_is_audited_by_the_screen_model(monkeypatch, screened):
    rejected = assessment(eligible=False, ineligible_reason="unclear")
    calls = []

    def ask(output, *, system, prompt, model, max_tokens=4096):
        calls.append((model, "Independently audit" in system))
        return rejected if not calls[1:] else LOW
    monkeypatch.setattr(matcher.llm, "ask", ask)
    a, model = matcher.assess(job(), candidate_profile().model_dump(), Preferences())
    assert calls == [("screen-model", False), ("screen-model", True)] and model == "screen-model"


@pytest.mark.parametrize("value", ["off", "", "none"])
def test_screen_can_be_turned_off(monkeypatch, value):
    monkeypatch.setenv("JOBRADAR_SCREEN_MODEL", value)
    fake = FakeLLM(LOW)
    monkeypatch.setattr(matcher.llm, "ask", fake)
    assert matcher.assess(job(), candidate_profile().model_dump(), Preferences())[1] == matcher.DEFAULT_MODEL
    assert [c["model"] for c in fake.calls] == [matcher.DEFAULT_MODEL]


def test_screen_does_not_change_the_cache_key(monkeypatch):
    base = context_hash(1, Preferences(), job())
    monkeypatch.setenv("JOBRADAR_SCREEN_MODEL", "screen-model")
    monkeypatch.setenv("JOBRADAR_SCREEN_MIN_SCORE", "75")
    assert context_hash(1, Preferences(), job()) == base


def test_default_screen_is_haiku_5_5(monkeypatch):
    monkeypatch.delenv("JOBRADAR_SCREEN_MODEL")
    assert matcher.screen_model() == "claude-haiku-5-5" and matcher.screen_min_score() == 60.0


def test_run_matching_stores_the_model_that_scored(engine, monkeypatch, screened):
    seed(engine, n=2)
    profile = add_profile(engine)

    def ask(output, *, system, prompt, model, max_tokens=4096):
        if model == "screen-model":
            return HIGH if "Backend Engineer 0" in prompt else LOW
        return MAIN
    monkeypatch.setattr(matcher.llm, "ask", ask)
    result = run_matching(engine, profile, PREFS)
    assert (result.assessed, result.failed) == (2, 0)
    with engine.connect() as conn:
        stored = {r.model: float(r.score) for r in conn.execute(select(job_matches.c.model, job_matches.c.score))}
    assert stored == {matcher.DEFAULT_MODEL: 90.0, "screen-model": 50.0}
    assert float(row(engine, "j0").match_score) == 90.0 and float(row(engine, "j1").match_score) == 50.0

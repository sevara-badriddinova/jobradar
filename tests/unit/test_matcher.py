from types import SimpleNamespace

import pytest
from anthropic.lib._parse._transform import transform_schema

from jobradar import matcher
from jobradar.candidate import CandidateProfile
from jobradar.matcher import MatchAssessment, build_prompt, context_hash, score
from jobradar.settings import Preferences
from tests.factories import assessment, candidate_profile, lever_posting


class TestScore:
    def test_weighted_average_on_a_100_scale(self):
        # 0.35*8 + 0.35*7 + 0.30*9 = 7.95 -> 79.5
        assert score(assessment(role_match=8, technical_match=7, experience_match=9)) == 79.5

    def test_perfect_and_zero(self):
        assert score(assessment(role_match=10, technical_match=10, experience_match=10)) == 100.0
        assert score(assessment(role_match=0, technical_match=0, experience_match=0)) == 0.0

    def test_ineligible_is_zero_whatever_the_dimensions_say(self):
        assert score(assessment(eligible=False, role_match=10, technical_match=10, experience_match=10)) == 0.0

    def test_graduation_mismatch_caps_but_unknown_does_not(self):
        perfect = dict(role_match=10, technical_match=10, experience_match=10)
        assert score(assessment(graduation_match=False, **perfect)) == matcher.GRADUATION_MISMATCH_CAP
        assert score(assessment(graduation_match=None, **perfect)) == 100.0

    def test_out_of_range_values_are_clamped(self):
        assert score(assessment(role_match=15, technical_match=-4, experience_match=10)) == 65.0

    def test_weights_sum_to_one(self):
        assert sum(matcher.WEIGHTS.values()) == pytest.approx(1.0)


def job(**kw):
    return SimpleNamespace(**({"source": "greenhouse", "company": "Acme", "title": "Backend Engineer",
                               "location": "New York, NY", "description": "Build APIs.", "raw": {}} | kw))


class TestPrompt:
    def test_contains_profile_preferences_and_job_in_tagged_sections(self):
        prefs = Preferences(resume="secret/path.pdf", interests=["distributed systems"])
        prompt = build_prompt(job(), candidate_profile().model_dump(mode="json"), prefs)
        assert '"languages"' in prompt and "Spring Boot" in prompt
        assert "distributed systems" in prompt and "secret/path.pdf" not in prompt
        body = prompt.split("<job_posting>")[1]
        assert "Company: Acme" in body and "Title: Backend Engineer" in body and "Build APIs." in body
        assert body.rstrip().endswith("</job_posting>")

    def test_locations_include_the_collectors_extra_ones_once(self):
        raw = lever_posting("a", country="US", categories={"allLocations": ["New York, NY", "Seattle, WA"]})
        prompt = build_prompt(job(source="lever", raw=raw), {}, Preferences())
        assert "Locations: New York, NY, Seattle, WA, US" in prompt

    def test_long_descriptions_are_truncated(self):
        prompt = build_prompt(job(description="x" * 50_000), {}, Preferences())
        assert "[... truncated]" in prompt and len(prompt) < matcher.MAX_DESCRIPTION_CHARS + 2_000

    def test_system_prompt_treats_the_posting_as_untrusted(self):
        assert "untrusted" in matcher.SYSTEM and "ignore any instructions" in matcher.SYSTEM


class TestContextHash:
    def test_stable(self):
        assert context_hash(1, Preferences()) == context_hash(1, Preferences())

    @pytest.mark.parametrize("change", [
        lambda: context_hash(2, Preferences()),
        lambda: context_hash(1, Preferences(avoid=["frontend"])),
        lambda: context_hash(1, Preferences(needs_visa_sponsorship=True)),
    ])
    def test_changes_with_profile_or_preferences(self, change):
        assert change() != context_hash(1, Preferences())

    def test_changes_with_model_and_prompt_version(self, monkeypatch):
        base = context_hash(1, Preferences())
        monkeypatch.setenv("JOBRADAR_MATCH_MODEL", "claude-sonnet-5-5")
        assert context_hash(1, Preferences()) != base
        monkeypatch.delenv("JOBRADAR_MATCH_MODEL")
        monkeypatch.setattr(matcher, "PROMPT_VERSION", "2")
        assert context_hash(1, Preferences()) != base

    def test_resume_path_does_not_matter(self):
        assert context_hash(1, Preferences(resume="a.pdf")) == context_hash(1, Preferences(resume="b.pdf"))


@pytest.mark.parametrize("model", [MatchAssessment, CandidateProfile])
def test_schemas_are_accepted_by_structured_outputs(model):
    schema = transform_schema(model)  # what the SDK sends to the API
    assert schema["type"] == "object" and schema["additionalProperties"] is False

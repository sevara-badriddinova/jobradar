from datetime import date

import pytest

from jobradar import matcher
from jobradar.candidate import normalize_experience
from jobradar.filters import reject_reason, sponsorship_evidence
from jobradar.settings import FilterSettings, Preferences
from tests.factories import FakeLLM, assessment, candidate_profile
from tests.unit.test_matcher import job


def work(start, end, kind="full_time"):
    return dict(start=start, end=end, kind=kind)


def test_pregraduation_work_counts_and_overlaps_do_not_double_count():
    p = {"graduation": "2026-12", "years_of_experience": 0, "work_experience": [
        work("2023-02", "2024-09"), work("2024-02", "2024-06"),
        work("2026-05", "2026-08", "internship")]}
    assert normalize_experience(p, date(2026, 10, 13))["years_of_experience"] == 1.58
    assert p["years_of_experience"] == 0


def test_present_future_and_unknown_dates():
    p = {"work_experience": [work("2026-01", "present"), work("2027-01", "2028-01"),
                             work(None, "2024-01"), work("bad", "present")]}
    assert normalize_experience(p, date(2026, 10, 13))["years_of_experience"] == 0.75


@pytest.mark.parametrize("text", [
    "We provide visa sponsorship for this position.",
    "Visa sponsorship is available for this role.",
    "We sponsor visas.",
    "We can sponsor qualified candidates.",
    "STEM OPT candidates are welcome.",
    "We support STEM OPT candidates.",
    "This role is eligible for H-1B visa sponsorship.",
    "Visa sponsorship: available.",
])
def test_affirmative_role_support(text):
    assert sponsorship_evidence(text) == text


@pytest.mark.parametrize("text", [
    "We do not provide visa sponsorship.", "Will you require visa sponsorship?",
    "We are an E-Verify employer.", "We have sponsored H-1B employees in the past.",
    "Visa sponsorship may be available.", "Visa sponsorship is available for some roles.",
    "We provide visa sponsorship on a case-by-case basis.",
    "We provide visa sponsorship for transfers only.",
    "US remote position. Equal opportunity employer.",
    "We provide sponsorship for local sports teams.",
    "We do sponsor visas! However, we aren't able to successfully sponsor visas for every role and every candidate.",
])
def test_unknown_or_conditional_support_is_not_confirmation(text):
    assert sponsorship_evidence(text) is None


def test_explicit_refusal_wins_over_positive_boilerplate():
    reason = reject_reason(source="greenhouse", title="Software Engineer", location="US", raw={},
        description="We provide visa sponsorship. This role cannot sponsor candidates.",
        settings=FilterSettings(require_explicit_sponsorship=True), needs_sponsorship=True)
    assert reason and reason.startswith("visa:")


def test_reviewer_corrects_false_missing_skill_and_experience(monkeypatch):
    p = candidate_profile().model_dump()
    bad = assessment(eligible=False, ineligible_reason="Missing Go", missing_skills=["Go"], technical_match=0)
    good = assessment(eligible=True, missing_skills=[], technical_match=9)
    fake = FakeLLM(bad, good)
    monkeypatch.setattr(matcher.llm, "ask", fake)
    got = matcher.assess_with_review(job(), p, Preferences())
    assert got.eligible and got.technical_match == 9 and not got.missing_skills
    assert len(fake.calls) == 2 and "Independently audit" in fake.calls[1]["system"]


def test_unsupported_rejection_stays_a_concern_not_zero(monkeypatch):
    fake = FakeLLM(assessment(eligible=False, ineligible_reason="Generic export control",
        hard_blockers=[dict(kind="citizenship", job_quote="Candidates may need to meet legal status requirements.",
                            candidate_fact="Needs sponsorship")]))
    monkeypatch.setattr(matcher.llm, "ask", fake)
    a = matcher.assess_with_review(job(description="Candidates may need to meet legal status requirements."),
                                  candidate_profile().model_dump(), Preferences())
    assert a.eligible and matcher.score(a) > 0
    assert "Unconfirmed" in a.concerns[-1]


def test_real_experience_blocker_requires_quote_and_conflicting_dates(monkeypatch):
    quote = "At least 5 years of professional experience required."
    fake = FakeLLM(assessment(eligible=False, ineligible_reason="5 years required",
        hard_blockers=[dict(kind="experience", job_quote=quote,
                            candidate_field="years_of_experience", candidate_fact="0.0")]))
    monkeypatch.setattr(matcher.llm, "ask", fake)
    a = matcher.assess_with_review(job(description=quote), candidate_profile().model_dump(), Preferences())
    assert not a.eligible and matcher.score(a) == 0
    a = matcher.assess_with_review(job(description="No minimum experience."),
                                  candidate_profile().model_dump(), Preferences())
    assert a.eligible  # fabricated quote cannot veto a job


def test_known_skill_cannot_stay_missing_after_review(monkeypatch):
    monkeypatch.setattr(matcher.llm, "ask", FakeLLM(assessment(missing_skills=["Go", "Kafka"])))
    a = matcher.assess_with_review(job(), candidate_profile().model_dump(), Preferences())
    assert a.missing_skills == ["Kafka"]


def test_content_and_secondary_locations_change_cache_key():
    base = matcher.context_hash(1, Preferences(), job())
    assert base != matcher.context_hash(1, Preferences(), job(description="Different requirements"))
    assert base != matcher.context_hash(1, Preferences(), job(raw={"offices": [{"location": "London"}]}))


def test_real_job_quote_with_fabricated_candidate_fact_cannot_reject(monkeypatch):
    quote = "At least 5 years of professional experience required."
    fake = FakeLLM(assessment(eligible=False, ineligible_reason="Invented experience",
        hard_blockers=[dict(kind="experience", job_quote=quote,
                            candidate_field="years_of_experience", candidate_fact="0.0")]))
    monkeypatch.setattr(matcher.llm, "ask", fake)
    p = candidate_profile().model_dump()
    p["work_experience"][0].update(kind="full_time", start="2020-01", end="2026-01")
    a = matcher.assess_with_review(job(description=quote), p, Preferences())
    assert a.eligible and not a.hard_blockers


@pytest.mark.parametrize('text', [
    'Authorization to receive technology without sponsorship for an export license.',
    'We cannot sponsor an export license.',
    'No sponsorship for an export licence.',
])
def test_export_license_sponsorship_is_not_a_visa_refusal(text):
    from jobradar.filters import sponsorship_reason
    assert sponsorship_reason(text) is None
    assert sponsorship_evidence(text) is None  # not a positive immigration offer either


@pytest.mark.parametrize('refusal', [
    'We do not provide visa sponsorship.', 'This role cannot sponsor candidates.',
    'Must be a US citizen.', 'Security clearance required.',
    'Employment requires authorization without sponsorship.',
])
def test_export_license_exception_does_not_hide_other_restrictions(refusal):
    from jobradar.filters import sponsorship_reason
    description = 'Authorization without sponsorship for an export license. ' + refusal
    assert sponsorship_reason(description) is not None


def test_export_license_does_not_block_an_explicit_visa_offer():
    from datetime import UTC, datetime
    reason = reject_reason(source='greenhouse', title='Software Engineer', location='US', raw={},
        description='We offer visa sponsorship for this role. Authorization without sponsorship for an export license.',
        settings=FilterSettings(max_posting_age_days=3, require_explicit_sponsorship=True),
        needs_sponsorship=True, posted_at=datetime.now(UTC))
    assert reason is None

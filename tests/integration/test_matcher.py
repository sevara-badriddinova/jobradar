import pytest
from sqlalchemy import func, insert, select, update

from jobradar import cli, matcher
from jobradar.db import candidate_profiles, job_matches, jobs as jobs_t
from jobradar.filters import run_filters
from jobradar.llm import LLMError
from jobradar.matcher import context_hash, run_matching
from jobradar.settings import FilterSettings, Preferences
from tests.factories import FakeLLM, assessment, candidate_profile, make_job
from tests.integration.helpers import ingest, row

PREFS = Preferences(interests=["backend"])


def add_profile(engine, **overrides):
    with engine.begin() as conn:
        return conn.execute(insert(candidate_profiles).values(
            resume_sha256="x", prompt_version="1", model="m",
            profile=candidate_profile(**overrides).model_dump(mode="json")).returning(candidate_profiles)).one()


def seed(engine, n=3):
    """n jobs that pass the filters, plus one rejected and one not yet filtered."""
    ingest(engine, [make_job(f"j{i}", title=f"Backend Engineer {i}") for i in range(n)]
           + [make_job("senior", title="Senior Engineer")])
    with engine.begin() as conn:
        run_filters(conn, FilterSettings())
    ingest(engine, [make_job("late", title="Platform Engineer")], board="later")


@pytest.fixture
def llm(monkeypatch):
    def install(*answers):
        fake = FakeLLM(*answers)
        monkeypatch.setattr(matcher.llm, "ask", fake)
        return fake
    return install


def matches_count(engine):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(job_matches)).scalar_one()


def test_matches_only_open_jobs_that_passed_the_filters(engine, llm):
    seed(engine)
    profile = add_profile(engine)
    fake = llm(assessment(role_match=8, technical_match=7, experience_match=9))
    result = run_matching(engine, profile, PREFS)

    assert (result.assessed, result.cached, result.failed) == (3, 0, 0)
    assert {c["prompt"].split("Title: ")[1].split("\n")[0] for c in fake.calls} == {
        "Backend Engineer 0", "Backend Engineer 1", "Backend Engineer 2"}
    j = row(engine, "j0")
    assert (j.status, float(j.match_score), j.match_id is not None) == ("MATCHED", 79.5, True)
    assert row(engine, "senior").status == "FILTERED" and row(engine, "late").status == "DISCOVERED"

    with engine.connect() as conn:
        m = conn.execute(select(job_matches).where(job_matches.c.id == j.match_id)).one()
    assert m.profile_id == profile.id and m.assessment["missing_skills"] == ["Kafka"]
    assert m.context_hash == context_hash(profile.id, PREFS, j, profile.profile) and float(m.score) == 79.5


def test_running_again_does_nothing(engine, llm):
    seed(engine)
    profile = add_profile(engine)
    fake = llm(assessment())
    run_matching(engine, profile, PREFS)
    again = run_matching(engine, profile, PREFS)
    assert (again.assessed, again.cached, len(fake.calls)) == (0, 0, 3)


def test_review_skips_do_not_spend_ai_calls(engine, llm):
    from jobradar.review import set_choice
    seed(engine)
    profile = add_profile(engine)
    fake = llm(assessment())
    with engine.begin() as conn:
        set_choice(conn, row(engine, "j0").id, "IGNORED")
        set_choice(conn, row(engine, "j1").id, "SPONSORSHIP")
    assert run_matching(engine, profile, PREFS).assessed == 1
    assert len(fake.calls) == 1


def test_rematch_reuses_cached_answers_when_nothing_changed(engine, llm):
    seed(engine)
    profile = add_profile(engine)
    fake = llm(assessment())
    run_matching(engine, profile, PREFS)
    result = run_matching(engine, profile, PREFS, rematch=True)
    assert (result.assessed, result.cached, len(fake.calls)) == (0, 3, 3)


def test_rematch_after_changing_preferences_or_profile_asks_again(engine, llm):
    seed(engine)
    profile = add_profile(engine)
    fake = llm(assessment(role_match=8), assessment(role_match=2))
    run_matching(engine, profile, PREFS)
    result = run_matching(engine, profile, Preferences(avoid=["backend"]), rematch=True)
    assert result.assessed == 3 and len(fake.calls) == 6 and matches_count(engine) == 6
    j = row(engine, "j0")
    with engine.connect() as conn:
        assert conn.execute(select(job_matches.c.assessment).where(
            job_matches.c.id == j.match_id)).scalar_one()["role_match"] == 2  # points at the newest

    new_profile = add_profile(engine, languages=["Rust"])
    assert run_matching(engine, new_profile, PREFS, rematch=True).assessed == 3


def test_limit_takes_the_newest_jobs_first(engine, llm):
    seed(engine, n=5)
    fake = llm(assessment())
    profile = add_profile(engine)
    result = run_matching(engine, profile, PREFS, limit=2)
    assert result.assessed == 2
    assert "Backend Engineer 4" in fake.calls[0]["prompt"] or "Backend Engineer 3" in fake.calls[0]["prompt"]
    assert run_matching(engine, profile, PREFS, limit=10).assessed == 3  # the rest


def test_one_failure_does_not_stop_the_rest_and_is_retried_next_time(engine, llm):
    seed(engine)
    profile = add_profile(engine)
    llm(assessment(), LLMError("overloaded"), assessment())
    result = run_matching(engine, profile, PREFS)
    assert (result.assessed, result.failed, result.aborted) == (2, 1, False)
    llm(assessment())
    assert run_matching(engine, profile, PREFS).assessed == 1


def test_stops_after_repeated_failures(engine, llm):
    seed(engine, n=6)
    fake = llm(LLMError("invalid x-api-key"))
    result = run_matching(engine, add_profile(engine), PREFS)
    assert result.aborted and result.failed == matcher.MAX_CONSECUTIVE_FAILURES
    assert len(fake.calls) == matcher.MAX_CONSECUTIVE_FAILURES


def test_progress_is_saved_job_by_job(engine, llm):
    seed(engine)
    llm(assessment(), assessment(), KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        run_matching(engine, add_profile(engine), PREFS)
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(jobs_t)
                            .where(jobs_t.c.status == "MATCHED")).scalar_one() == 2


def test_closed_jobs_are_not_matched(engine, llm):
    seed(engine)
    with engine.begin() as conn:
        conn.execute(update(jobs_t).where(jobs_t.c.source_job_id == "j0").values(closed_at=func.now()))
    llm(assessment())
    assert run_matching(engine, add_profile(engine), PREFS).assessed == 2


def test_concurrent_run_storing_first_wins(engine, monkeypatch):
    seed(engine, n=1)
    profile = add_profile(engine)
    key = context_hash(profile.id, PREFS, row(engine, "j0"), profile.profile)
    job_id = row(engine, "j0").id

    def other_process_got_there_first(output, *, system, prompt, model, max_tokens=4096):
        with engine.begin() as conn:
            conn.execute(insert(job_matches).values(
                job_id=job_id, profile_id=profile.id, context_hash=key, model="m", prompt_version="1",
                assessment=assessment(role_match=10).model_dump(mode="json"), score=42))
        return assessment(role_match=0)

    monkeypatch.setattr(matcher.llm, "ask", other_process_got_there_first)
    run_matching(engine, profile, PREFS)
    assert float(row(engine, "j0").match_score) == 42 and matches_count(engine) == 1


class TestCli:
    def test_match_needs_a_profile(self, engine, db_env, capsys):
        assert cli.main(["match"]) == 2
        assert "no profile yet" in capsys.readouterr().err

    def test_match_then_top(self, engine, db_env, llm, capsys):
        seed(engine)
        add_profile(engine)
        with engine.begin() as conn:
            conn.execute(update(jobs_t).where(jobs_t.c.source_job_id == "j0").values(
                description="At least 5 years of professional experience required."))
        llm(assessment(role_match=10, technical_match=10, experience_match=10, reasons=["Great fit"]),
            assessment(role_match=5, technical_match=5, experience_match=5),
            assessment(eligible=False, ineligible_reason="needs 5 years", concerns=["Experience"],
                       hard_blockers=[{"kind": "experience", "job_quote": "At least 5 years of professional experience required.",
                                       "candidate_field": "years_of_experience", "candidate_fact": "0.0"}]))
        assert cli.main(["match"]) == 0
        assert "assessed 3, reused 0 cached, failed 0" in capsys.readouterr().out

        assert cli.main(["top"]) == 0
        out = capsys.readouterr().out
        assert "100.0  Acme | Backend Engineer" in out and "+ Great fit" in out and "missing: Kafka" in out
        assert "1 match(es) scoring 70+" in out

        assert cli.main(["top", "--min-score", "0"]) == 0
        out = capsys.readouterr().out
        assert "3 match(es)" in out and out.index("100.0") < out.index(" 50.0") < out.index("  0.0")

    def test_match_exit_code_reports_failures(self, engine, db_env, llm, capsys):
        seed(engine, n=1)
        add_profile(engine)
        llm(LLMError("boom"))
        assert cli.main(["match"]) == 1

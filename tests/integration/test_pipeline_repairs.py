from datetime import UTC, datetime, timedelta

from sqlalchemy import insert, select, update

from jobradar import cli, matcher
from jobradar.candidate import refresh_experience
from jobradar.companies import CompanySpec, list_companies, sync_companies
from jobradar.db import candidate_profiles, collector_runs, jobs, notifications
from jobradar.filters import run_filters
from jobradar.matcher import run_matching
from jobradar.notifications import enqueue, send_immediate
from jobradar.settings import FilterSettings, NotificationSettings, Preferences, Settings
from jobradar.telegram import Telegram
from tests.factories import FakeLLM, assessment, candidate_profile, make_job
from tests.fake_telegram import FakeTelegram
from tests.integration.helpers import ingest, row
from tests.integration.test_matcher import add_profile, seed


def test_stale_jobs_progress_beyond_batch_limit_without_rematch(engine, monkeypatch):
    seed(engine, n=6)
    p = add_profile(engine)
    fake = FakeLLM(assessment())
    monkeypatch.setattr(matcher.llm, "ask", fake)
    assert run_matching(engine, p, Preferences()).assessed == 6
    preferences = Preferences(interests=["changed"])
    assert [run_matching(engine, p, preferences, limit=2).assessed for _ in range(4)] == [2, 2, 2, 0]
    assert len(fake.calls) == 12


def test_changed_content_invalidates_score_and_reassesses(engine, monkeypatch):
    ingest(engine, [make_job("a")])
    p = add_profile(engine)
    monkeypatch.setattr(matcher.llm, "ask", FakeLLM(assessment(role_match=0), assessment(role_match=10)))
    with engine.begin() as c:
        run_filters(c, FilterSettings())
    run_matching(engine, p, Preferences())
    old = row(engine, "a")
    ingest(engine, [make_job("a", description="Build APIs. We provide visa sponsorship.")])
    assert row(engine, "a").status == "DISCOVERED" and row(engine, "a").match_score is None
    with engine.begin() as c:
        run_filters(c, FilterSettings())
    assert run_matching(engine, p, Preferences()).assessed == 1
    assert row(engine, "a").match_id != old.match_id
    assert row(engine, "a").match_score > old.match_score


def test_job_changed_during_ai_call_cannot_receive_obsolete_score(engine, monkeypatch):
    ingest(engine, [make_job("a")])
    with engine.begin() as c:
        run_filters(c, FilterSettings())
    def change(output, **kwargs):
        ingest(engine, [make_job("a", description="Different requirements.")])
        return assessment()
    monkeypatch.setattr(matcher.llm, "ask", change)
    run_matching(engine, add_profile(engine), Preferences())
    assert row(engine, "a").status == "DISCOVERED" and row(engine, "a").match_score is None


def test_repost_flows_to_telegram_once_with_strict_policy(engine, db_env, monkeypatch):
    now = datetime.now(UTC)
    description = "Build backend APIs. We provide visa sponsorship for this position."
    ingest(engine, [make_job("old", description=description, posted_at=now - timedelta(days=10))])
    with engine.begin() as c:
        c.execute(update(jobs).where(jobs.c.source_job_id == "old").values(closed_at=now))
    ingest(engine, [make_job("repost", description=description, posted_at=now - timedelta(hours=2))])
    assert row(engine, "repost").status == "REPOSTED"
    policy = Settings(filters=FilterSettings(max_posting_age_days=3, require_explicit_sponsorship=True),
                      preferences=Preferences(needs_visa_sponsorship=True))
    monkeypatch.setattr(cli, "load_settings", lambda path: policy)
    monkeypatch.setattr(matcher.llm, "ask", FakeLLM(assessment(role_match=10, technical_match=10)))
    add_profile(engine)
    server = FakeTelegram()
    monkeypatch.setattr(cli, "make_client", server.client)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", server.token)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    assert cli.main(["filter"]) == 0
    assert cli.main(["match"]) == 0
    assert cli.main(["notify", "--digest"]) == 0
    assert row(engine, "repost").status == "NOTIFIED"
    with engine.connect() as c:
        sent = c.execute(select(notifications).where(notifications.c.job_id == row(engine, "repost").id)).one()
        assert sent.status == "sent" and sent.attempts == 1
    assert cli.main(["notify", "--digest"]) == 0
    with engine.connect() as c:
        assert c.execute(select(notifications.c.attempts).where(notifications.c.id == sent.id)).scalar_one() == 1


def test_strict_policy_does_not_send_older_or_sponsorship_unknown_jobs(engine, db_env, monkeypatch):
    from tests.integration.test_notifications import matched
    matched(engine, {"old": 95, "unknown": 95})
    with engine.begin() as c:
        c.execute(update(jobs).where(jobs.c.source_job_id == "unknown").values(posted_at=datetime.now(UTC)))
    policy = Settings(filters=FilterSettings(max_posting_age_days=3, require_explicit_sponsorship=True))
    monkeypatch.setattr(cli, "load_settings", lambda path: policy)
    server = FakeTelegram()
    monkeypatch.setattr(cli, "make_client", server.client)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", server.token)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    assert cli.main(["notify", "--digest"]) == 0
    with engine.connect() as c:
        assert not c.execute(select(notifications)).all()


def test_experience_correction_versions_existing_profile_without_llm(engine):
    p = candidate_profile().model_dump()
    p["work_experience"][0].update(kind="full_time", start="2023-02", end="2024-09")
    with engine.begin() as c:
        c.execute(insert(candidate_profiles).values(profile=p, resume_sha256="original", model="original", prompt_version="1"))
        corrected = refresh_experience(c)
        assert corrected.id == 2 and corrected.profile["years_of_experience"] == 1.58
        assert refresh_experience(c).id == 2


def test_cancelled_unattempted_notification_can_be_recovered(engine):
    from tests.integration.test_notifications import matched
    matched(engine, {"a": 95})
    with engine.begin() as c:
        enqueue(c, NotificationSettings())
        c.execute(update(notifications).values(status="cancelled"))
        assert enqueue(c, NotificationSettings()) == {"immediate": 1, "digest": 0}
    server = FakeTelegram()
    tg = Telegram(server.token, server.client())
    assert send_immediate(engine, tg, "42").sent == 1
    with engine.begin() as c:
        assert enqueue(c, NotificationSettings()) == {"immediate": 0, "digest": 0}


def test_batch_rotation_prefers_unpolled_then_oldest_even_after_errors(engine):
    with engine.begin() as c:
        sync_companies(c, [CompanySpec(name=n, collector="greenhouse", board=n) for n in ["a", "b", "c"]])
        assert [r.board for r in list_companies(c, limit=2)] == ["a", "b"]
        c.execute(insert(collector_runs).values(source="greenhouse", board="a", ok=False))
        c.execute(insert(collector_runs).values(source="greenhouse", board="b", ok=True))
        assert [r.board for r in list_companies(c, limit=2)] == ["c", "a"]


def test_applied_repost_never_reaches_matching(engine, monkeypatch):
    from jobradar.applications import link_applications, mark_applied_by_hand
    ingest(engine, [make_job("old")])
    with engine.begin() as c:
        mark_applied_by_hand(c, row(engine, "old").id)
        c.execute(update(jobs).values(closed_at=datetime.now(UTC)))
    ingest(engine, [make_job("repost")])
    assert row(engine, "repost").status == "REPOSTED"
    with engine.begin() as c:
        link_applications(c)
        run_filters(c, FilterSettings())
    assert row(engine, "repost").status == "APPLIED"
    fake = FakeLLM(assessment())
    monkeypatch.setattr(matcher.llm, "ask", fake)
    assert run_matching(engine, add_profile(engine), Preferences()).assessed == 0


def test_parallel_listing_of_live_repost_is_duplicate(engine):
    ingest(engine, [make_job("old")])
    with engine.begin() as c:
        c.execute(update(jobs).values(closed_at=datetime.now(UTC)))
    ingest(engine, [make_job("repost"), make_job("parallel")])
    assert row(engine, "repost").status == "REPOSTED"
    assert row(engine, "parallel").status == "DUPLICATE"
    assert row(engine, "parallel").duplicate_of_id == row(engine, "repost").id

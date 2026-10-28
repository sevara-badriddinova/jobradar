"""Sponsorship, three ways: explicit support -> AI pipeline; explicit refusal ->
rejected; not stated -> a persistent, deduplicated review queue (no AI, no alert)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text, update

from jobradar import sponsorship_review as review
from jobradar.db import jobs as jobs_t, sponsorship_reviews
from jobradar.filters import reject_reason, run_filters
from jobradar.notifications import _eligible
from jobradar.settings import FilterSettings, Preferences
from jobradar.sponsorship_review import UNKNOWN_REASON
from tests.factories import assessment, make_job
from tests.integration.helpers import ingest, row
from tests.integration.test_matcher import add_profile
from tests.integration.test_migrations import scratch  # noqa: F401 - fixture
from tests.integration.test_workers import (SETTINGS, add_board, boards, llm, make_worker,  # noqa: F401
                                            poll, tasks, telegram)

NOW = datetime.now(UTC)
POLICY = FilterSettings(max_posting_age_days=3, require_explicit_sponsorship=True)
YES = "Build backend services. We sponsor H-1B visas for this role."
NO = "Build backend services. We are unable to sponsor visas for this position."
UNKNOWN = "Build backend services in Python and Go."


def classify(description, location="New York, NY", title="Software Engineer"):
    return reject_reason(source="greenhouse", title=title, location=location, raw={}, description=description,
                         settings=POLICY, needs_sponsorship=True, posted_at=NOW - timedelta(hours=1))


# --- classification, including ambiguous wording ---------------------------------------------

@pytest.mark.parametrize("text_,expected", [
    (YES, None),
    ("Visa sponsorship is available for this role.", None),
    ("We support STEM OPT and H-1B sponsorship for this position.", None),
    (NO, "refusal"),
    ("This role does not offer visa sponsorship.", "refusal"),
    ("Candidates must be able to work without sponsorship now or in the future.", "refusal"),
    ("Must be a U.S. citizen.", "refusal"),
    ("An active security clearance is required.", "refusal"),
    (UNKNOWN, "unknown"),
    # application questions and boilerplate are neither a yes nor a no
    ("Will you now or in the future require visa sponsorship?", "unknown"),
    ("We are an E-Verify employer.", "unknown"),
    ("Applicants must be authorized to work in the United States.", "unknown"),
    ("We may consider sponsorship for exceptional candidates.", "unknown"),
    ("H-1B transfers only.", "unknown"),
    ("Historically we have sponsored visas.", "unknown"),
    # a refusal always wins over positive boilerplate
    ("We provide visa sponsorship. This role cannot sponsor candidates.", "refusal"),
])
def test_three_way_classification(text_, expected):
    reason = classify(text_)
    if expected is None:
        assert reason is None
    elif expected == "unknown":
        assert reason == UNKNOWN_REASON
    else:
        assert reason and reason.startswith("visa:") and reason != UNKNOWN_REASON


def test_only_jobs_passing_every_other_filter_are_held():
    assert classify(UNKNOWN, location="London, United Kingdom").startswith("location:")
    assert classify(UNKNOWN, title="Senior Software Engineer") == "title: senior"
    assert classify(UNKNOWN + " Requires 5+ years of experience.").startswith("experience:")


# --- the queue ----------------------------------------------------------------------------

def held(engine):
    with engine.connect() as conn:
        return conn.execute(select(sponsorship_reviews).order_by(sponsorship_reviews.c.id)).all()


def filter_now(engine):
    with engine.begin() as conn:
        return run_filters(conn, POLICY, needs_sponsorship=True)


def posting(sid, description=UNKNOWN, **kw):
    kw.setdefault("posted_at", NOW - timedelta(hours=2))
    kw.setdefault("title", "Backend Engineer")
    return make_job(sid, description=description, **kw)


def test_unknown_is_held_once_and_never_scored_or_alerted(engine):
    ingest(engine, [posting("u"), posting("y", YES, title="Platform Engineer"),
                    posting("n", NO, title="Infra Engineer")])
    result = filter_now(engine)
    assert result.passed == 1 and result.rejected[UNKNOWN_REASON] == 1
    assert row(engine, "u").status == "FILTERED" and row(engine, "u").filter_reason == UNKNOWN_REASON
    assert row(engine, "n").filter_reason.startswith("visa:") and row(engine, "n").filter_reason != UNKNOWN_REASON
    [entry] = held(engine)
    assert (entry.title, entry.state, entry.job_id) == ("Backend Engineer", "pending", row(engine, "u").id)
    filter_now(engine)                                   # idempotent
    assert len(held(engine)) == 1


def test_repost_updates_the_same_entry_and_dismissal_sticks(engine):
    ingest(engine, [posting("old")])
    filter_now(engine)
    [first] = held(engine)
    with engine.begin() as conn:
        review.decide(conn, first.id, "dismissed")
    # Same company/title/location reposted under a new ID (and on another board).
    ingest(engine, [posting("new", posted_at=NOW - timedelta(minutes=5))], board="acme-careers")
    filter_now(engine)
    [entry] = held(engine)
    assert entry.id == first.id and entry.state == "dismissed"
    with engine.connect() as conn:
        assert review.pending(conn) == []


def test_a_new_role_from_the_same_company_is_a_new_entry(engine):
    ingest(engine, [posting("a"), posting("b", title="Data Engineer")])
    filter_now(engine)
    assert len(held(engine)) == 2


def test_keep_releases_the_job_into_the_ai_pipeline_and_survives_rechecks(engine):
    ingest(engine, [posting("k")])
    filter_now(engine)
    [entry] = held(engine)
    with engine.begin() as conn:
        assert review.decide(conn, entry.id, "kept") == row(engine, "k").id
    assert row(engine, "k").status == "DISCOVERED"
    with engine.begin() as conn:                        # a policy change rechecks everything
        result = run_filters(conn, POLICY, needs_sponsorship=True, recheck=True)
    assert result.passed == 1 and row(engine, "k").status == "DISCOVERED"
    # Delivery honours the decision even though the posting states nothing.
    fake = type("J", (), dict(closed_at=None, status="MATCHED", choice=None, posted_at=NOW, description=UNKNOWN))
    assert _eligible(fake, 3, True, kept=True) and not _eligible(fake, 3, True, kept=False)
    refused = type("J", (), dict(closed_at=None, status="MATCHED", choice=None, posted_at=NOW, description=NO))
    assert not _eligible(refused, 3, True, kept=True)   # an explicit refusal still blocks


def test_a_later_explicit_statement_resolves_the_entry(engine):
    ingest(engine, [posting("r")])
    filter_now(engine)
    ingest(engine, [posting("r", YES)])                  # the employer edits the posting
    filter_now(engine)
    [entry] = held(engine)
    assert entry.state == "resolved" and entry.resolution == "passed"
    assert row(engine, "r").status == "DISCOVERED"
    ingest(engine, [posting("r")])                       # ...and removes it again
    filter_now(engine)
    assert held(engine)[0].state == "pending"


def test_closed_or_expired_jobs_leave_the_pending_list(engine):
    ingest(engine, [posting("c"), posting("e", title="Platform Engineer")])
    filter_now(engine)
    with engine.begin() as conn:
        conn.execute(update(jobs_t).where(jobs_t.c.source_job_id == "c").values(closed_at=func.now()))
        conn.execute(update(jobs_t).where(jobs_t.c.source_job_id == "e")
                     .values(filter_reason="posting date: older than 3 days"))
        assert review.pending(conn) == []


def test_decide_rejects_unknown_ids_and_states(engine):
    with engine.begin() as conn:
        with pytest.raises(LookupError):
            review.decide(conn, 999, "kept")
        with pytest.raises(ValueError):
            review.decide(conn, 1, "maybe")


# --- end to end through the workers -----------------------------------------------------------

def test_worker_pipeline_makes_no_ai_call_and_sends_no_alert_for_unknown(engine, boards, llm, telegram):
    server, tg = telegram
    settings = SETTINGS.model_copy(update={
        "filters": POLICY, "preferences": Preferences(interests=["backend"], needs_visa_sponsorship=True)})
    add_board(engine, "acme")
    add_profile(engine)
    boards.snapshots["acme"] = [posting("u"), posting("y", YES, title="Platform Engineer")]
    fake = llm(assessment(role_match=10, technical_match=10, experience_match=10))
    w = make_worker(engine, boards, telegram=tg, settings=settings)
    poll(w, engine)
    w.filter_once()
    assert [t.job_id for t in tasks(engine, "match")] == [row(engine, "y").id]
    w.match_once()
    w.notify_once()
    assert len(fake.calls) == 1                          # only the explicit-support job was scored
    assert len(server.sent) == 1 and "Platform Engineer" in server.sent[0]["text"]
    assert [e.title for e in held(engine)] == ["Backend Engineer"]


def test_cli_lists_keeps_and_dismisses(engine, db_env, capsys):
    from jobradar.cli import main
    ingest(engine, [posting("a"), posting("b", title="Data Engineer")])
    filter_now(engine)
    by_title = {e.title: e for e in held(engine)}
    a, b = by_title["Backend Engineer"], by_title["Data Engineer"]
    assert main(["sponsorship-review"]) == 0
    out = capsys.readouterr().out
    assert "2 pending" in out and "Backend Engineer" in out and "Data Engineer" in out
    assert main(["sponsorship-review", "--keep", str(a.id), "--dismiss", str(b.id)]) == 0
    out = capsys.readouterr().out
    assert "queued for matching" in out and "0 pending, 1 kept, 1 dismissed" in out
    assert [t.job_id for t in tasks(engine, "match")] == [row(engine, "a").id]
    assert main(["sponsorship-review", "--keep", "999"]) == 1


# --- migration 0014 data step -----------------------------------------------------------------

def test_migration_requeues_recent_no_explicit_rejections_only(scratch, monkeypatch):
    # Rows are ingested into an older schema with today's code: leave out the
    # newer discovered_in_backfill column (written only for initial scans).
    from tests.integration.helpers import ingest_with_an_older_schema
    ingest_with_an_older_schema(monkeypatch)
    from alembic import command
    from alembic.config import Config
    from jobradar.db import migrate
    old = "visa: no explicit sponsorship or STEM OPT support in this posting"
    config = Config()
    config.set_main_option("script_location", "jobradar:migrations")
    with scratch.connect() as conn:          # alembic owns commits (CONCURRENTLY indexes)
        config.attributes["connection"] = conn
        command.upgrade(config, "0012")
    ingest(scratch, [posting("recent"), posting("stale", posted_at=NOW - timedelta(days=5), title="X Engineer"),
                     posting("other", title="Senior Engineer")])
    with scratch.begin() as conn:
        conn.execute(text("UPDATE jobs SET status = 'FILTERED', filtered_at = now(), filter_reason = :r"), {"r": old})
        conn.execute(text("UPDATE jobs SET filter_reason = 'title: senior' WHERE source_job_id = 'other'"))
    migrate(scratch)
    with scratch.connect() as conn:
        pending = dict(conn.execute(text("SELECT source_job_id, filtered_at IS NULL FROM jobs")).all())
    assert pending == {"recent": True, "stale": False, "other": False}


def test_two_held_jobs_with_one_fingerprint_in_one_batch(engine):
    """A repost and its (reopened) original can be held together; the upsert
    must not propose the same queue key twice in one statement."""
    ingest(engine, [posting("orig")])
    ingest(engine, [posting("repost")], board="acme-careers")
    with engine.begin() as conn:      # make both canonical, as after a reopen
        conn.execute(update(jobs_t).values(status="DISCOVERED", duplicate_of_id=None, dedup_reason=None))
    result = filter_now(engine)
    assert result.rejected[UNKNOWN_REASON] == 2
    [entry] = held(engine)
    assert entry.job_id == row(engine, "repost").id


# --- require_explicit_sponsorship = false: silent postings are scored, not held ----------------

SCORE_SILENT = FilterSettings(max_posting_age_days=3, require_explicit_sponsorship=False)


def test_turning_the_requirement_off_releases_held_jobs_without_a_full_recheck(engine):
    from jobradar.db import app_state
    from jobradar.filters import _signature
    ingest(engine, [posting("held"), posting("stale", posted_at=NOW - timedelta(days=5), title="Data Engineer"),
                    posting("gone", title="Platform Engineer"), posting("senior", title="Senior Engineer")])
    filter_now(engine)                                   # held under the explicit requirement
    with engine.begin() as conn:
        conn.execute(update(jobs_t).where(jobs_t.c.source_job_id == "stale").values(
            status="FILTERED", filter_reason=UNKNOWN_REASON, filtered_at=func.now()))
        review.hold(conn, [row(engine, "stale").id])
        conn.execute(update(jobs_t).where(jobs_t.c.source_job_id == "gone").values(closed_at=func.now()))
        # Production stores a version-5 signature that includes the old setting.
        old = _signature(POLICY.model_dump(exclude={"max_discovery_age_days"}), True, version=5)
        conn.execute(update(app_state).where(app_state.c.key == "filter_context").values(value=old))
        # A row a full recheck would relabel: content-rejected, and old.
        conn.execute(update(jobs_t).where(jobs_t.c.source_job_id == "senior").values(
            posted_at=NOW - timedelta(days=9)))
    assert {e.state for e in held(engine)} == {"pending"}

    with engine.begin() as conn:
        result = run_filters(conn, SCORE_SILENT, needs_sponsorship=True)
    assert result.passed_ids == [row(engine, "held").id]
    assert row(engine, "held").status == "DISCOVERED" and row(engine, "held").filter_reason is None
    assert row(engine, "stale").filter_reason == "posting date: older than 3 days"
    assert row(engine, "gone").filter_reason == UNKNOWN_REASON            # closed jobs are left alone
    assert row(engine, "senior").filter_reason == "title: senior"         # no full recheck happened
    assert {(e.state, e.resolution) for e in held(engine)} == {
        ("resolved", "released: explicit sponsorship no longer required")}
    with engine.connect() as conn:
        stored = conn.execute(select(app_state.c.value).where(app_state.c.key == "filter_context")).scalar_one()
    assert stored == _signature(SCORE_SILENT.model_dump(exclude={"max_discovery_age_days",
                                                                 "require_explicit_sponsorship"}), True, version=6)
    with engine.begin() as conn:                         # nothing left to release: a no-op
        assert review.release_held(conn) == []


def test_new_silent_postings_pass_and_refusals_are_still_rejected(engine):
    ingest(engine, [posting("u"), posting("n", NO, title="Infra Engineer")])
    with engine.begin() as conn:
        result = run_filters(conn, SCORE_SILENT, needs_sponsorship=True)
    assert result.passed_ids == [row(engine, "u").id]
    assert row(engine, "n").filter_reason.startswith("visa:")
    assert held(engine) == []
    fake = type("J", (), dict(closed_at=None, status="MATCHED", choice=None, posted_at=NOW, description=UNKNOWN))
    assert _eligible(fake, 3, False)                     # deliverable without explicit support


def test_worker_releases_held_jobs_scores_them_and_tags_the_alert(engine, boards, llm, telegram):
    server, tg = telegram
    prefs = Preferences(interests=["backend"], needs_visa_sponsorship=True)
    strict = SETTINGS.model_copy(update={"filters": POLICY, "preferences": prefs})
    add_board(engine, "acme")
    add_profile(engine)
    boards.snapshots["acme"] = [posting("u")]
    fake = llm(assessment(role_match=10, technical_match=10, experience_match=10))
    w = make_worker(engine, boards, telegram=tg, settings=strict)
    poll(w, engine)
    w.filter_once()
    assert [e.title for e in held(engine)] == ["Backend Engineer"] and tasks(engine, "match") == []

    w.settings = SETTINGS.model_copy(update={"filters": SCORE_SILENT, "preferences": prefs})
    boards.snapshots["acme"] = [posting("u"), posting("v", title="Platform Engineer")]
    with engine.begin() as conn:
        conn.execute(text("UPDATE board_state SET next_due_at = now(), snapshot_hash = NULL"))
    poll(w, engine)
    assert [t.job_id for t in tasks(engine, "filter")] == [row(engine, "v").id]   # unchanged "u" isn't requeued
    w.filter_once()                                      # the new job's batch also releases the held one
    assert sorted(t.job_id for t in tasks(engine, "match")) == sorted([row(engine, "u").id, row(engine, "v").id])
    w.match_once()
    w.notify_once()
    assert len(fake.calls) == 2 and len(server.sent) == 2
    assert all("🛂 Sponsorship: not mentioned in the posting" in m["text"] for m in server.sent)

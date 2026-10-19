from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from jobradar.db import jobs, notifications
from jobradar.filters import run_filters
from jobradar.matcher import run_matching
from jobradar.notifications import enqueue, process_updates, send_digest, send_immediate
from jobradar.queue_ui import request_queue
from jobradar.recency import recent_jobs
from jobradar.review import set_choice
from jobradar.settings import FilterSettings, NotificationSettings, Preferences
from tests.factories import assessment, make_job
from tests.integration.helpers import ingest, row
from tests.integration.test_matcher import add_profile, llm
from tests.integration.test_notifications import CHAT, matched, server, tg


def seed_dates(engine):
    matched(engine, {'recent': 90, 'old': 90, 'unknown': 90, 'future': 90})
    now = datetime.now(UTC)
    dates = {'recent': now - timedelta(days=2), 'old': now - timedelta(days=15),
             'unknown': None, 'future': now + timedelta(days=1)}
    with engine.begin() as conn:
        for sid, date in dates.items():
            conn.execute(update(jobs).where(jobs.c.source_job_id == sid).values(posted_at=date))


def test_query_boundary_is_inclusive_and_unknown_is_ineligible(engine):
    seed_dates(engine)
    now = datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.source_job_id == 'recent').values(posted_at=now-timedelta(days=14)))
        assert conn.execute(select(jobs.c.source_job_id).where(recent_jobs(14, now))).scalars().all() == ['recent']
        assert set(conn.execute(select(jobs.c.source_job_id).where(~recent_jobs(14, now))).scalars()) == {
            'old', 'unknown', 'future'}


def test_each_scan_expires_notified_jobs_and_keeps_application_history(engine):
    seed_dates(engine)
    with engine.begin() as conn:
        conn.execute(update(jobs).values(status='NOTIFIED'))
        conn.execute(update(jobs).where(jobs.c.source_job_id == 'future').values(status='APPLIED'))
        run_filters(conn, FilterSettings(max_posting_age_days=14))
    assert row(engine, 'old').status == 'FILTERED'
    assert row(engine, 'unknown').filter_reason == 'posting date: unavailable'
    assert row(engine, 'recent').status == 'NOTIFIED'
    assert row(engine, 'future').status == 'APPLIED'
    with engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.source_job_id == 'recent')
                     .values(posted_at=datetime.now(UTC)-timedelta(days=15)))
        run_filters(conn, FilterSettings(max_posting_age_days=14))
    assert row(engine, 'recent').status == 'FILTERED'


def test_matching_does_not_spend_ai_calls_on_old_or_unknown_jobs(engine, llm):
    seed_dates(engine)
    with engine.begin() as conn:
        conn.execute(update(jobs).values(status='DISCOVERED', filtered_at=datetime.now(UTC)))
    fake = llm(assessment())
    result = run_matching(engine, add_profile(engine), Preferences(), max_posting_age_days=14)
    assert result.assessed == 1 and len(fake.calls) == 1
    assert row(engine, 'recent').status == 'MATCHED'
    assert row(engine, 'old').status == 'DISCOVERED'


def test_queue_removes_expired_jobs_in_place_and_cancels_pending_delivery(engine, server, tg):
    seed_dates(engine)
    with engine.begin() as conn:
        enqueue(conn, NotificationSettings())  # pending from before the cutoff was configured
        request_queue(conn, CHAT, publish=True)
    process_updates(engine, tg, CHAT)
    assert len(server.sent) == 1
    with engine.begin() as conn:
        conn.execute(update(notifications).values(status='pending'))
        set_choice(conn, row(engine, 'old').id, 'QUEUED')
        request_queue(conn, CHAT, view='new', max_posting_age_days=14)
    process_updates(engine, tg, CHAT)
    text = server.edited[-1]['text']
    assert 'Engineer recent' in text
    assert all(f'Engineer {sid}' not in text for sid in ('old', 'unknown', 'future'))
    with engine.begin() as conn:
        assert len(conn.execute(select(notifications.c.id).where(notifications.c.status == 'cancelled')).all()) == 3
        request_queue(conn, CHAT, view='liked')
    process_updates(engine, tg, CHAT)
    assert 'Liked · 0 jobs' in server.edited[-1]['text']
    # An old inline job selector cannot put an expired job back on screen.
    server.press(f"q:open:{row(engine, 'old').id}:new:0", chat_id=CHAT, message_id=101)
    process_updates(engine, tg, CHAT)
    assert 'Engineer old' not in server.edited[-1]['text']
    assert len(server.sent) == 1
    assert all(edit['message_id'] == 101 for edit in server.edited)


def test_enqueue_and_both_alert_modes_enforce_cutoff(engine, server, tg):
    seed_dates(engine)
    with engine.begin() as conn:
        assert enqueue(conn, NotificationSettings(), max_posting_age_days=14) == {'immediate': 1, 'digest': 0}
        enqueue(conn, NotificationSettings())  # simulate a pre-existing outbox
    result = send_immediate(engine, tg, CHAT, max_posting_age_days=14)
    assert (result.sent, result.cancelled) == (1, 3)
    with engine.begin() as conn:
        conn.execute(update(notifications).values(status='pending', kind='digest'))
        conn.execute(update(jobs).values(status='MATCHED'))
    result = send_digest(engine, tg, CHAT, max_posting_age_days=14)
    assert (result.sent, result.cancelled) == (1, 3)


def test_date_only_collection_change_can_recover_unknown_date_job(engine):
    job = make_job('date').model_copy(update={'posted_at': None})
    ingest(engine, [job])
    with engine.begin() as conn:
        run_filters(conn, FilterSettings(max_posting_age_days=14))
    assert row(engine, 'date').status == 'FILTERED'
    publication = datetime.now(UTC)-timedelta(days=1)
    ingest(engine, [job.model_copy(update={'posted_at': publication})])
    with engine.begin() as conn:
        run_filters(conn, FilterSettings(max_posting_age_days=14))
    assert row(engine, 'date').status == 'DISCOVERED'
    assert row(engine, 'date').posted_at == publication

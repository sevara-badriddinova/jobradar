from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from jobradar import cli, matcher
from jobradar.collectors import COLLECTORS
from jobradar.db import jobs, notifications
from jobradar.pipeline import run_collector
from jobradar.settings import FilterSettings, Preferences, Settings
from tests.ats_fixtures import DATE, WORKDAY_BOARD, public_handler
from tests.factories import FakeLLM, assessment, RecordingTransport
from tests.fake_telegram import FakeTelegram
from tests.integration.test_matcher import add_profile


@pytest.mark.parametrize('source', ['workday', 'workable', 'recruitee', 'rippling', 'personio'])
def test_new_source_reaches_telegram_once_under_strict_policy(engine, db_env, monkeypatch, source):
    published = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    def handler(r):
        response = public_handler(source)(r)
        if source == 'workday' and r.method == 'POST':
            # A malformed search entry must not prevent valid sponsored jobs
            # from completing the delivery pipeline.
            payload = response.json()
            payload['total'] += 1
            payload['jobPostings'].append({'bulletFields': ['missing-url']})
            response = httpx.Response(200, json=payload)
        if source == 'personio' and r.url.path != '/xml' and r.url.params.get('language') == 'en':
            response = httpx.Response(200, text='<html>No English publication metadata</html>')
        return httpx.Response(200, text=response.text.replace(DATE, published).replace(
            '2026-10-12 12:00:00 UTC', published))
    collector = COLLECTORS[source](WORKDAY_BOARD if source == 'workday' else 'acme',
        company='Acme', client=RecordingTransport(handler).client())
    assert run_collector(engine, collector).ok
    policy = Settings(filters=FilterSettings(max_posting_age_days=3, require_explicit_sponsorship=True),
                      preferences=Preferences(needs_visa_sponsorship=True))
    monkeypatch.setattr(cli, 'load_settings', lambda path: policy)
    monkeypatch.setattr(matcher.llm, 'ask', FakeLLM(assessment(role_match=10, technical_match=10)))
    add_profile(engine)
    telegram = FakeTelegram()
    monkeypatch.setattr(cli, 'make_client', telegram.client)
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', telegram.token)
    monkeypatch.setenv('TELEGRAM_CHAT_ID', '42')
    assert cli.main(['filter']) == 0
    assert cli.main(['match']) == 0
    assert cli.main(['notify', '--digest']) == 0
    assert cli.main(['notify', '--digest']) == 0
    with engine.connect() as conn:
        rows = conn.execute(select(jobs)).all()
        assert len(rows) == 2 and all(r.status == 'NOTIFIED' for r in rows)
        sent = conn.execute(select(notifications)).all()
        assert len(sent) == 2 and all(n.status == 'sent' and n.attempts == 1 for n in sent)


def test_partial_detail_failure_does_not_close_existing_job(engine):
    source = 'workday'
    collector = COLLECTORS[source](WORKDAY_BOARD, client=RecordingTransport(public_handler(source)).client())
    assert run_collector(engine, collector).result.new == 2
    def missing(request):
        if request.url.path.endswith('R2'):
            return httpx.Response(404)
        return public_handler(source)(request)
    collector = COLLECTORS[source](WORKDAY_BOARD, client=RecordingTransport(missing).client())
    report = run_collector(engine, collector)
    assert report.ok and report.fetched == 1 and report.result.closed == 0
    with engine.connect() as conn:
        assert all(r.closed_at is None for r in conn.execute(select(jobs)))


def test_malformed_workday_list_preserves_previously_known_job(engine):
    from tests.ats_fixtures import workday_item
    collector = COLLECTORS['workday'](WORKDAY_BOARD,
        client=RecordingTransport(public_handler('workday')).client())
    assert run_collector(engine, collector).result.new == 2
    def broken_listing(request):
        if request.method == 'POST':
            return httpx.Response(200, json={'total': 2, 'jobPostings': [
                workday_item('1'), {'bulletFields': ['R2']}]})
        return public_handler('workday')(request)
    collector = COLLECTORS['workday'](WORKDAY_BOARD, client=RecordingTransport(broken_listing).client())
    report = run_collector(engine, collector)
    assert report.ok and report.fetched == 1 and report.result.closed == 0
    with engine.connect() as conn:
        assert all(r.closed_at is None for r in conn.execute(select(jobs)))

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, func, select

from jobradar import cli
from jobradar.applications import link_applications, record
from jobradar.db import app_state, applications, feedback, jobs, processed_emails
from jobradar.inbox import Email, EmailVerdict, sync_inbox
from jobradar.notion import NotionError, sync_notion
from jobradar.settings import Settings
from tests.factories import make_job
from tests.integration.test_applications import add, seed
from tests.integration.helpers import ingest, row
from tests.unit.test_notion import SOURCE, SETTINGS, page

MESSAGE = Email("message-1", "jobs@example.com", "Thanks for applying", None, "Software Engineer")
VERDICT = EmailVerdict(kind="application_received", company="Acme", role="Software Engineer")


class Tracker:
    def __init__(self, pages):
        self.rows = pages
        self.since = []
        self.fail = False

    def resolve_source(self, **kwargs):
        return SOURCE

    def validate_schema(self, *args):
        pass

    def pages(self, source_id, since):
        self.since.append(since)
        yield from self.rows
        if self.fail:
            raise NotionError("second page failed")


def count(engine, table):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def test_500_notion_rows_repeat_without_writes_or_ai(engine):
    tracker = Tracker([page(i) for i in range(500)])
    first = sync_notion(engine, tracker, SETTINGS)
    second = sync_notion(engine, tracker, SETTINGS)
    assert first.read == first.changed == 500
    assert second.read == 500 and second.changed == 0
    assert count(engine, applications) == 500 and count(engine, app_state) == 1
    assert tracker.since == [None, datetime(2026, 10, 1, 9, 59, tzinfo=UTC)]


def test_notion_edit_updates_existing_row_and_mapping_change_backfills(engine):
    tracker = Tracker([page()])
    sync_notion(engine, tracker, SETTINGS)
    tracker.rows = [page(status="Rejected", edited="2026-10-02T10:00:00Z")]
    assert sync_notion(engine, tracker, SETTINGS).changed == 1
    assert count(engine, applications) == 1
    with engine.connect() as conn:
        assert conn.execute(select(applications.c.kind)).scalar_one() == "REJECTED"
    assert sync_notion(engine, tracker, SETTINGS, full=True).changed == 0
    assert tracker.since[-1] is None
    changed_settings = SETTINGS.model_copy(update={"date_property": None})
    sync_notion(engine, tracker, changed_settings)
    assert tracker.since[-1] is None


def test_overlapping_notion_imports_converge(engine):
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(sync_notion, engine, Tracker([page()]), SETTINGS) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert sum(result.changed for result in results) == 1
    assert count(engine, applications) == count(engine, app_state) == 1


def test_notion_failure_rolls_back_rows_and_checkpoint(engine):
    tracker = Tracker([page()])
    tracker.fail = True
    with pytest.raises(NotionError):
        sync_notion(engine, tracker, SETTINGS)
    assert count(engine, applications) == count(engine, app_state) == 0
    tracker.fail = False
    assert sync_notion(engine, tracker, SETTINGS).changed == 1
    with engine.connect() as conn:
        checkpoint = conn.execute(select(app_state.c.value)).scalar_one()
    tracker.rows = [page(2, edited="2026-10-03T00:00:00Z"), page(3, status="Unknown")]
    with pytest.raises(NotionError):
        sync_notion(engine, tracker, SETTINGS)
    assert count(engine, applications) == 1
    with engine.connect() as conn:
        assert conn.execute(select(app_state.c.value)).scalar_one() == checkpoint


def test_notion_saved_and_archived_rows_do_not_suppress_jobs(engine):
    tracker = Tracker([page(status="Saved"), page(2) | {"archived": True}])
    result = sync_notion(engine, tracker, SETTINGS)
    assert result.skipped == 2 and count(engine, applications) == 0


def test_rejection_first_stays_rejected_and_repeat_is_noop(engine):
    seed(engine)
    add(engine, external_id="reject", kind="REJECTED")
    add(engine, external_id="apply", kind="APPLIED")
    with engine.begin() as conn:
        link_applications(conn)
    assert row(engine, "ng").status == "REJECTED"
    with engine.begin() as conn:
        result = link_applications(conn)
    assert result.jobs_marked == result.applications_linked == 0
    assert count(engine, feedback) == 2


def test_record_identical_input_does_not_update_or_duplicate(engine):
    values = dict(source="notion", external_id="1", company="Acme", role="Engineer", kind="APPLIED")
    with engine.begin() as conn:
        assert record(conn, **values)
        assert not record(conn, **values)
    assert count(engine, applications) == 1


def test_gmail_rechecks_duplicate_ids_even_if_fetch_returns_them(engine):
    calls = []
    def decide(message):
        calls.append(message.message_id)
        return VERDICT
    fetch = lambda *args: [MESSAGE, MESSAGE]
    result = sync_inbox(engine, "me", "pw", fetch=fetch, decide=decide)
    again = sync_inbox(engine, "me", "pw", fetch=fetch, decide=decide)
    assert result.read == result.skipped == 1
    assert again.read == 0 and again.skipped == 2
    assert calls == [MESSAGE.message_id]
    assert count(engine, applications) == count(engine, processed_emails) == 1


def test_overlapping_gmail_syncs_make_one_paid_call(engine):
    barrier = threading.Barrier(2)
    calls = []
    def fetch(*args):
        barrier.wait(timeout=5)
        return [MESSAGE]
    def decide(message):
        calls.append(message.message_id)
        return VERDICT
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(sync_inbox, engine, "me", "pw", fetch=fetch, decide=decide) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert sum(r.read for r in results) == 1 and calls == [MESSAGE.message_id]
    assert count(engine, applications) == count(engine, processed_emails) == 1


def test_failure_between_application_and_processed_marker_is_atomic(engine):
    def fail(conn, cursor, statement, *args):
        if statement.startswith("INSERT INTO processed_emails"):
            raise RuntimeError("simulated database failure")
    event.listen(engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="simulated"):
            sync_inbox(engine, "me", "pw", fetch=lambda *args: [MESSAGE], decide=lambda _: VERDICT)
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    assert count(engine, applications) == count(engine, processed_emails) == 0
    assert sync_inbox(engine, "me", "pw", fetch=lambda *args: [MESSAGE], decide=lambda _: VERDICT).read == 1


def test_inbox_budget_and_early_close(engine):
    closed = []
    def fetch(*args):
        try:
            for i in range(201):
                yield Email(str(i), "sender", "subject", None, "body")
        finally:
            closed.append(True)
    result = sync_inbox(engine, "me", "pw", fetch=fetch, decide=lambda _: VERDICT, limit=200)
    assert result.read == 200 and closed == [True]


@pytest.fixture
def config(monkeypatch):
    # Synthetic trackers use default columns, independent of personal profile.toml.
    monkeypatch.setattr(cli, "load_settings", lambda _: Settings())
    for name in ["NOTION_TOKEN", "NOTION_DATA_SOURCE_ID", "NOTION_DATABASE_ID", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NOTION_TOKEN", "test-token")
    monkeypatch.setenv("NOTION_DATA_SOURCE_ID", SOURCE)
    return monkeypatch


def test_cli_auto_notion_never_calls_gmail_and_repeats_safely(engine, db_env, config, capsys):
    config.setenv("GMAIL_ADDRESS", "me@example.com")
    config.setenv("GMAIL_APP_PASSWORD", "test")
    tracker = Tracker([page()])
    config.setattr(cli, "Notion", lambda *args: tracker)
    config.setattr(cli, "sync_inbox", lambda *args, **kwargs: pytest.fail("Gmail must not run in Notion auto mode"))
    assert cli.main(["applications", "sync"]) == 0
    assert cli.main(["applications", "sync"]) == 0
    assert "changed 0" in capsys.readouterr().out and count(engine, applications) == 1


def test_cli_all_runs_notion_before_bounded_gmail(engine, db_env, config):
    ingest(engine, [make_job("one", company="Acme", title="Software Engineer")])
    config.setenv("GMAIL_ADDRESS", "me@example.com")
    config.setenv("GMAIL_APP_PASSWORD", "test")
    config.setattr(cli, "Notion", lambda *args: Tracker([page()]))
    def gmail(eng, address, password, *, days, limit):
        assert count(eng, applications) in {1, 2} and limit == 200
        return sync_inbox(eng, address, password, days=days, limit=limit,
                          fetch=lambda *args: [MESSAGE], decide=lambda _: VERDICT)
    config.setattr(cli, "sync_inbox", gmail)
    assert cli.main(["applications", "sync", "--source", "all"]) == 0
    assert cli.main(["applications", "sync", "--source", "all"]) == 0
    assert count(engine, applications) == 2  # distinct provenance, one suppression decision
    assert row(engine, "one").status == "APPLIED"
    assert count(engine, feedback) == count(engine, processed_emails) == 1


def test_notion_failure_never_silently_falls_back_to_paid_gmail(engine, db_env, config):
    tracker = Tracker([page()])
    tracker.fail = True
    config.setattr(cli, "Notion", lambda *args: tracker)
    config.setattr(cli, "sync_inbox", lambda *args, **kwargs: pytest.fail("unexpected paid fallback"))
    assert cli.main(["applications", "sync"]) == 2
    assert count(engine, applications) == 0


def test_incomplete_notion_config_fails_before_io(config):
    config.delenv("NOTION_TOKEN")
    config.setattr(cli, "make_engine", lambda: pytest.fail("unexpected database access"))
    assert cli.main(["applications", "sync"]) == 2

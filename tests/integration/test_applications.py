from datetime import UTC, datetime

from sqlalchemy import func, select, update

from jobradar import cli
from jobradar.applications import link_applications, record
from jobradar.db import applications, feedback, jobs as jobs_t, processed_emails
from jobradar.inbox import Email, EmailVerdict, sync_inbox
from jobradar.llm import LLMError
from tests.factories import FakeLLM, make_job
from tests.integration.helpers import ingest, row

WHEN = datetime(2026, 8, 14, tzinfo=UTC)


def link(engine):
    with engine.begin() as conn:
        return link_applications(conn)


def add(engine, **kw):
    values = dict(source="gmail", external_id="m1", company="Notion", role="Software Engineer, New Grad",
                  kind="APPLIED", occurred_at=WHEN) | kw
    with engine.begin() as conn:
        record(conn, **values)


def seed(engine):
    ingest(engine, [
        make_job("ng", company="Notion", title="Software Engineer, New Grad (Dec 2026)", location="SF"),
        make_job("ec", company="Notion", title="Software Engineer, Early Career", location="SF"),
        make_job("ecai", company="Notion", title="Software Engineer, Early Career (AI)", location="SF"),
        make_job("ramp", company="Ramp", title="Software Engineer, New Grad", location="NYC"),
    ], board="notion")


class TestLinking:
    def test_company_prefixes_in_both_directions_preserve_word_boundaries(self, engine):
        ingest(engine, [
            make_job("short", company="Acme", title="Software Engineer"),
            make_job("long", company="Acme Research Labs", title="Software Engineer"),
            make_job("boundary", company="Acme Researcher", title="Software Engineer"),
            make_job("unrelated", company="Other", title="Software Engineer"),
        ])
        add(engine, company="Acme Research", role="Software Engineer")
        add(engine, external_id="duplicate", company="Acme Research", role="Software Engineer")
        assert link(engine).jobs_marked == 2
        assert row(engine, "short").status == "APPLIED"
        assert row(engine, "long").status == "APPLIED"
        assert row(engine, "boundary").status == "DISCOVERED"
        assert row(engine, "unrelated").status == "DISCOVERED"
        assert link(engine).jobs_marked == 0

    def test_marks_the_same_role_at_the_same_company_only(self, engine):
        seed(engine)
        add(engine, role="Software Engineer, New Grad (Dec 2026)")
        assert link(engine).jobs_marked == 1
        ng = row(engine, "ng")
        assert (ng.status, ng.applied_at) == ("APPLIED", WHEN)
        assert row(engine, "ecai").status == "DISCOVERED" and row(engine, "ramp").status == "DISCOVERED"
        with engine.connect() as conn:
            app = conn.execute(select(applications)).one()
            assert app.job_id == ng.id
            assert conn.execute(select(feedback.c.kind, feedback.c.source)).all() == [("APPLIED", "gmail")]

    def test_early_career_does_not_cover_early_career_ai(self, engine):
        seed(engine)
        add(engine, role="Software Engineer, Early Career")
        link(engine)
        assert row(engine, "ec").status == "APPLIED" and row(engine, "ecai").status == "DISCOVERED"

    def test_reposts_found_later_are_covered_too(self, engine):
        seed(engine)
        add(engine, role="Software Engineer, New Grad (Dec 2026)")
        link(engine)
        ingest(engine, [make_job("repost", company="Notion Labs", title="Software Engineer - New Grad (Dec 2026)",
                                 url="https://jobs.ashbyhq.com/notion/new-id")], board="notion-ashby")
        assert link(engine).jobs_marked == 1
        assert row(engine, "repost").status == "APPLIED"

    def test_rejection_wins_over_applied_but_not_the_reverse(self, engine):
        seed(engine)
        add(engine, external_id="a", role="Software Engineer, Early Career")
        add(engine, external_id="b", role="Software Engineer, Early Career", kind="REJECTED")
        link(engine)
        assert row(engine, "ec").status == "REJECTED"
        add(engine, external_id="c", role="Software Engineer, Early Career", kind="APPLIED")
        link(engine)
        assert row(engine, "ec").status == "REJECTED"

    def test_matched_and_notified_jobs_become_applied(self, engine):
        seed(engine)
        with engine.begin() as conn:
            conn.execute(update(jobs_t).where(jobs_t.c.source_job_id == "ng").values(status="NOTIFIED"))
        add(engine, role="Software Engineer, New Grad (Dec 2026)")
        link(engine)
        assert row(engine, "ng").status == "APPLIED"

    def test_company_only_application_marks_nothing(self, engine):
        seed(engine)
        add(engine, role=None)
        assert link(engine).jobs_marked == 0

    def test_idempotent(self, engine):
        seed(engine)
        add(engine, role="Software Engineer, New Grad (Dec 2026)")
        link(engine)
        again = link(engine)
        assert (again.jobs_marked, again.applications_linked) == (0, 0)
        with engine.connect() as conn:
            assert conn.execute(select(func.count()).select_from(feedback)).scalar_one() == 1

    def test_record_upserts_by_source_and_id(self, engine):
        add(engine, kind="APPLIED")
        add(engine, kind="REJECTED")
        with engine.connect() as conn:
            assert conn.execute(select(applications.c.kind)).scalars().all() == ["REJECTED"]


class TestInboxSync:
    EMAILS = [
        Email("1", "no-reply@ashbyhq.com", "Thanks for applying to Notion", WHEN, "Software Engineer, New Grad (Dec 2026)"),
        Email("2", "no-reply@ashbyhq.com", "Update from Notion", WHEN, "Unfortunately... Early Career"),
        Email("3", "alerts@jobboard.com", "50 new jobs for you", WHEN, "Notion, Ramp, ..."),
        Email("4", "no-reply@greenhouse-mail.io", "Thanks for applying to Together AI", WHEN, "no role named"),
    ]
    VERDICTS = {
        "1": EmailVerdict(kind="application_received", company="Notion", role="Software Engineer, New Grad (Dec 2026)"),
        "2": EmailVerdict(kind="rejection", company="Notion", role="Software Engineer, Early Career"),
        "3": EmailVerdict(kind="other", company=None, role=None),
        "4": EmailVerdict(kind="application_received", company="Together AI", role=None),
    }

    def fetch(self, address, password, days, seen):
        return [e for e in self.EMAILS if e.message_id not in seen]

    def test_records_applications_and_remembers_every_email(self, engine):
        calls = []

        def decide(message):
            calls.append(message.message_id)
            return self.VERDICTS[message.message_id]

        result = sync_inbox(engine, "me", "pw", fetch=self.fetch, decide=decide)
        assert result.read == 4 and dict(result.found) == {"APPLIED": 2, "REJECTED": 1}
        with engine.connect() as conn:
            assert conn.execute(select(func.count()).select_from(processed_emails)).scalar_one() == 4
            stored = conn.execute(select(applications.c.external_id, applications.c.kind, applications.c.role)
                                  .order_by(applications.c.external_id)).all()
        assert stored == [("1", "APPLIED", "Software Engineer, New Grad (Dec 2026)"),
                          ("2", "REJECTED", "Software Engineer, Early Career"), ("4", "APPLIED", None)]

        again = sync_inbox(engine, "me", "pw", fetch=self.fetch, decide=decide)
        assert again.read == 0 and calls == ["1", "2", "3", "4"]  # nothing classified twice

    def test_failed_classification_is_retried_next_time(self, engine):
        attempts = []

        def flaky(message):
            attempts.append(message.message_id)
            if message.message_id == "2" and attempts.count("2") == 1:
                raise LLMError("overloaded")
            return self.VERDICTS[message.message_id]

        assert sync_inbox(engine, "me", "pw", fetch=self.fetch, decide=flaky).failed == 1
        assert sync_inbox(engine, "me", "pw", fetch=self.fetch, decide=flaky).read == 1
        assert attempts == ["1", "2", "3", "4", "2"]

    def test_stops_after_repeated_failures(self, engine):
        def broken(message):
            raise LLMError("invalid x-api-key")
        result = sync_inbox(engine, "me", "pw", fetch=self.fetch, decide=broken)
        assert result.aborted and result.failed == 3


class TestCli:
    def test_sync_without_gmail_config_still_links(self, engine, db_env, monkeypatch, capsys):
        monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
        seed(engine)
        add(engine, role="Software Engineer, New Grad (Dec 2026)")
        assert cli.main(["applications", "sync"]) == 0
        out = capsys.readouterr().out
        assert "inbox: skipped" in out and "jobs marked as applied/rejected: 1" in out

    def test_sync_with_gmail(self, engine, db_env, monkeypatch, capsys):
        seed(engine)
        monkeypatch.setenv("GMAIL_ADDRESS", "me@gmail.com")
        monkeypatch.setenv("GMAIL_APP_PASSWORD", "pw")
        sync = TestInboxSync()
        monkeypatch.setattr(cli, "sync_inbox", lambda engine, a, p, days, limit: sync_inbox(
            engine, a, p, days=days, fetch=sync.fetch, decide=lambda m: sync.VERDICTS[m.message_id]))
        assert cli.main(["applications", "sync", "--days", "30"]) == 0
        out = capsys.readouterr().out
        assert "inbox: read 4 new email(s): 2 applied, 1 rejected" in out
        assert row(engine, "ng").status == "APPLIED" and row(engine, "ec").status == "REJECTED"

    def test_applied_by_hand_and_lists(self, engine, db_env, capsys):
        seed(engine)
        add(engine, external_id="x", role="Software Engineer, New Grad (AI)")  # renamed later: no exact match
        job_id = row(engine, "ecai").id
        cli.main(["applications", "list", "--unmatched"])
        out = capsys.readouterr().out
        assert "Software Engineer, New Grad (AI)" in out and f"maybe: [{job_id}] Software Engineer, Early Career (AI)" in out
        assert out.count("maybe:") == 3

        assert cli.main(["applied", str(job_id), "999999"]) == 0
        captured = capsys.readouterr()
        assert "Notion | Software Engineer, Early Career (AI)" in captured.out and "no such job" in captured.err
        assert row(engine, "ecai").status == "APPLIED"
        cli.main(["applications", "list"])
        assert "[manual]" in capsys.readouterr().out

    def test_match_and_notify_skip_applied_roles(self, engine, db_env, monkeypatch, capsys):
        from jobradar import matcher
        from tests.factories import assessment
        from tests.integration.test_matcher import add_profile
        seed(engine)
        cli.main(["filter"])
        add(engine, role="Software Engineer, New Grad (Dec 2026)")
        add_profile(engine)
        fake = FakeLLM(assessment())
        monkeypatch.setattr(matcher.llm, "ask", fake)
        cli.main(["match"])
        assert "Software Engineer, New Grad (Dec 2026)" not in " ".join(c["prompt"] for c in fake.calls)
        assert row(engine, "ng").status == "APPLIED"

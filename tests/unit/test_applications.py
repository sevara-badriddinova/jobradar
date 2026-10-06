from datetime import UTC, datetime

import pytest

from jobradar import inbox
from jobradar.applications import same_company, same_role
from jobradar.inbox import Email, EmailVerdict, InboxError, _parse, classify, fetch_emails
from tests.factories import FakeLLM
from tests.fake_imap import FakeIMAP, make_email


class TestSameRole:
    @pytest.mark.parametrize("a,b", [
        ("Software Engineer, New Grad (Dec 2026)", "Software Engineer, New Grad (Dec 2026)"),
        ("Software Engineer, New Grad", "Software Engineer, New Grad (2027)"),
        ("R247320 Associate, Software Engineer, New Grad", "Associate, Software Engineer, New Grad"),
        ("2027 University Graduate - Software Engineer", "University Graduate - Software Engineer (2027)"),
        ("Software Engineer - New Grad (Remote, US)", "software engineer, new grad"),
    ])
    def test_same(self, a, b):
        assert same_role(a, b) and same_role(b, a)

    @pytest.mark.parametrize("a,b", [
        ("Software Engineer, Early Career", "Software Engineer, Early Career (AI)"),
        ("Software Engineer, New Grad (AI)", "Software Engineer, Early Career (AI)"),
        ("Software Engineer I", "Software Engineer II"),
        ("Software Engineer 1", "Software Engineer 2"),
        ("Software Engineer L3", "Software Engineer L4"),
        ("Developer 3D", "Developer"),
        ("Software Engineer", "Software Engineer, Product Infrastructure"),
        ("Backend Engineer", "Frontend Engineer"),
        ("University Grad Software Engineer 2027 (USA)", "University Grad Software Engineer (Toronto)"),
        ("", ""), ("2027", "2027"),
    ])
    def test_different(self, a, b):
        assert not same_role(a, b)


@pytest.mark.parametrize("a,b,same", [
    ("notion", "notion", True), ("notion", "notion labs", True), ("scale ai", "scale", True),
    ("notion", "notional", False), ("", "notion", False), ("ramp", "rampart", False),
])
def test_same_company(a, b, same):
    assert same_company(a, b) is same


class TestParse:
    def test_plain_text(self):
        e = _parse("42", make_email("Thanks for applying to Acme!", "Hi Sevara,\n\nWe received   your application."))
        assert (e.message_id, e.subject, e.sender) == ("42", "Thanks for applying to Acme!", "no-reply@ashbyhq.com")
        assert e.body == "Hi Sevara, We received your application."
        assert e.date == datetime(2026, 10, 2, 13, 5, 11, tzinfo=UTC)

    def test_html_only_email_is_converted(self):
        e = _parse("1", make_email("Update", "<p>Unfortunately we <b>won't</b> move forward.</p>", html=True))
        assert e.body == "Unfortunately we won't move forward."

    def test_bad_date_and_long_body(self):
        e = _parse("1", make_email("x", "word " * 5000, date="not a date"))
        assert e.date is None and len(e.body) == inbox.MAX_BODY_CHARS


class TestFetch:
    def server(self, **kw):
        return FakeIMAP({"111": make_email("Thanks for applying to Acme", "role: Backend Engineer"),
                         "222": make_email("Your application to Globex", "Software Engineer I"),
                         "333": make_email("Thank you for applying to Initech", "New Grad")}, **kw)

    def fetch(self, imap, seen=frozenset(), password="app-password"):
        return list(fetch_emails("me@gmail.com", password, 30, set(seen), imap_factory=imap))

    def test_reads_new_emails_read_only_from_all_mail(self):
        imap = self.server()
        emails = self.fetch(imap)
        assert [e.message_id for e in emails] == ["333", "222", "111"]
        assert imap.host == "imap.gmail.com" and imap.selected == '"[Gmail]/All Mail"' and imap.readonly is True
        assert imap.logged_out
        query = imap.searches[0]
        assert query.startswith('"') and 'newer_than:30d' in query and '\\"thank you for applying\\"' in query

    def test_already_processed_emails_are_never_downloaded(self):
        imap = self.server()
        assert [e.message_id for e in self.fetch(imap, seen={"111", "333"})] == ["222"]
        assert imap.body_fetches == ["2"]

    def test_translated_all_mail_folder(self):
        imap = self.server(all_mail="[Gmail]/Alle Nachrichten")
        assert len(self.fetch(imap)) == 3 and imap.selected == '"[Gmail]/Alle Nachrichten"'

    def test_wrong_password_explains_app_passwords(self):
        imap = self.server()
        with pytest.raises(InboxError, match="app password"):
            self.fetch(imap, password="my-normal-password")
        assert imap.logged_out

    def test_cannot_connect(self):
        def refuse(host):
            raise OSError("network unreachable")
        with pytest.raises(InboxError, match="cannot connect"):
            list(fetch_emails("a", "b", 1, set(), imap_factory=refuse))

    def test_limit_applies_before_body_downloads_and_processed_ids_do_not_use_budget(self):
        imap = self.server()
        emails = list(fetch_emails("me", "app-password", 180, {"333"}, imap_factory=imap, limit=1))
        assert [e.message_id for e in emails] == ["222"]
        assert imap.body_fetches == ["2"] and imap.logged_out

    @pytest.mark.parametrize("failure", ["SEARCH", "(X-GM-MSGID)", "(BODY.PEEK[])"])
    def test_imap_failures_are_not_reported_as_empty_success(self, failure):
        imap = self.server()
        original = imap.uid
        def fail(command, *args):
            return ("NO", [b"failed"]) if command == failure or args[-1] == failure else original(command, *args)
        imap.uid = fail
        with pytest.raises(InboxError, match="incomplete"):
            self.fetch(imap)
        assert imap.logged_out


def test_classify_prompt(monkeypatch):
    fake = FakeLLM(EmailVerdict(kind="rejection", company="Acme", role="Backend Engineer"))
    monkeypatch.setattr(inbox.llm, "ask", fake)
    message = Email("1", "jobs@acme.com", "Update from Acme", None, "Unfortunately...")
    assert classify(message).kind == "rejection"
    (call,) = fake.calls
    assert call["output"] is EmailVerdict and call["model"] == inbox.DEFAULT_MODEL
    assert "Subject: Update from Acme" in call["prompt"] and "<email>\nUnfortunately...\n</email>" in call["prompt"]
    assert "untrusted" in call["system"]

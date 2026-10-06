"""A tiny stand-in for Gmail's IMAP server (only what inbox.fetch_emails uses)."""

import imaplib
from email.message import EmailMessage


def make_email(subject: str, body: str, sender: str = "no-reply@ashbyhq.com",
               date: str = "Fri, 02 Oct 2026 13:05:11 +0000", html: bool = False) -> bytes:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"], msg["Date"] = sender, "me@example.com", subject, date
    if html:
        msg.set_content(body, subtype="html")
    else:
        msg.set_content(body)
    return bytes(msg)


class FakeIMAP:
    def __init__(self, messages: dict[str, bytes], password: str = "app-password",
                 all_mail: str = "[Gmail]/All Mail") -> None:
        # uid -> (X-GM-MSGID, raw); message ids are derived from the uid
        self.messages = {str(i + 1): (msgid, raw) for i, (msgid, raw) in enumerate(messages.items())}
        self.password, self.all_mail = password, all_mail
        self.selected = None
        self.readonly = None
        self.searches: list[str] = []
        self.body_fetches: list[str] = []
        self.logged_out = False

    def __call__(self, host: str) -> "FakeIMAP":  # used as imap_factory
        self.host = host
        return self

    def login(self, user, password):
        if password != self.password:
            raise imaplib.IMAP4.error("b'[AUTHENTICATIONFAILED] Invalid credentials (Failure)'")

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"',
                      f'(\\All \\HasNoChildren) "/" "{self.all_mail}"'.encode()]

    def select(self, mailbox, readonly=False):
        self.selected, self.readonly = mailbox, readonly
        return ("OK", [b"3"]) if mailbox == f'"{self.all_mail}"' else ("NO", [b"no such folder"])

    def uid(self, command, *args):
        if command == "SEARCH":
            self.searches.append(args[1])
            return "OK", [" ".join(self.messages).encode()]
        uids, what = args
        if what == "(X-GM-MSGID)":
            return "OK", [f"{u} (X-GM-MSGID {self.messages[u][0]} UID {u})".encode() for u in uids.split(",")]
        self.body_fetches.append(uids)
        raw = self.messages[uids][1]
        return "OK", [(f"{uids} (UID {uids} BODY[] {{{len(raw)}}}".encode(), raw), b")"]

    def logout(self):
        self.logged_out = True

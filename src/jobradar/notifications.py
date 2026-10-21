"""Telegram notifications, built on an outbox table.

1. enqueue():   one `notifications` row per matched job above the digest
                threshold. UNIQUE(job_id, channel) makes this idempotent.
2. send_*():    claim a row (pending -> sending, committed), send, then mark it
                sent. A crash or timeout mid-send leaves it "sending": its
                delivery is unknown, so it's reported, never silently resent.
                That's at-most-once delivery: no duplicate alerts.
3. process_updates(): button presses ("Applied", "Not interested") become
                feedback. Telegram's update offset is stored in the same
                transaction, so each press is applied exactly once.
"""

import html
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import case, func, literal, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection, Engine, Row

from jobradar.applications import record
from jobradar.db import app_state, feedback, job_matches, job_reviews, jobs, notifications
from jobradar.filters import sponsorship_evidence, sponsorship_reason
from jobradar.sponsorship_review import kept_fingerprints
from jobradar.recency import publication_reason, recent_jobs
from jobradar.review import not_skipped, remaining, set_choice
from jobradar.queue_ui import parse_callback, read_state, request_queue, sync_queue
from jobradar.settings import NotificationSettings
from jobradar.telegram import MAX_MESSAGE_CHARS, Buttons, Telegram, TelegramError

log = logging.getLogger(__name__)

CHANNEL = "telegram"
MAX_ATTEMPTS = 5             # definite failures before a notification is marked failed
MAX_FAILURES_IN_A_ROW = 3    # e.g. wrong chat id: stop instead of failing every row
OFFSET_KEY = "telegram_update_offset"


@dataclass
class SendResult:
    sent: int = 0
    failed: int = 0      # definitely not delivered (will be retried until MAX_ATTEMPTS)
    stuck: int = 0       # delivery unknown; see `notify --resend-stuck`
    cancelled: int = 0   # job closed before it could be announced


# --- 1. queue ------------------------------------------------------------------------

def enqueue(conn: Connection, settings: NotificationSettings, *, max_posting_age_days=None) -> dict[str, int]:
    """Queue every open matched job scoring at least the digest threshold. Idempotent."""
    kind = case((jobs.c.match_score >= settings.immediate_min_score, "immediate"), else_="digest")
    # Reconcile pending rows with current scores; a cancelled, never-attempted
    # alert can become eligible after a corrected assessment. Sent/uncertain
    # deliveries remain protected by their existing outbox identity.
    conn.execute(update(notifications).where(notifications.c.job_id == jobs.c.id,
        notifications.c.status == "pending",
        jobs.c.match_score >= settings.digest_min_score).values(kind=kind))
    conn.execute(update(notifications).where(notifications.c.job_id == jobs.c.id,
        notifications.c.status == "pending",
        jobs.c.match_score < settings.digest_min_score).values(status="cancelled"))
    queued = conn.execute(
        insert(notifications)
        .from_select(["job_id", "channel", "kind"],
                     select(jobs.c.id, literal(CHANNEL), kind)
                     .where(jobs.c.status == "MATCHED", jobs.c.closed_at.is_(None),
                            jobs.c.match_score >= settings.digest_min_score, not_skipped(),
                            recent_jobs(max_posting_age_days)))
        .on_conflict_do_update(constraint="uq_notifications_job_channel",
            set_={"status": "pending", "kind": case(
                (select(jobs.c.match_score).where(jobs.c.id == notifications.c.job_id).scalar_subquery()
                 >= settings.immediate_min_score, "immediate"), else_="digest"), "last_error": None},
            where=(notifications.c.status == "cancelled") & (notifications.c.attempts == 0))
        .returning(notifications.c.kind)
    ).scalars().all()
    return {"immediate": queued.count("immediate"), "digest": queued.count("digest")}


# --- 2. send -------------------------------------------------------------------------

def _job(conn: Connection, job_id: int) -> Row:
    return conn.execute(
        select(jobs.c.id, jobs.c.company, jobs.c.title, jobs.c.location, jobs.c.url,
               jobs.c.first_seen_at, jobs.c.posted_at, jobs.c.closed_at, jobs.c.status, jobs.c.match_score,
               jobs.c.description, jobs.c.fingerprint,
               job_matches.c.assessment, job_reviews.c.choice)
        .outerjoin(job_matches, jobs.c.match_id == job_matches.c.id)
        .outerjoin(job_reviews, jobs.c.id == job_reviews.c.job_id)
        .where(jobs.c.id == job_id)
    ).one()


def _mark_sent(conn: Connection, ids: list[int], job_ids: list[int], message_id: int) -> None:
    conn.execute(update(notifications).where(notifications.c.id.in_(ids))
                 .values(status="sent", sent_at=func.now(), message_id=message_id, last_error=None))
    conn.execute(update(jobs).where(jobs.c.id.in_(job_ids), jobs.c.status == "MATCHED")
                 .values(status="NOTIFIED", notified_at=func.now()))


def _mark_failed(conn: Connection, ids: list[int], error: TelegramError) -> None:
    if error.maybe_delivered:  # leave it "sending": we don't know, so we don't resend
        conn.execute(update(notifications).where(notifications.c.id.in_(ids)).values(last_error=str(error)))
        return
    conn.execute(update(notifications).where(notifications.c.id.in_(ids)).values(
        status=case((notifications.c.attempts >= MAX_ATTEMPTS, "failed"), else_="pending"),
        last_error=str(error)))


def send_immediate(engine: Engine, tg: Telegram, chat_id: str, now: datetime | None = None, *,
                   max_posting_age_days=None, require_explicit_sponsorship=False) -> SendResult:
    result, failures_in_a_row = SendResult(), 0
    with engine.connect() as conn:
        ids = conn.execute(select(notifications.c.id).where(
            notifications.c.channel == CHANNEL, notifications.c.kind == "immediate",
            notifications.c.status == "pending").order_by(notifications.c.id)).scalars().all()

    for notification_id in ids:
        with engine.begin() as conn:  # claim: committed *before* sending
            claimed = conn.execute(
                update(notifications)
                .where(notifications.c.id == notification_id, notifications.c.status == "pending")
                .values(status="sending", attempts=notifications.c.attempts + 1)
                .returning(notifications.c.job_id)
            ).scalar_one_or_none()
            if claimed is None:
                continue  # another process took it
            job = _job(conn, claimed)
            if not _eligible(job, max_posting_age_days, require_explicit_sponsorship, _kept(conn, job)):
                conn.execute(update(notifications).where(notifications.c.id == notification_id)
                             .values(status="cancelled", attempts=notifications.c.attempts - 1))
                result.cancelled += 1
                continue
        try:
            message_id = tg.send_message(chat_id, job_message(job, now), job_buttons(job))
        except TelegramError as exc:
            with engine.begin() as conn:
                _mark_failed(conn, [notification_id], exc)
            log.error("notification %s for job %s: %s", notification_id, job.id, exc)
            if exc.maybe_delivered:
                result.stuck += 1
                continue
            result.failed += 1
            failures_in_a_row += 1
            if failures_in_a_row >= MAX_FAILURES_IN_A_ROW:
                log.error("stopping after %d failed sends in a row", failures_in_a_row)
                break
            continue
        failures_in_a_row = 0
        with engine.begin() as conn:
            _mark_sent(conn, [notification_id], [job.id], message_id)
        result.sent += 1
    return result


def send_digest(engine: Engine, tg: Telegram, chat_id: str, *, max_posting_age_days=None,
                require_explicit_sponsorship=False) -> SendResult:
    """Send every pending digest notification, grouped into as few messages as possible."""
    result = SendResult()
    with engine.begin() as conn:
        claimed = conn.execute(
            update(notifications)
            .where(notifications.c.channel == CHANNEL, notifications.c.kind == "digest",
                   notifications.c.status == "pending")
            .values(status="sending", attempts=notifications.c.attempts + 1)
            .returning(notifications.c.id, notifications.c.job_id)
        ).all()
        items = []
        for row in claimed:
            job = _job(conn, row.job_id)
            if not _eligible(job, max_posting_age_days, require_explicit_sponsorship, _kept(conn, job)):
                conn.execute(update(notifications).where(notifications.c.id == row.id).values(
                    status="cancelled", attempts=notifications.c.attempts - 1))
                result.cancelled += 1
            else:
                items.append((row.id, job))
    items.sort(key=lambda item: (-float(item[1].match_score), item[1].id))

    for text, chunk in digest_messages(items):
        ids, job_ids = [i for i, _ in chunk], [j.id for _, j in chunk]
        try:
            message_id = tg.send_message(chat_id, text, digest_buttons([job for _, job in chunk]))
        except TelegramError as exc:
            with engine.begin() as conn:
                _mark_failed(conn, ids, exc)
            log.error("digest message (%d jobs): %s", len(ids), exc)
            if exc.maybe_delivered:
                result.stuck += len(ids)
            else:
                result.failed += len(ids)
            continue
        with engine.begin() as conn:
            _mark_sent(conn, ids, job_ids, message_id)
        result.sent += len(ids)
    return result


def resend_stuck(conn: Connection) -> int:
    """Put notifications whose delivery is unknown back in the queue (your call to make)."""
    return conn.execute(update(notifications).where(notifications.c.status == "sending")
                        .values(status="pending")).rowcount


# --- 3. buttons ----------------------------------------------------------------------

FEEDBACK_ACTIONS = {"applied": ("APPLIED", "✅ Marked as applied"),
                    "ignored": ("IGNORED", "👎 Not interested"),
                    "sponsorship": ("SPONSORSHIP", "🛂 Skipped: sponsorship required"),
                    "queue": ("QUEUED", "♥ Liked")}
CONSUMER_LOCK = 741863201


def _eligible(job: Row, max_posting_age_days=None, require_explicit_sponsorship=False,
              kept: bool = False) -> bool:
    """kept: the user kept this role from the sponsorship review queue, which
    stands in for explicit support (an explicit refusal still blocks it)."""
    return (job.closed_at is None and job.status == "MATCHED"
            and job.choice not in ("IGNORED", "SPONSORSHIP", "APPLIED")
            and publication_reason(job.posted_at, max_posting_age_days) is None
            and (not require_explicit_sponsorship or (
                (kept or sponsorship_evidence(job.description) is not None)
                and sponsorship_reason(job.description) is None)))


def _kept(conn: Connection, job: Row) -> bool:
    return bool(kept_fingerprints(conn, [getattr(job, "fingerprint", None)]))


def process_updates(engine: Engine, tg: Telegram, chat_id: str, timeout: int = 0) -> int:
    """One update consumer at a time, including getUpdates and cosmetic replies."""
    with engine.connect() as consumer:
        # Keep the transaction open for the whole poll so PgBouncer pins the
        # backend. A transaction lock is released even if processing fails.
        locked = consumer.execute(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": CONSUMER_LOCK}).scalar_one()
        if not locked:
            return 0
        return _process_updates(engine, tg, chat_id, timeout)


def _process_updates(engine: Engine, tg: Telegram, chat_id: str, timeout: int) -> int:
    with engine.connect() as conn:
        offset = int(conn.execute(select(app_state.c.value).where(app_state.c.key == OFFSET_KEY))
                     .scalar_one_or_none() or 0)
    applied = 0
    edits: dict[int, int] = {}
    for upd in tg.get_updates(offset, timeout=timeout):
        started = time.monotonic()
        # Telegram may redeliver an old update. Never repeat its state change or summary.
        if upd["update_id"] < offset:
            continue
        query, message = upd.get("callback_query"), upd.get("message")
        from_us = query is not None and str((query.get("message") or {}).get("chat", {}).get("id")) == str(chat_id)
        own_message = message is not None and str(message.get("chat", {}).get("id")) == str(chat_id)
        command = (message.get("text") or "").split() if own_message else []
        command_view = ({"/new": "new", "/liked": "liked", "/applied": "applied", "/skipped": "skipped",
                         "/menu": "menu", "/start": "menu", "/queue": "new", "/done": "new"}
                        .get(command[0].split("@")[0]) if command else None)
        show_queue = (from_us and query.get("data") == "show_queue") or (
            command_view is not None)
        navigation = parse_callback(query.get("data", "")) if from_us else None
        with engine.begin() as conn:
            outcome = None
            if navigation:
                action, selected_id, view, page = navigation
                if action not in ("open", "list", "menu"):
                    outcome = _apply_feedback(conn, f"{'queue' if action == 'like' else action}:{selected_id}")
                request_queue(conn, chat_id, view=view, page=page, job_id=selected_id, explicit=True)
            elif from_us and not show_queue:
                outcome = _apply_feedback(conn, query.get("data", ""))
            finished = False
            if outcome is not None:
                _, _, job_id, changed = outcome
                # A card belongs to the review list only after its alert was sent.
                was_sent = conn.execute(select(notifications.c.id).where(
                    notifications.c.job_id == job_id, notifications.c.channel == CHANNEL,
                    notifications.c.status == "sent")).first() is not None
                finished = changed and was_sent and remaining(conn) == 0
            if show_queue or (finished and not navigation):
                request_queue(conn, chat_id, view=command_view or "new", page=0, explicit=show_queue)
            elif outcome is not None and not navigation:
                request_queue(conn, chat_id, create=False)
            offset = upd["update_id"] + 1
            conn.execute(insert(app_state).values(key=OFFSET_KEY, value=str(offset))
                         .on_conflict_do_update(index_elements=["key"], set_={"value": str(offset)}))
        if not from_us:
            continue
        if outcome is None:
            label = ("Queue updated" if show_queue or navigation else "Already recorded" if query.get("data") == "noop"
                     else "This button is no longer available")
        else:
            applied += 1
            label, _, job_id, _ = outcome
        try:  # the choice and any summary are already committed
            tg.answer_callback(query["id"], label)
            log.info("button acknowledged in %.2fs: %s", time.monotonic() - started, label)
        except TelegramError as exc:
            log.info("could not answer button press (it may be too old): %s", exc)
        if outcome is not None and not navigation:
            with engine.connect() as conn:
                state = read_state(conn, chat_id)
            if not state or state.get("message_id") != query["message"]["message_id"]:
                edits[query["message"]["message_id"]] = job_id
    # Save and acknowledge every click before cosmetic requests. Coalesce rapid
    # clicks on the same card/digest into one edit reflecting its final state.
    for message_id, job_id in edits.items():
        try:
            with engine.connect() as conn:
                buttons = _message_buttons(conn, message_id, job_id)
            tg.edit_buttons(chat_id, message_id, buttons)
        except TelegramError as exc:
            log.info("could not update the message buttons: %s", exc)
    sync_queue(engine, tg, chat_id)
    return applied


def _apply_feedback(conn: Connection, data: str) -> tuple[str, str, int, bool] | None:
    action, _, raw_id = data.partition(":")
    if action not in FEEDBACK_ACTIONS or not raw_id.isascii() or not raw_id.isdigit() or len(raw_id) > 19:
        return None
    job_id = int(raw_id)
    if job_id > 2**63 - 1:
        return None
    job = conn.execute(select(jobs).where(jobs.c.id == job_id).with_for_update()).first()
    if job is None:
        return None
    kind, label = FEEDBACK_ACTIONS[action]
    if kind == "QUEUED" and (job.closed_at is not None or job.status not in ("MATCHED", "NOTIFIED")):
        return "This role is closed, applied, or no longer eligible", job.url, job_id, False
    changed = set_choice(conn, job_id, kind)
    if kind in ("APPLIED", "IGNORED"):
        conn.execute(insert(feedback).values(job_id=job_id, kind=kind, source=CHANNEL)
                     .on_conflict_do_nothing(constraint="uq_feedback_job_kind"))
    if kind == "APPLIED":
        record(conn, source=CHANNEL, external_id=str(job_id), company=job.company, role=job.title, kind="APPLIED")
        conn.execute(update(jobs).where(jobs.c.id == job_id,
                     jobs.c.status.in_(["DISCOVERED", "FILTERED", "MATCHED", "NOTIFIED"]))
                     .values(status="APPLIED", applied_at=func.coalesce(jobs.c.applied_at, func.now())))
    if kind != "QUEUED":
        conn.execute(update(notifications).where(notifications.c.job_id == job_id,
                     notifications.c.status == "pending").values(status="cancelled"))
    return label, job.url, job_id, changed


def _message_buttons(conn: Connection, message_id: int, fallback_job_id: int | None = None) -> Buttons:
    records = conn.execute(select(notifications.c.job_id, notifications.c.kind).where(
        notifications.c.channel == CHANNEL, notifications.c.message_id == message_id,
        notifications.c.status == "sent").order_by(notifications.c.id)).all()
    if records:
        selected = [_job(conn, row.job_id) for row in records]
        return digest_buttons(selected) if records[0].kind == "digest" else job_buttons(selected[0])
    return job_buttons(_job(conn, fallback_job_id)) if fallback_job_id is not None else []


def refresh_buttons(engine: Engine, tg: Telegram, chat_id: str) -> int:
    """Restore current controls on existing sent cards, without resending alerts."""
    with engine.connect() as conn:
        state = read_state(conn, chat_id)
        message_ids = conn.execute(select(notifications.c.message_id).where(
            notifications.c.channel == CHANNEL, notifications.c.status == "sent",
            notifications.c.message_id.is_not(None)).distinct()).scalars().all()
    refreshed = 0
    for message_id in message_ids:
        if state and message_id == state.get("message_id"):
            continue
        with engine.connect() as conn:
            buttons = _message_buttons(conn, message_id)
        try:
            tg.edit_buttons(chat_id, message_id, buttons)
            refreshed += 1
        except TelegramError as exc:
            if "message is not modified" not in str(exc):
                raise
    return refreshed


# --- formatting ----------------------------------------------------------------------

def _is_web_url(url: str) -> bool:
    return url.startswith(("https://", "http://"))


def _ago(then: datetime, now: datetime) -> str:
    minutes = max(0, int((now - then).total_seconds() // 60))
    if minutes < 60:
        return "just now" if minutes < 1 else f"{minutes} min ago"
    return f"{minutes // 60} h ago" if minutes < 24 * 60 else f"{minutes // (24 * 60)} d ago"


def _clip(text: str, limit: int = 200) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def job_message(job: Row, now: datetime | None = None) -> str:
    e = html.escape
    a = job.assessment or {}
    lines = [
        f"🎯 <b>{float(job.match_score):.0f}% match · {e(_clip(job.title, 150))}</b>",
        f"{e(job.company)} · {e(job.location or 'location not listed')}",
        f"<i>found {_ago(job.first_seen_at, now or datetime.now(UTC))}</i>",
    ]
    if getattr(job, "posted_at", None):
        lines.append(f"Published: {job.posted_at:%Y-%m-%d %H:%M UTC}")
    if evidence := sponsorship_evidence(getattr(job, "description", "")):
        lines.append(f"🛂 Posting says: {e(_clip(evidence, 300))}")
    for heading, items in (("Why", a.get("reasons")), ("Concerns", a.get("concerns"))):
        if items:
            lines += ["", f"<b>{heading}</b>", *(f"• {e(_clip(item))}" for item in items[:4])]
    if a.get("missing_skills"):
        lines += ["", f"<b>Missing:</b> {e(_clip(', '.join(a['missing_skills'][:8])))}"]
    return "\n".join(lines)


def job_buttons(job: Row) -> Buttons:
    rows: Buttons = [[{"text": "Apply ↗", "url": job.url}]] if _is_web_url(job.url) else []
    choice = getattr(job, "choice", None)
    applied = getattr(job, "status", None) in ("APPLIED", "REJECTED") or choice == "APPLIED"
    return rows + [
        [{"text": "✅ Marked as applied" if applied else "✅ Applied", "callback_data": f"applied:{job.id}"},
         {"text": "✓ Not interested" if choice == "IGNORED" else "👎 Not interested", "callback_data": f"ignored:{job.id}"}],
        [{"text": "✓ In queue" if choice == "QUEUED" else "📋 Add to queue", "callback_data": f"queue:{job.id}"},
         {"text": "✓ Sponsorship required" if choice == "SPONSORSHIP" else "🛂 Sponsorship required",
          "callback_data": f"sponsorship:{job.id}"}],
        [{"text": "Show queue", "callback_data": "show_queue"}],
    ]


def digest_buttons(selected: list[Row]) -> Buttons:
    return [[{"text": _clip(f"{job.company} — {job.title}", 65),
              "callback_data": f"q:open:{job.id}:new:0"}] for job in selected] + [
        [{"text": "View all jobs", "callback_data": "show_queue"}]]


def digest_messages(items: list[tuple[int, Row]]) -> list[tuple[str, list[tuple[int, Row]]]]:
    """Split digest entries into messages that fit Telegram's size limit."""
    e = html.escape
    messages, lines, chunk = [], [], []
    header = "📬 <b>Digest: more matches worth a look</b>"
    for item in items:
        job = item[1]
        title = f'<a href="{e(job.url)}">{e(_clip(job.title, 120))}</a>' if _is_web_url(job.url) else e(job.title)
        line = f"<b>{float(job.match_score):.0f}%</b> · {title}\n     {e(job.company)} · {e(job.location or '-')}"
        if getattr(job, "posted_at", None):
            line += f"\n     Published: {job.posted_at:%Y-%m-%d %H:%M UTC}"
        if evidence := sponsorship_evidence(getattr(job, "description", "")):
            line += f"\n     🛂 {e(_clip(evidence, 180))}"
        if chunk and (len(chunk) >= 10 or len("\n\n".join([header, *lines, line])) > MAX_MESSAGE_CHARS - 100):
            messages.append(("\n\n".join([header, *lines]), chunk))
            lines, chunk = [], []
        lines.append(line)
        chunk.append(item)
    if chunk:
        messages.append(("\n\n".join([header, *lines]), chunk))
    return messages

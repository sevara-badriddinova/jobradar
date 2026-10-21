"""One persistent, paginated Telegram message for browsing and reviewing jobs."""

import hashlib
import html
import json
import logging

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert

from jobradar.db import app_state, companies, job_matches, job_reviews, jobs, notifications, review_responses
from jobradar.telegram import TelegramError
from jobradar.job_age import posting_age
from jobradar.query_fields import display_columns
from jobradar.recency import publication_reason, recent_jobs

log = logging.getLogger(__name__)
VIEWS = {"new": "New", "liked": "Liked", "applied": "Applied", "skipped": "Skipped"}
VIEW_ICONS = {"new": "🆕", "liked": "❤️", "applied": "✅", "skipped": "⏭️"}
PAGE_SIZE = 5
_UNCHANGED = object()


def state_key(chat_id):
    return f"telegram_queue:{chat_id}"


def read_state(conn, chat_id, *, lock=False):
    if lock:
        conn.execute(text("SELECT pg_advisory_xact_lock(741863202, hashtext(:key))"),
                     {"key": state_key(chat_id)})
    value = conn.execute(select(app_state.c.value).where(app_state.c.key == state_key(chat_id))).scalar_one_or_none()
    return json.loads(value) if value else None


def write_state(conn, chat_id, state):
    value = json.dumps(state)
    conn.execute(insert(app_state).values(key=state_key(chat_id), value=value)
        .on_conflict_do_update(index_elements=["key"], set_={"value": value}))


def request_queue(conn, chat_id, *, view=None, page=None, job_id=None,
                  explicit=False, create=True, min_score=None, publish=None, max_posting_age_days=_UNCHANGED,
                  discovery_days=_UNCHANGED, relocate=False, from_message_id=None):
    """relocate: answer a typed/menu command with a fresh message at the bottom of
    the chat instead of editing the long-lived queue message in place (which may
    be far up the history, and is not touched at all when nothing changed, so the
    command looked ignored). The previous message is retired by sync_queue."""
    state = read_state(conn, chat_id, lock=True)
    if state is None:
        if not create:
            return
        # Upgrade the latest existing queue summary in place.
        message_id = conn.execute(select(review_responses.c.message_id)
            .where(review_responses.c.status == "sent", review_responses.c.message_id.is_not(None))
            .order_by(review_responses.c.update_id.desc(), review_responses.c.part.desc()).limit(1)).scalar_one_or_none()
        state = dict(message_id=message_id, view="new", page=0, job_id=None,
                     revision=0, sending=False, min_score=70, publish=False)
    if view in (*VIEWS, "menu"):
        state.update(view=view, page=page or 0, job_id=job_id)
    if max_posting_age_days is not _UNCHANGED:
        state["max_posting_age_days"] = max_posting_age_days
    if discovery_days is not _UNCHANGED:
        # The same freshness rule as filtering and alerts (see recency.py).
        state["max_discovery_age_days"] = discovery_days
    if min_score is not None:
        state["min_score"] = min_score
    if publish is not None:
        state["publish"] = publish
    if from_message_id is not None and from_message_id == state.get("message_id"):
        relocate = False      # pressed on the queue message itself: update it in place
    if relocate and state.get("message_id") and not state.get("sending"):
        state["retired_message_id"] = state["message_id"]
        state.update(message_id=None, fingerprint=None)
    if explicit and not state.get("message_id"):
        state["sending"] = False  # explicit recovery from unknown first delivery
    state.update(dirty=True, revision=state["revision"] + 1)
    write_state(conn, chat_id, state)
    # Old summary snapshots are superseded by this single live view.
    conn.execute(update(review_responses).where(review_responses.c.status == "pending").values(status="failed"))


def parse_callback(data):
    if data == "q:menu":
        return "menu", None, "menu", 0
    parts = data.split(":")
    if len(parts) == 4 and parts[:2] == ["q", "list"]:
        _, _, view, page = parts
        action, job_id = "list", None
    elif len(parts) == 5 and parts[0] == "q" and parts[1] in (
            "open", "like", "ignored", "applied", "sponsorship"):
        _, action, raw_id, view, page = parts
        if not raw_id.isascii() or not raw_id.isdigit() or len(raw_id) > 19:
            return None
        job_id = int(raw_id)
        if not 0 < job_id <= 2**63 - 1:
            return None
    else:
        return None
    if view not in VIEWS or not page.isascii() or not page.isdigit() or len(page) > 6:
        return None
    return action, job_id, view, int(page)


def _query(view, min_score, max_posting_age_days=None, discovery_days=None):
    active = and_(jobs.c.closed_at.is_(None), jobs.c.status.in_(["MATCHED", "NOTIFIED"]),
                  recent_jobs(max_posting_age_days, discovery_days=discovery_days))
    predicates = {
        "new": and_(active, jobs.c.match_score >= min_score, job_reviews.c.job_id.is_(None)),
        "liked": and_(active, job_reviews.c.choice == "QUEUED"),
        "applied": jobs.c.status.in_(["APPLIED", "REJECTED"]),
        "skipped": and_(job_reviews.c.choice.in_(["IGNORED", "SPONSORSHIP"]),
                        ~jobs.c.status.in_(["APPLIED", "REJECTED"])),
    }
    return select(*display_columns(), job_reviews.c.choice).outerjoin(job_reviews, jobs.c.id == job_reviews.c.job_id).where(predicates[view])


def _clip(value, length):
    value = " ".join(str(value or "").split())
    return value if len(value) <= length else value[:length - 1] + "…"


def _e(value, length=150):
    value = _clip(value, length)
    while len(html.escape(value).encode("utf-16-le")) // 2 > length * 2:
        value = value[:-2] + "…"
    return html.escape(value)


def render_queue(conn, state):
    view = state["view"]
    max_age = state.get("max_posting_age_days")
    discovery = state.get("max_discovery_age_days")
    counts = {v: conn.execute(select(func.count()).select_from(_query(v, state["min_score"], max_age, discovery).subquery())).scalar_one()
              for v in VIEWS}
    if view == "menu":
        return ("🏠 <b>JobRadar</b>\nChoose a list to browse your jobs.",
                [[{"text": f"{VIEW_ICONS[v]} {VIEWS[v]} · {counts[v]}",
                   "callback_data": f"q:list:{v}:0"} for v in pair]
                 for pair in (("new", "liked"), ("applied", "skipped"))])
    pages = max(1, (counts[view] + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(state["page"], pages - 1)
    home = [{"text": "🏠 Main menu", "callback_data": "q:menu"}]
    if state.get("job_id"):
        job = conn.execute(select(*display_columns(), job_reviews.c.choice, job_matches.c.assessment)
            .outerjoin(job_reviews, job_reviews.c.job_id == jobs.c.id)
            .outerjoin(job_matches, job_matches.c.id == jobs.c.match_id)
            .where(jobs.c.id == state["job_id"])).first()
        if job is not None and (view in ("applied", "skipped") or
                                publication_reason(job.posted_at, max_age, first_seen_at=job.first_seen_at,
                                                   backfill=job.discovered_in_backfill,
                                                   discovery_days=discovery) is None):
            score = f"{float(job.match_score):.0f}% match" if job.match_score is not None else "Not scored"
            status = ("Applied" if job.status == "APPLIED" else "Rejected" if job.status == "REJECTED"
                      else "Closed" if job.closed_at else
                      {"QUEUED": "Liked", "IGNORED": "Not interested", "SPONSORSHIP": "Sponsorship required"}
                      .get(job.choice, "Not reviewed"))
            lines = [f"<b>{_e(job.title)}</b>", _e(job.company, 80), _e(job.location or "Location not listed", 120),
                     f"🕒 {posting_age(job)}", f"\n🎯 {score} · {status}"]
            for heading, key in (("Why it matches", "reasons"), ("Things to consider", "concerns")):
                items = (job.assessment or {}).get(key) or []
                if items:
                    lines += [f"\n<b>{heading}</b>", *(f"• {_e(item, 180)}" for item in items[:3])]
            callback = lambda action: f"q:{action}:{job.id}:{view}:{page}"
            buttons = []
            if job.closed_at is None and job.url.startswith(("https://", "http://")):
                buttons.append([{"text": "🔗 Open application", "url": job.url}])
            if job.status not in ("APPLIED", "REJECTED") and job.closed_at is None:
                buttons += [[{"text": "❤️ Liked" if job.choice == "QUEUED" else "🤍 Like", "callback_data": callback("like")},
                             {"text": "👎 Not interested",
                              "callback_data": callback("ignored")}],
                            [{"text": "✅ Mark applied", "callback_data": callback("applied")},
                             {"text": "🛂 Sponsorship required", "callback_data": callback("sponsorship")}]]
            buttons += [[{"text": "⬅️ Back to jobs", "callback_data": f"q:list:{view}:{page}"}], home]
            return "\n".join(lines), buttons
    rows = conn.execute(_query(view, state["min_score"], max_age, discovery).order_by(jobs.c.match_score.desc().nullslast(), jobs.c.id)
                        .offset(page * PAGE_SIZE).limit(PAGE_SIZE)).all()
    lines = ["📋 <b>Your jobs</b>", f"{VIEW_ICONS[view]} {VIEWS[view]} · {counts[view]} job{'s' if counts[view] != 1 else ''}",
             *(["Tap a job below to review it."] if rows else [])]
    if max_age is not None and view in ("new", "liked"):
        lines.append(f"Posted within the last {max_age} days" + (
            f", or newly listed and posted within {discovery} days." if discovery else "."))
    buttons = []
    for job in rows:
        score = f"{float(job.match_score):.0f}%" if job.match_score is not None else "Unscored"
        heart = "❤️ " if job.choice == "QUEUED" else ""
        lines += [f"\n<b>{_e(job.title, 95)}</b>",
                  f"{_e(job.company, 55)} · {score} · {_e(job.location or 'Location not listed', 65)}",
                  f"🕒 {posting_age(job)}"]
        buttons.append([{"text": _clip(f"{heart}{job.company} — {job.title}", 65),
                         "callback_data": f"q:open:{job.id}:{view}:{page}"}])
    if not rows:
        lines += ["\n" + {"new": "No new jobs meet your filters right now.",
                           "liked": "No liked jobs yet. Open a job under New and tap Like.",
                           "applied": "No applications recorded yet.",
                           "skipped": "No skipped jobs."}[view]]
        if view == "new":
            active = and_(jobs.c.closed_at.is_(None), recent_jobs(max_age, discovery_days=discovery))
            below = conn.execute(select(func.count()).select_from(jobs).where(active,
                jobs.c.status.in_(["MATCHED", "NOTIFIED"]),
                jobs.c.match_score < state["min_score"])).scalar_one()
            awaiting = conn.execute(select(func.count()).select_from(jobs).where(active,
                jobs.c.status == "DISCOVERED", jobs.c.filtered_at.is_not(None))).scalar_one()
            monitored = conn.execute(select(func.count()).select_from(companies)
                .where(companies.c.enabled.is_(True))).scalar_one()
            lines += [f"Match threshold: {state['min_score']:g}%."]
            if below:
                lines += [f"{below} scored job{'s are' if below != 1 else ' is'} below this threshold."]
            if awaiting:
                lines += [f"{awaiting} job{'s' if awaiting != 1 else ''} awaiting scoring."]
            lines += [f"{monitored} employers monitored.",
                      "\nRefresh reloads saved results; it does not start a scan.",
                      "New matches appear here automatically. Reviewed jobs are in the main menu."]
    if pages > 1:
        lines += [f"\nPage {page + 1} of {pages}"]
        nav = []
        if page:
            nav.append({"text": "⬅️ Previous", "callback_data": f"q:list:{view}:{page - 1}"})
        if page + 1 < pages:
            nav.append({"text": "Next ➡️", "callback_data": f"q:list:{view}:{page + 1}"})
        buttons.append(nav)
    return "\n".join(lines), buttons + [home + [{"text": "🔄 Refresh", "callback_data": f"q:list:{view}:{page}"}]]


def sync_queue(engine, tg, chat_id):
    """Caller owns the Telegram consumer lock. Edits are safe to retry after a timeout."""
    with engine.begin() as conn:
        state = read_state(conn, chat_id, lock=True)
        if not state or not state.get("dirty") or (state.get("sending") and not state.get("message_id")):
            return
        body, buttons = render_queue(conn, state)
        fingerprint = hashlib.sha256(json.dumps([body, buttons], sort_keys=True).encode()).hexdigest()
        pending = []
        if state.get("publish"):
            eligible = select(jobs.c.id).outerjoin(job_reviews, job_reviews.c.job_id == jobs.c.id).where(
                jobs.c.status.in_(["MATCHED", "NOTIFIED"]), jobs.c.closed_at.is_(None),
                jobs.c.match_score >= state["min_score"],
                recent_jobs(state.get("max_posting_age_days"),
                            discovery_days=state.get("max_discovery_age_days")),
                or_(job_reviews.c.choice.is_(None), job_reviews.c.choice == "QUEUED"))
            conn.execute(update(notifications).where(notifications.c.channel == "telegram",
                notifications.c.status == "pending", notifications.c.job_id.not_in(eligible)).values(status="cancelled"))
            pending = conn.execute(select(notifications.c.id, jobs.c.id.label("job_id"))
                .join(jobs, jobs.c.id == notifications.c.job_id)
                .outerjoin(job_reviews, job_reviews.c.job_id == jobs.c.id)
                .where(notifications.c.channel == "telegram", notifications.c.status == "pending",
                       jobs.c.status.in_(["MATCHED", "NOTIFIED"]), jobs.c.closed_at.is_(None),
                       jobs.c.match_score >= state["min_score"],
                       recent_jobs(state.get("max_posting_age_days"),
                            discovery_days=state.get("max_discovery_age_days")),
                       or_(job_reviews.c.choice.is_(None), job_reviews.c.choice == "QUEUED"))).all()
        if not state.get("message_id"):
            state["sending"] = True
            write_state(conn, chat_id, state)
    message_id = state.get("message_id")
    try:
        if message_id:
            if fingerprint != state.get("fingerprint"):
                tg.edit_message(chat_id, message_id, body, buttons)
        else:
            message_id = tg.send_message(chat_id, body, buttons, http_timeout=3, max_attempts=1)
    except TelegramError as exc:
        if message_id and "message is not modified" in str(exc):
            pass  # an earlier edit succeeded but its HTTP response was lost
        else:
            with engine.begin() as conn:
                current = read_state(conn, chat_id, lock=True)
                if message_id and "message to edit not found" in str(exc):
                    current.update(message_id=None, sending=False, dirty=True)
                elif not message_id:
                    current["sending"] = exc.maybe_delivered
                write_state(conn, chat_id, current)
            log.warning("queue update failed; saved choices are preserved: %s", exc)
            return
    retired = state.get("retired_message_id")
    with engine.begin() as conn:
        current = read_state(conn, chat_id, lock=True)
        current.update(message_id=message_id, sending=False, fingerprint=fingerprint,
                       dirty=current["revision"] != state["revision"])
        if retired and current.get("retired_message_id") == retired:
            current.pop("retired_message_id")
        write_state(conn, chat_id, current)
        if pending:
            conn.execute(update(notifications).where(notifications.c.id.in_([r.id for r in pending]),
                notifications.c.status == "pending").values(status="sent", message_id=message_id,
                sent_at=func.now(), last_error=None))
            conn.execute(update(jobs).where(jobs.c.id.in_([r.job_id for r in pending]), jobs.c.status == "MATCHED")
                         .values(status="NOTIFIED", notified_at=func.now()))
    if retired and retired != message_id:
        try:  # cosmetic: old buttons would only lead back here
            tg.edit_message(chat_id, retired, "⬇️ Moved to the latest message below.", [])
        except TelegramError as exc:
            log.info("could not retire the previous queue message: %s", exc)

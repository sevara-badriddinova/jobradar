"""The user's shortlist and review choices. No LLM calls or remote lookups."""

import html

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection

from jobradar.db import job_reviews, jobs, notifications, review_responses


def not_skipped():
    return ~select(job_reviews.c.job_id).where(
        job_reviews.c.job_id == jobs.c.id,
        job_reviews.c.choice.in_(["IGNORED", "SPONSORSHIP", "APPLIED"]),
    ).exists()


def set_choice(conn: Connection, job_id: int, choice: str) -> bool:
    """One current choice per job; repeated clicks change nothing."""
    return conn.execute(insert(job_reviews).values(job_id=job_id, choice=choice)
                        .on_conflict_do_update(index_elements=["job_id"],
                            set_={"choice": choice, "updated_at": func.now()},
                            where=job_reviews.c.choice != choice)
                        .returning(job_reviews.c.job_id)).scalar_one_or_none() is not None


def remaining(conn: Connection) -> int:
    """Undecided roles in the current notification list, including sends in flight."""
    return conn.execute(select(func.count()).select_from(notifications)
        .join(jobs, jobs.c.id == notifications.c.job_id)
        .outerjoin(job_reviews, job_reviews.c.job_id == jobs.c.id)
        .where(notifications.c.channel == "telegram",
               or_(notifications.c.status.in_(["sending", "sent"]),
                   and_(notifications.c.status == "pending", notifications.c.kind == "immediate")),
               jobs.c.status.in_(["MATCHED", "NOTIFIED"]), jobs.c.closed_at.is_(None),
               job_reviews.c.job_id.is_(None))).scalar_one()


def queue_messages(conn: Connection) -> list[str]:
    rows = conn.execute(select(jobs.c.id, jobs.c.title, jobs.c.company, jobs.c.url, jobs.c.match_score)
        .join(job_reviews, job_reviews.c.job_id == jobs.c.id)
        .where(job_reviews.c.choice == "QUEUED", jobs.c.closed_at.is_(None),
               jobs.c.status.in_(["MATCHED", "NOTIFIED"]), jobs.c.match_score.is_not(None))
        .order_by(jobs.c.match_score.desc(), jobs.c.id)).all()
    count = remaining(conn)
    header = f"📋 <b>Your queue · {len(rows)} role(s)</b>\n{count} role(s) left to review."
    header += "\nMatch % is the saved fit score."
    if not rows:
        return [header + "\n\nNo open, unapplied roles selected. Use Add to queue on a job card."]
    messages, lines = [], [header]
    for job in rows:
        title = html.escape(job.title[:120])
        # Bound even unusual URL values so a single entry always fits Telegram.
        if job.url.startswith(("https://", "http://")) and len(job.url) <= 500:
            title = f'<a href="{html.escape(job.url, quote=True)}">{title}</a>'
        line = f"<b>{float(job.match_score):.0f}%</b> · {title}\n{html.escape(job.company[:100])} · #{job.id}"
        if len("\n\n".join([*lines, line]).encode("utf-16-le")) // 2 > 3900:
            messages.append("\n\n".join(lines))
            lines = [header]
        lines.append(line)
    messages.append("\n\n".join(lines))
    return messages


def save_summary(conn: Connection, update_id: int) -> None:
    for part, body in enumerate(queue_messages(conn)):
        conn.execute(insert(review_responses).values(update_id=update_id, part=part, body=body)
                     .on_conflict_do_nothing())

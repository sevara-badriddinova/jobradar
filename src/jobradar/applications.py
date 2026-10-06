"""Jobs you've already applied to, from any source, and linking them to jobs.

Sources (Gmail, Notion, manual) only *record* applications. link_applications()
is the one place that decides which jobs they cover: same company and a very
similar title. It marks every such job, including reposts that show up later,
so a role you applied to is never suggested again.
"""

import difflib
import logging
import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection

from jobradar.db import applications, feedback, jobs
from jobradar.dedup import company_key, normalize_key

log = logging.getLogger(__name__)

OPEN_STATUSES = ["DISCOVERED", "FILTERED", "MATCHED", "NOTIFIED"]  # still "not acted on"


def record(conn: Connection, *, source: str, external_id: str, company: str, role: str | None,
           kind: str, occurred_at: datetime | None = None) -> bool:
    """Upsert a source record; return whether it changed. Identical input is a no-op."""
    values = dict(company=company.strip(), company_key=company_key(company), role=(role or "").strip() or None,
                  kind=kind, occurred_at=occurred_at)
    changed_identity = or_(applications.c.company.is_distinct_from(values["company"]),
                           applications.c.role.is_distinct_from(values["role"]))
    updated = values | {"job_id": case((changed_identity, None), else_=applications.c.job_id)}
    return conn.execute(insert(applications).values(source=source, external_id=external_id, **values)
                        .on_conflict_do_update(
                            constraint="uq_applications_source_external", set_=updated,
                            where=or_(*(applications.c[k].is_distinct_from(v) for k, v in values.items())))
                        .returning(applications.c.id)).scalar_one_or_none() is not None


def same_company(a: str, b: str) -> bool:
    """Same company key, or one is the other plus extra words ("notion" / "notion labs")."""
    if not a or not b:
        return False
    shorter, longer = sorted((a, b), key=len)
    return longer == shorter or longer.startswith(shorter + " ")


# Words that differ between listings of the *same* role: dates, seasons, places
# that are just "US", work mode. Anything else (levels like I/II, "AI", a team
# name, a city) makes it a different role.
_NOISE = {
    "jan", "january", "feb", "february", "mar", "march", "apr", "april", "may", "jun", "june", "jul",
    "july", "aug", "august", "sep", "sept", "september", "oct", "october", "nov", "november", "dec",
    "december", "spring", "summer", "fall", "autumn", "winter", "us", "usa", "u", "s", "united",
    "states", "remote", "hybrid", "onsite", "in", "office",
}


def _role_words(title: str) -> frozenset[str]:
    """Ignore years and explicit requisitions, preserving numeric levels and skills."""
    title = re.sub(r"\b(?:req(?:uisition)?\s*(?:id|number|no)?\s*[:#-]?\s*\d+|r\d{4,})\b",
                   " ", title, flags=re.IGNORECASE)
    return frozenset(w for w in normalize_key(title).split()
                     if w not in _NOISE and not re.fullmatch(r"(?:19|20)\d{2}", w))


def same_role(a: str, b: str) -> bool:
    """Same role if the identifying words are identical (order and dates don't matter)."""
    words = _role_words(a)
    return bool(words) and words == _role_words(b)


def title_similarity(a: str, b: str) -> float:
    """0-1, for *suggesting* near-misses to a human. Never used to mark a job."""
    return difflib.SequenceMatcher(None, normalize_key(a), normalize_key(b)).ratio()


@dataclass
class LinkResult:
    jobs_marked: int = 0
    applications_linked: int = 0


def _jobs_by_company(conn: Connection, *where) -> dict[str, list]:
    grouped: dict[str, list] = {}
    for job in conn.execute(select(jobs.c.id, jobs.c.company_key, jobs.c.title, jobs.c.status).where(*where)):
        grouped.setdefault(job.company_key, []).append(job)
    return grouped


def _same_company_jobs(grouped: dict[str, list], key: str) -> list:
    return [job for k, company_jobs in grouped.items() if same_company(key, k) for job in company_jobs]


def link_applications(conn: Connection) -> LinkResult:
    """Mark jobs covered by recorded applications. Idempotent; cheap enough to run often."""
    result = LinkResult()
    # Lock in a stable order; current status predicates below decide transitions,
    # never the stale job snapshot used to find title matches.
    apps = conn.execute(select(applications).where(applications.c.role.is_not(None))
                        .order_by(applications.c.id).with_for_update()).all()
    grouped = _jobs_by_company(conn) if apps else {}
    for app in apps:
        matches = [job for job in _same_company_jobs(grouped, app.company_key) if same_role(app.role, job.title)]
        if not matches:
            continue
        best = max(matches, key=lambda job: job.id)  # newest listing
        if app.job_id != best.id:
            conn.execute(update(applications).where(applications.c.id == app.id).values(job_id=best.id))
            result.applications_linked += 1
        new_status = "REJECTED" if app.kind == "REJECTED" else "APPLIED"
        for job in matches:
            conn.execute(insert(feedback).values(job_id=job.id, kind=app.kind, source=app.source)
                         .on_conflict_do_nothing(constraint="uq_feedback_job_kind"))
            allowed = OPEN_STATUSES + (["APPLIED"] if new_status == "REJECTED" else [])
            result.jobs_marked += conn.execute(
                update(jobs).where(jobs.c.id == job.id, jobs.c.status.in_(allowed)).values(
                    status=new_status,
                    applied_at=func.coalesce(jobs.c.applied_at, app.occurred_at, func.now()))).rowcount
    return result


def mark_applied_by_hand(conn: Connection, job_id: int) -> str | None:
    """`jobradar applied <id>`: record it like any other source. Returns the job title, or None."""
    job = conn.execute(select(jobs.c.company, jobs.c.title).where(jobs.c.id == job_id)).first()
    if job is None:
        return None
    record(conn, source="manual", external_id=str(job_id), company=job.company, role=job.title, kind="APPLIED")
    return f"{job.company} | {job.title}"


def unmatched(conn: Connection, limit: int = 50, suggestions: int = 3) -> list[tuple]:
    """Applications not linked to any job, each with up to `suggestions` closest jobs at that company."""
    rows = conn.execute(select(applications).where(applications.c.job_id.is_(None))
                        .order_by(applications.c.occurred_at.desc().nulls_last()).limit(limit)).all()
    grouped = _jobs_by_company(conn, jobs.c.duplicate_of_id.is_(None))
    out = []
    for app in rows:
        nearby = [(title_similarity(app.role or "", job.title), job)
                  for job in _same_company_jobs(grouped, app.company_key)]
        out.append((app, sorted(nearby, key=lambda m: -m[0])[:suggestions]))
    return out

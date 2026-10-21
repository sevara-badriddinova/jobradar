"""Jobs you've already applied to, from any source, and linking them to jobs.

Sources (Gmail, Notion, manual) only *record* applications. link_applications()
is the one place that decides which jobs they cover: same company and a very
similar title. It marks every such job, including reposts that show up later,
so a role you applied to is never suggested again.
"""

import difflib
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection

from jobradar.db import app_state, applications, feedback, jobs
from jobradar.dedup import company_key, normalize_key

log = logging.getLogger(__name__)

OPEN_STATUSES = ["DISCOVERED", "REPOSTED", "FILTERED", "MATCHED", "NOTIFIED"]  # still "not acted on"


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


LINK_STATE_KEY = "application_links"
FULL_LINK_SECONDS = 24 * 3600  # periodic full pass (catches retitled jobs and stragglers)
LINK_OVERLAP_IDS = 2000        # re-check recent ids: inserts can commit out of id order


def _apps_signature(apps) -> str:
    return hashlib.sha256(json.dumps([[a.id, a.company_key, a.role, a.kind] for a in apps]).encode()).hexdigest()


def link_applications(conn: Connection, *, full: bool = False) -> LinkResult:
    """Mark jobs covered by recorded applications. Idempotent.

    Incremental: while the applications are unchanged, only jobs inserted since
    the previous pass can newly match (a newer listing is always the best
    match), so only those are read. A full pass (every application against all
    of its employers' jobs) runs when the applications change, every
    FULL_LINK_SECONDS, or when full=True. The full pass reads hundreds of MB on
    the VM; running it every two minutes saturated the disk.
    """
    result = LinkResult()
    # Lock in a stable order; current status predicates below decide transitions,
    # never the stale job snapshot used to find title matches.
    apps = conn.execute(select(applications).where(applications.c.role.is_not(None))
                        .order_by(applications.c.id).with_for_update()).all()
    signature = _apps_signature(apps)
    raw_state = conn.execute(select(app_state.c.value).where(app_state.c.key == LINK_STATE_KEY)).scalar_one_or_none()
    try:
        state = json.loads(raw_state) if raw_state else {}
    except ValueError:
        state = {}
    now = time.time()
    top = conn.execute(select(func.max(jobs.c.id))).scalar_one() or 0
    incremental = (not full and state.get("signature") == signature and isinstance(state.get("cursor"), int)
                   and now - float(state.get("full_at", 0)) < FULL_LINK_SECONDS)
    if incremental:
        since = max(0, state["cursor"] - LINK_OVERLAP_IDS)
        app_keys = {a.company_key for a in apps if a.company_key}
        grouped = {k: v for k, v in _jobs_by_company(conn, jobs.c.id > since, jobs.c.id <= top).items()
                   if any(same_company(k, a) for a in app_keys)} if apps else {}
        _link(conn, apps, grouped, result, partial=True)
        _save_link_state(conn, signature=signature, cursor=top, full_at=float(state["full_at"]))
        return result
    # Compare distinct employers once, then fetch only their job titles. Running
    # the prefix comparison for every job/application pair is costly on a small
    # VM. Materialization prevents PostgreSQL from inlining it back into that loop.
    # Distinct job employers come from a recursive "loose index scan" over
    # ix_jobs_company_key: one index probe per employer instead of a sequential
    # scan of every job row (800k rows read ~100k heap pages on the VM).
    first_key = (select(func.min(jobs.c.company_key).label("company_key"))
                 .where(jobs.c.company_key > "").cte("job_company_keys", recursive=True))
    later_jobs = jobs.alias("later_jobs")
    next_key = select(func.min(later_jobs.c.company_key)).where(
        later_jobs.c.company_key > first_key.c.company_key).scalar_subquery()
    job_keys = first_key.union_all(select(next_key).where(first_key.c.company_key.is_not(None)))
    app_keys = select(applications.c.company_key).where(
        applications.c.role.is_not(None), applications.c.company_key != "").distinct().cte("application_company_keys")
    relevant_company = select(app_keys.c.company_key).where(or_(
        job_keys.c.company_key == app_keys.c.company_key,
        func.starts_with(job_keys.c.company_key, app_keys.c.company_key + " "),
        func.starts_with(app_keys.c.company_key, job_keys.c.company_key + " "))).exists()
    relevant_keys = (select(job_keys.c.company_key).where(job_keys.c.company_key.is_not(None), relevant_company)
                     .cte("relevant_company_keys").prefix_with("MATERIALIZED"))
    grouped = _jobs_by_company(conn, jobs.c.company_key.in_(select(relevant_keys.c.company_key)),
                               jobs.c.id <= top) if apps else {}
    _link(conn, apps, grouped, result)
    _save_link_state(conn, signature=signature, cursor=top, full_at=now)
    return result


def _save_link_state(conn: Connection, **state) -> None:
    value = json.dumps(state)
    conn.execute(insert(app_state).values(key=LINK_STATE_KEY, value=value)
                 .on_conflict_do_update(index_elements=[app_state.c.key], set_={"value": value}))


def _link(conn: Connection, apps, grouped: dict[str, list], result: LinkResult, *,
          partial: bool = False) -> None:
    for app in apps:
        matches = [job for job in _same_company_jobs(grouped, app.company_key) if same_role(app.role, job.title)]
        if not matches:
            continue
        best = max(matches, key=lambda job: job.id)  # newest listing
        # A partial (incremental) view can only add newer listings, never demote the link.
        if app.job_id != best.id and not (partial and app.job_id is not None and app.job_id > best.id):
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

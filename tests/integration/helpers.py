from sqlalchemy import func, select

from jobradar.db import jobs as jobs_t
from jobradar.repository import ingest_snapshot


def ingest(engine, jobs, board="acme", source="greenhouse", complete=True):
    with engine.begin() as conn:
        return ingest_snapshot(conn, source=source, board=board, jobs=jobs, complete=complete)


def row(engine, sid, source="greenhouse"):
    with engine.connect() as conn:
        return conn.execute(
            select(jobs_t).where(jobs_t.c.source == source, jobs_t.c.source_job_id == sid)
        ).one()


def count(engine, *where):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(jobs_t).where(*where)).scalar_one()


def ingest_with_an_older_schema(monkeypatch):
    """Let today's ingestion write into a schema from before newer optional
    columns (discovered_in_backfill, raw_digest), as the migration tests need."""
    from sqlalchemy import literal
    from jobradar import repository
    monkeypatch.setattr(repository, "_is_initial_scan", lambda *a: False)
    monkeypatch.setattr(repository, "_pruned", lambda: literal(False))
    monkeypatch.setattr(repository, "_raw_changed", lambda payload, source: repository._stable(
        repository.jobs_t.c.raw, source).is_distinct_from(repository._stable(payload, source)))

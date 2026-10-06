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

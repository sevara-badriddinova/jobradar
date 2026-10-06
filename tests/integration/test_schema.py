"""The database enforces invariants even if application code is wrong."""

import pytest
from sqlalchemy import inspect, insert
from sqlalchemy.exc import IntegrityError

from jobradar.db import jobs as jobs_t


def base_row(**kw):
    row = dict(source="greenhouse", board="acme", source_job_id="1", company="Acme",
               company_key="acme", title="SWE", url="https://x/1", canonical_url="https://x/1",
               description="", description_hash="h", fingerprint="f", raw={})
    row.update(kw)
    return row


def insert_row(engine, **kw):
    with engine.begin() as conn:
        return conn.execute(insert(jobs_t).values(**base_row(**kw)).returning(jobs_t.c.id)).scalar_one()


def test_unique_source_job_id(engine):
    insert_row(engine)
    with pytest.raises(IntegrityError, match="uq_jobs_source_job"):
        insert_row(engine)


def test_status_must_be_known(engine):
    with pytest.raises(IntegrityError, match="ck_jobs_status"):
        insert_row(engine, status="MAYBE")


def test_duplicate_link_and_reason_go_together(engine):
    original = insert_row(engine)
    with pytest.raises(IntegrityError, match="ck_jobs_dedup_link"):
        insert_row(engine, source_job_id="2", duplicate_of_id=original)
    with pytest.raises(IntegrityError, match="ck_jobs_dedup_link"):
        insert_row(engine, source_job_id="3", dedup_reason="fingerprint")


def test_duplicate_must_point_at_existing_job(engine):
    with pytest.raises(IntegrityError):
        insert_row(engine, duplicate_of_id=999_999, dedup_reason="fingerprint")


def test_defaults(engine):
    insert_row(engine)
    with engine.connect() as conn:
        r = conn.execute(jobs_t.select()).one()
    assert r.status == "DISCOVERED" and r.first_seen_at is not None and r.last_seen_at is not None


def test_expected_indexes_exist(engine):
    names = {ix["name"] for ix in inspect(engine).get_indexes("jobs")}
    assert {"ix_jobs_canonical_url", "ix_jobs_fingerprint", "ix_jobs_open_by_board",
            "ix_jobs_status_first_seen"} <= names


def test_open_jobs_index_is_partial(engine):
    with engine.connect() as conn:
        ddl = conn.exec_driver_sql(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_jobs_open_by_board'").scalar_one()
    assert "WHERE (closed_at IS NULL)" in ddl

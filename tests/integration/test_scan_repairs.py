"""Exercise bounded JSON comparisons and incremental queries on a real database."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import event, text

from jobradar.filters import run_filters
from jobradar.repository import COMPARISON_MAX_BYTES, COMPARISON_MAX_ROWS, _comparison_batches
from jobradar.settings import FilterSettings
from tests.factories import make_job
from tests.integration.helpers import ingest, row


def test_large_board_comparison_survives_and_keeps_jsonb_equality(engine):
    # The old statement bound all 25 MB at once. Run this with the production
    # 256 MB database limit; each new comparison is at most 512 KiB.
    records = [make_job(str(i), title=f"Backend Engineer {i}").model_copy(update={
        "raw": {"html": "x" * 100_000, "number": 1, "id": str(i)}}) for i in range(250)]
    assert ingest(engine, records).new == 250
    reordered = [j.model_copy(update={"raw": {"id": j.source_job_id, "number": 1.0,
                                             "html": "x" * 100_000}}) for j in records]
    assert ingest(engine, reordered).changed == 0
    records[125] = records[125].model_copy(update={"raw": {"changed": True}})
    assert ingest(engine, records).changed == 1
    assert row(engine, "125").raw == {"changed": True}


def test_batches_bound_bytes_rows_and_keep_oversized_single_record():
    import json
    records = {str(i): make_job(str(i)).model_copy(update={"raw": {"html": "x" * n}})
               for i, n in enumerate([1] * 60 + [100_000] * 15 + [COMPARISON_MAX_BYTES * 2])}
    batches = list(_comparison_batches(records))
    assert [sid for batch in batches for sid, _ in batch] == list(records)
    assert all(len(batch) <= COMPARISON_MAX_ROWS for batch in batches)
    assert all(sum(len(json.dumps(raw).encode()) for _, raw in batch) <= COMPARISON_MAX_BYTES
               or len(batch) == 1 for batch in batches)


def test_expiry_rechecks_policy_changes_and_corrected_dates(engine):
    now = datetime.now(UTC)
    job = make_job("older", posted_at=now-timedelta(days=5))
    ingest(engine, [job])
    with engine.begin() as conn:
        assert run_filters(conn, FilterSettings(max_posting_age_days=3)).rejected.total() == 1
        assert run_filters(conn, FilterSettings(max_posting_age_days=3)).rejected.total() == 0
        assert run_filters(conn, FilterSettings(max_posting_age_days=7)).passed == 1
        assert run_filters(conn, FilterSettings(max_posting_age_days=3)).rejected.total() == 1
    ingest(engine, [job.model_copy(update={"posted_at": now})])
    with engine.begin() as conn:
        assert run_filters(conn, FilterSettings(max_posting_age_days=3)).passed == 1


def test_empty_incremental_filter_uses_small_indexes(engine):
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO jobs (source,board,source_job_id,company,company_key,title,url,canonical_url,
                description,description_hash,fingerprint,raw,status,posted_at,filtered_at,filter_reason)
            SELECT 'greenhouse','history',n::text,'Acme','acme','Engineer','u','u','d','h','f','{}',
                'FILTERED',now()-interval '10 days',now(),'posting date: older than 3 days'
            FROM generate_series(1,20000) n
        """))
        conn.execute(text("ANALYZE jobs"))
        run_filters(conn, FilterSettings(max_posting_age_days=3))
    plans = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        if (statement.startswith("SELECT jobs.id, jobs.title") or statement.startswith("UPDATE jobs SET")):
            with conn.connection.cursor() as explain:
                explain.execute("EXPLAIN (FORMAT JSON) " + statement, parameters)
                plans.append(str(explain.fetchone()[0]))
    event.listen(engine, "before_cursor_execute", capture)
    try:
        with engine.begin() as conn:
            assert run_filters(conn, FilterSettings(max_posting_age_days=3)).rejected.total() == 0
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(plans) == 4
    assert all("Seq Scan" not in plan for plan in plans)
    assert "ix_jobs_pending_active" in plans[-1]
    assert all("ix_jobs_expiry_active" in plan for plan in plans[:3])


def test_never_filtered_duplicates_do_not_force_sequential_sweeps(engine):
    """Undated duplicates keep filtered_at NULL forever; they must not make the
    sweep estimate a large candidate set and scan the whole jobs heap."""
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO jobs (source,board,source_job_id,company,company_key,title,url,canonical_url,
                description,description_hash,fingerprint,raw,status,posted_at,duplicate_of_id,dedup_reason)
            SELECT 'workday','dupes',n::text,'Acme','acme','Engineer','u','u','d','h','f','{}',
                'DUPLICATE',NULL,NULL,NULL FROM generate_series(1,30000) n
        """))
        conn.execute(text("UPDATE jobs SET duplicate_of_id = id, dedup_reason = 'fingerprint'"))
        conn.execute(text("ANALYZE jobs"))
        run_filters(conn, FilterSettings(max_posting_age_days=3))
    plans = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT jobs.id, jobs.title") or statement.startswith("UPDATE jobs SET"):
            with conn.connection.cursor() as explain:
                explain.execute("EXPLAIN (FORMAT JSON) " + statement, parameters)
                plans.append(str(explain.fetchone()[0]))
    event.listen(engine, "before_cursor_execute", capture)
    try:
        with engine.begin() as conn:
            # Reproduce production's choice: there the OR-heavy estimate made a
            # heap scan look cheaper than the tiny partial index.
            conn.execute(text("SET LOCAL random_page_cost = 1000"))
            conn.execute(text("SET LOCAL seq_page_cost = 0.01"))
            run_filters(conn, FilterSettings(max_posting_age_days=3))
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert plans and all("Seq Scan" not in plan for plan in plans)

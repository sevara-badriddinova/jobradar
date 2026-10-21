"""Migrations must produce exactly the schema the code expects, from any starting point."""

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from jobradar.db import metadata, migrate


@pytest.fixture
def scratch(database_url):
    """An empty database of its own, so these tests can drop and recreate everything."""
    admin = create_engine(database_url, isolation_level="AUTOCOMMIT")
    name = "jobradar_migrations_test"
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        conn.execute(text(f"CREATE DATABASE {name}"))
    engine = create_engine(admin.url.set(database=name))
    yield engine
    engine.dispose()
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
    admin.dispose()


def head():
    config = Config()
    config.set_main_option("script_location", "jobradar:migrations")
    return ScriptDirectory.from_config(config).get_current_head()


def schema_diff(engine):
    with engine.connect() as conn:
        return compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), metadata)


def version(engine):
    with engine.connect() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def test_fresh_database_matches_the_code(scratch):
    migrate(scratch)
    assert schema_diff(scratch) == []
    assert version(scratch) == head()


def test_migrate_is_idempotent(scratch):
    migrate(scratch)
    migrate(scratch)
    assert version(scratch) == head() and schema_diff(scratch) == []


def test_database_created_before_migrations_is_upgraded_in_place(scratch):
    # Simulate an `init-db` from slices 1-4: the baseline tables, no alembic_version.
    from alembic import command
    from alembic.config import Config
    config = Config()
    config.set_main_option("script_location", "jobradar:migrations")
    with scratch.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "0001")
        conn.execute(text("DROP TABLE alembic_version"))
        conn.execute(text(
            "INSERT INTO jobs (source, board, source_job_id, company, company_key, title, url, "
            "canonical_url, description, description_hash, fingerprint, raw) "
            "VALUES ('greenhouse','acme','1','Acme','acme','SWE','u','u','d','h','f','{}')"))

    migrate(scratch)
    assert schema_diff(scratch) == [] and version(scratch) == head()
    columns = {c["name"] for c in inspect(scratch).get_columns("jobs")}
    assert {"filtered_at", "filter_reason"} <= columns
    with scratch.connect() as conn:  # existing data survives
        assert conn.execute(text("SELECT title FROM jobs")).scalar_one() == "SWE"


def test_review_migration_preserves_previous_button_choices(scratch):
    from alembic import command
    config = Config()
    config.set_main_option("script_location", "jobradar:migrations")
    with scratch.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "0006")
        conn.execute(text("""
            INSERT INTO jobs (source, board, source_job_id, company, company_key, title, url,
                              canonical_url, description, description_hash, fingerprint, raw)
            SELECT 'greenhouse','acme', n::text,'Acme','acme','SWE','u','u','d','h','f','{}'
            FROM generate_series(1,2) n
        """))
        conn.execute(text("""
            INSERT INTO feedback (job_id, kind, source)
            VALUES (1, 'IGNORED', 'telegram'), (2, 'APPLIED', 'telegram'), (2, 'IGNORED', 'telegram')
        """))
    migrate(scratch)
    with scratch.connect() as conn:
        assert conn.execute(text("SELECT job_id, choice FROM job_reviews ORDER BY job_id")).all() == [
            (1, "IGNORED"), (2, "APPLIED")]


def test_publication_migration_removes_update_fallback_and_preserves_original_date(scratch, monkeypatch):
    # Rows are ingested into an older schema with today's code: leave out the
    # newer discovered_in_backfill column (written only for initial scans).
    monkeypatch.setattr("jobradar.repository._is_initial_scan", lambda *a: False)
    from datetime import UTC, datetime
    from alembic import command
    from jobradar.db import jobs
    from sqlalchemy import select
    from tests.factories import make_job
    from tests.integration.helpers import ingest

    config = Config()
    config.set_main_option("script_location", "jobradar:migrations")
    with scratch.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "0007")
    old_date = datetime(2026, 8, 1, tzinfo=UTC)
    records = [
        make_job('known', title='Engineer known').model_copy(update={
            'raw': {'first_published': old_date.isoformat(), 'updated_at': '2026-10-06T14:00:00Z'}}),
        make_job('unknown', title='Engineer unknown').model_copy(update={
            'raw': {'updated_at': '2026-10-06T14:00:00Z'}}),
        make_job('invalid', title='Engineer invalid').model_copy(update={
            'raw': {'first_published': 'yesterday', 'updated_at': '2026-10-06T14:00:00Z'}}),
    ]
    ingest(scratch, records)
    migrate(scratch)
    with scratch.connect() as conn:
        dates = dict(conn.execute(select(jobs.c.source_job_id, jobs.c.posted_at)).all())
    assert dates == {'known': old_date, 'unknown': None, 'invalid': None}

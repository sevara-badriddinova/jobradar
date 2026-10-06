"""Integration fixtures: a real PostgreSQL database.

The schema is created once per session; each test starts from empty tables
(TRUNCATE is far faster than drop/create per test).
"""

import os

import pytest
from sqlalchemy import text

from jobradar.db import make_engine, metadata, migrate


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("JOBRADAR_TEST_DATABASE_URL")
    if not url:
        pytest.skip("JOBRADAR_TEST_DATABASE_URL not set; skipping integration tests")
    if not url.rstrip("/").endswith("_test"):
        # This fixture TRUNCATEs every table. Refuse to point it at real data.
        pytest.exit(f"refusing to run against non-test database: {url}", returncode=2)
    return url


@pytest.fixture(scope="session")
def _schema_engine(database_url):
    eng = make_engine(database_url)
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    migrate(eng)  # build the schema the same way production does
    yield eng
    eng.dispose()


@pytest.fixture
def engine(_schema_engine):
    tables = ", ".join(t.name for t in metadata.sorted_tables)
    with _schema_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    return _schema_engine


@pytest.fixture
def db_env(monkeypatch, database_url):
    """Point the app's own config (used by the CLI) at the test database."""
    monkeypatch.setenv("JOBRADAR_DATABASE_URL", database_url)
    return database_url

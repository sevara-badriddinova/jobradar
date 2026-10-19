import pytest

from jobradar.db import DEFAULT_DATABASE_URL, make_engine


@pytest.mark.parametrize("configured,explicit,expected", [
    ({}, None, DEFAULT_DATABASE_URL),
    ({"DATABASE_URL": "postgresql://localhost/neon"}, None, "neon"),
    ({"DATABASE_URL": "postgresql://localhost/pooled",
      "DATABASE_URL_UNPOOLED": "postgresql://localhost/direct"}, None, "direct"),
    ({"JOBRADAR_DATABASE_URL": "postgresql://localhost/custom",
      "DATABASE_URL_UNPOOLED": "postgresql://localhost/direct"}, None, "custom"),
    ({"JOBRADAR_DATABASE_URL": "postgresql://localhost/custom"},
     "postgresql://localhost/explicit", "explicit"),
])
def test_database_selection(monkeypatch, configured, explicit, expected):
    for key in ("JOBRADAR_DATABASE_URL", "DATABASE_URL_UNPOOLED", "DATABASE_URL"):
        monkeypatch.delenv(key, raising=False)
    for key, value in configured.items():
        monkeypatch.setenv(key, value)
    engine = make_engine(explicit)
    try:
        assert engine.url.database == ("jobradar" if expected == DEFAULT_DATABASE_URL else expected)
        assert engine.url.drivername == "postgresql+psycopg"
    finally:
        engine.dispose()

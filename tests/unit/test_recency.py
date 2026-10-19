from datetime import UTC, datetime, timedelta

import pytest

from jobradar.recency import publication_reason

NOW = datetime(2026, 10, 6, 14, tzinfo=UTC)


@pytest.mark.parametrize('timestamp,reason', [
    (NOW, None),
    (NOW - timedelta(days=14), None),
    (NOW - timedelta(days=14, microseconds=1), 'posting date: older than 14 days'),
    (None, 'posting date: unavailable'),
    (NOW + timedelta(seconds=1), 'posting date: in the future'),
    (datetime(2026, 10, 5, 14), None),
])
def test_strict_publication_window(timestamp, reason):
    assert publication_reason(timestamp, 14, NOW) == reason


def test_optional_cutoff():
    assert publication_reason(None, None, NOW) is None

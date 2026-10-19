from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from jobradar.job_age import posting_age

NOW = datetime(2026, 10, 6, 14, tzinfo=UTC)


@pytest.mark.parametrize("delta,label", [(timedelta(seconds=10), "just now"),
    (timedelta(minutes=1), "1 minute ago"), (timedelta(hours=3), "3 hours ago"),
    (timedelta(days=2), "2 days ago")])
def test_publication_age(delta, label):
    job = SimpleNamespace(source="ashby", posted_at=NOW-delta, first_seen_at=NOW, raw={})
    assert posting_age(job, NOW) == f"Posted {label}"


def test_discovery_is_not_mislabeled_as_publication():
    job = SimpleNamespace(posted_at=None, first_seen_at=NOW-timedelta(hours=2))
    assert posting_age(job, NOW) == "First seen 2 hours ago"
    assert posting_age(SimpleNamespace(), NOW) == "Posting date unavailable"


def test_greenhouse_update_is_not_mislabeled_as_publication():
    job = SimpleNamespace(source="greenhouse", posted_at=NOW-timedelta(days=1),
                          raw={"updated_at": "2026-10-05T14:00:00Z"})
    assert posting_age(job, NOW) == "Updated 1 day ago"
    job.raw["first_published"] = "2026-10-01T10:00:00-04:00"
    assert posting_age(job, NOW) == "Posted 5 days ago"

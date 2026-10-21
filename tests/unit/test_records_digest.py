"""Record-level snapshot digests let an identical listing skip normalization."""

import pytest

from jobradar.collectors.base import JobCollector, UnchangedSnapshot
from jobradar.models import Job


class Counting(JobCollector):
    source = "greenhouse"
    volatile_keys = ("postedOn",)

    def __init__(self, records):
        super().__init__("acme", company="Acme")
        self.records, self.normalized = records, 0

    def collect(self):
        return self.normalize_all(list(self.records))

    def normalize(self, raw):
        self.normalized += 1
        return Job(source="greenhouse", source_job_id=str(raw["id"]), company="Acme", title=raw["title"],
                   location=None, url=f"https://x/{raw['id']}", description="d", posted_at=None, raw=raw)


RECORDS = [{"id": 1, "title": "Engineer", "postedOn": "Posted Today"}, {"id": 2, "title": "Analyst"}]


def digest_of(records, **attrs):
    c = Counting(records)
    for k, v in attrs.items():
        setattr(c, k, v)
    c.collect()
    return c.snapshot_digest


def test_identical_listing_raises_before_normalizing():
    first = digest_of(RECORDS)
    c = Counting(list(reversed(RECORDS)))      # order does not matter
    c.known_digest = first
    with pytest.raises(UnchangedSnapshot) as raised:
        c.collect()
    assert c.normalized == 0 and raised.value.records == 2


def test_volatile_keys_are_ignored_but_real_changes_are_not():
    first = digest_of(RECORDS)
    ticked = [{**RECORDS[0], "postedOn": "Posted Yesterday"}, RECORDS[1]]
    assert digest_of(ticked) == first
    retitled = [{**RECORDS[0], "title": "Senior Engineer"}, RECORDS[1]]
    assert digest_of(retitled) != first
    assert digest_of(RECORDS[:1]) != first


def test_ingestion_parameters_and_partial_snapshots_change_the_digest():
    first = digest_of(RECORDS)
    assert digest_of(RECORDS, digest_salt='{"max_age_days": 60}') != first
    assert digest_of(RECORDS, complete_snapshot=False) != first


def test_empty_listing_has_no_digest():
    c = Counting([])
    assert c.collect() == [] and c.snapshot_digest is None

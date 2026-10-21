"""ingest_snapshot semantics against real PostgreSQL."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from jobradar import dedup
from jobradar.db import jobs as jobs_t
from tests.factories import make_job
from tests.integration.helpers import count, ingest, row


class TestIdempotency:
    def test_first_ingest_is_new_and_reingest_is_a_no_op(self, engine):
        snapshot = [make_job("1"), make_job("2", title="Backend Engineer I")]
        assert ingest(engine, snapshot).new == 2
        for _ in range(3):
            r = ingest(engine, snapshot)
            assert (r.new, r.seen, r.changed, r.duplicates, r.closed) == (0, 2, 0, 0, 0)
        assert count(engine) == 2

    def test_new_ids_are_the_inserted_primary_keys(self, engine):
        r = ingest(engine, [make_job("1"), make_job("2", title="Other")])
        assert sorted(r.new_ids) == sorted([row(engine, "1").id, row(engine, "2").id])

    def test_in_batch_repeat_is_collapsed_last_one_wins(self, engine):
        r = ingest(engine, [make_job("1", description="old"), make_job("1", description="new")])
        assert r.new == 1 and row(engine, "1").description == "new"

    def test_same_id_from_different_sources_are_different_jobs(self, engine):
        ingest(engine, [make_job("1", source="greenhouse")], source="greenhouse")
        r = ingest(engine, [make_job("1", source="lever", title="Other", url="https://jobs.lever.co/a/1")],
                   source="lever")
        assert r.new == 1 and count(engine) == 2


class TestStoredFields:
    def test_row_contains_normalized_and_derived_fields(self, engine):
        job = make_job("1", url="https://www.acme.com/c/?gh_jid=1&utm_source=x")
        ingest(engine, [job], board="acme")
        r = row(engine, "1")
        assert (r.source, r.board, r.company, r.title, r.location, r.status) == (
            "greenhouse", "acme", "Acme", "Software Engineer, New Grad", "New York, NY", "DISCOVERED")
        assert r.url == job.url  # original URL kept for display/clicks
        assert r.canonical_url == "https://acme.com/c?gh_jid=1"
        assert r.company_key == "acme"
        assert r.fingerprint == dedup.fingerprint(job.company, job.title, job.location)
        assert r.description_hash == dedup.description_hash(job.description)
        assert r.raw == {"id": "1", "title": job.title}  # JSONB round-trip
        assert r.posted_at == datetime(2026, 10, 1, tzinfo=timezone.utc)
        assert r.duplicate_of_id is None and r.dedup_reason is None
        assert r.closed_at is None and r.content_changed_at is None
        assert r.first_seen_at == r.last_seen_at

    def test_unchanged_repoll_writes_nothing(self, engine):
        # Re-listing a known open job must not rewrite its row (each rewrite is a
        # new tuple + WAL + index entries); the board's last success records it.
        ingest(engine, [make_job("1")])
        before = row(engine, "1")
        with engine.connect() as conn:
            xmin = conn.execute(text("SELECT xmin::text FROM jobs WHERE source_job_id = '1'")).scalar_one()
        ingest(engine, [make_job("1")])
        after = row(engine, "1")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT xmin::text FROM jobs WHERE source_job_id = '1'")).scalar_one() == xmin
        assert after.first_seen_at == before.first_seen_at
        assert after.last_seen_at == before.last_seen_at


class TestChangeDetection:
    @pytest.mark.parametrize("change", [
        {"description": "Build backend services. Now with Kafka."},
        {"title": "Software Engineer I"},
        {"location": "Remote"},
        {"url": "https://boards.greenhouse.io/acme/jobs/1?v=2"},
    ])
    def test_material_change_is_recorded_but_not_new(self, engine, change):
        ingest(engine, [make_job("1")])
        r = ingest(engine, [make_job("1", **change)])
        assert (r.new, r.changed, r.seen) == (0, 1, 1)
        stored = row(engine, "1")
        assert stored.content_changed_at is not None and stored.status == "DISCOVERED"
        for field, value in change.items():
            assert getattr(stored, field) == value

    def test_derived_keys_are_recomputed_on_change(self, engine):
        ingest(engine, [make_job("1")])
        ingest(engine, [make_job("1", title="Data Engineer")])
        stored = row(engine, "1")
        assert stored.fingerprint == dedup.fingerprint("Acme", "Data Engineer", "New York, NY")

    def test_whitespace_only_description_change_is_not_a_change(self, engine):
        ingest(engine, [make_job("1", description="Build backend services.")])
        r = ingest(engine, [make_job("1", description="  Build   backend\nservices. ")])
        assert r.changed == 0


class TestClosing:
    def test_missing_job_is_closed_and_reopens_without_being_new(self, engine):
        ingest(engine, [make_job("1"), make_job("2", title="Data Engineer")])
        r = ingest(engine, [make_job("1")])
        assert r.closed == 1 and row(engine, "2").closed_at is not None
        r = ingest(engine, [make_job("1"), make_job("2", title="Data Engineer")])
        assert (r.new, r.reopened, r.closed) == (0, 1, 0) and row(engine, "2").closed_at is None

    def test_closing_does_not_touch_pipeline_status(self, engine):
        ingest(engine, [make_job("1"), make_job("2", title="Other")])
        with engine.begin() as conn:
            conn.execute(jobs_t.update().where(jobs_t.c.source_job_id == "2").values(status="APPLIED"))
        ingest(engine, [make_job("1")])
        stored = row(engine, "2")
        assert stored.status == "APPLIED" and stored.closed_at is not None

    def test_already_closed_jobs_are_not_closed_again(self, engine):
        ingest(engine, [make_job("1"), make_job("2", title="Other")])
        assert ingest(engine, [make_job("1")]).closed == 1
        assert ingest(engine, [make_job("1")]).closed == 0

    def test_empty_snapshot_does_not_mass_close(self, engine):
        ingest(engine, [make_job("1")])
        assert ingest(engine, []).closed == 0 and row(engine, "1").closed_at is None

    def test_incomplete_snapshot_never_closes(self, engine):
        ingest(engine, [make_job("1"), make_job("2", title="Other")])
        assert ingest(engine, [make_job("1")], complete=False).closed == 0

    def test_close_detection_is_scoped_to_board_and_source(self, engine):
        ingest(engine, [make_job("1")], board="acme")
        ingest(engine, [make_job("9", company="Other", title="SWE")], board="other")
        ingest(engine, [make_job("L1", source="lever", title="X", url="https://jobs.lever.co/acme/1")],
               source="lever", board="acme")
        ingest(engine, [make_job("1")], board="acme")
        assert row(engine, "9").closed_at is None
        assert row(engine, "L1", source="lever").closed_at is None


class TestDedupLevels:
    def test_level2_same_canonical_url_under_new_id(self, engine):
        original = make_job("1", url="https://acme.com/careers?gh_jid=1")
        ingest(engine, [original])
        r = ingest(engine, [original, make_job("1b", title="Different title",
                                               url="https://www.acme.com/careers/?gh_jid=1&utm_source=x")])
        assert (r.new, r.duplicates) == (0, 1)
        dup = row(engine, "1b")
        assert (dup.status, dup.dedup_reason, dup.duplicate_of_id) == ("DUPLICATE", "canonical_url", row(engine, "1").id)

    def test_level2_across_sources(self, engine):
        ingest(engine, [make_job("1", url="https://acme.com/jobs/swe-ng")], source="greenhouse")
        r = ingest(engine, [make_job("abc", source="lever", title="SWE NG", url="https://acme.com/jobs/swe-ng/")],
                   source="lever", board="acme")
        assert r.duplicates == 1 and row(engine, "abc", source="lever").dedup_reason == "canonical_url"

    def test_level2_wins_over_level3(self, engine):
        ingest(engine, [make_job("1", url="https://acme.com/j?id=1")])
        r = ingest(engine, [make_job("1", url="https://acme.com/j?id=1"), make_job("2", url="https://acme.com/j?id=1")])
        assert r.duplicates == 1 and row(engine, "2").dedup_reason == "canonical_url"

    def test_level3_fingerprint_while_original_open_is_duplicate(self, engine):
        ingest(engine, [make_job("1")])
        r = ingest(engine, [make_job("1"), make_job("2", title="software engineer - NEW GRAD")])
        assert (r.new, r.duplicates) == (0, 1)
        assert (row(engine, "2").status, row(engine, "2").dedup_reason) == ("DUPLICATE", "fingerprint")

    def test_level3_fingerprint_after_original_closed_is_repost(self, engine):
        ingest(engine, [make_job("1"), make_job("x", title="Other role")])
        ingest(engine, [make_job("x", title="Other role")])  # job 1 closes
        r = ingest(engine, [make_job("x", title="Other role"), make_job("2")])
        assert (r.new, r.duplicates) == (0, 1)
        rep = row(engine, "2")
        assert (rep.status, rep.dedup_reason, rep.duplicate_of_id) == ("REPOSTED", "fingerprint", row(engine, "1").id)

    def test_level3_within_one_batch(self, engine):
        r = ingest(engine, [make_job("1"), make_job("2")])
        assert (r.new, r.duplicates) == (1, 1)
        assert row(engine, "2").duplicate_of_id == row(engine, "1").id

    def test_same_title_different_location_is_not_a_duplicate(self, engine):
        r = ingest(engine, [make_job("1", location="New York, NY"), make_job("2", location="Seattle, WA")])
        assert r.new == 2

    def test_same_title_different_company_is_not_a_duplicate(self, engine):
        ingest(engine, [make_job("1", company="Acme")], board="acme")
        r = ingest(engine, [make_job("2", company="Globex")], board="globex")
        assert r.new == 1

    def test_duplicates_link_to_the_root_not_to_each_other(self, engine):
        ingest(engine, [make_job("1")])
        ingest(engine, [make_job("1"), make_job("2")])
        ingest(engine, [make_job("1"), make_job("2"), make_job("3")])
        assert row(engine, "3").duplicate_of_id == row(engine, "1").id

    def test_duplicates_are_seen_not_new_on_later_polls(self, engine):
        ingest(engine, [make_job("1"), make_job("2")])
        r = ingest(engine, [make_job("1"), make_job("2")])
        assert (r.new, r.duplicates, r.seen) == (0, 0, 2)


class TestTransactionality:
    def test_wrong_source_in_snapshot_is_rejected(self, engine):
        with pytest.raises(ValueError, match="'lever'"):
            ingest(engine, [make_job("1"), make_job("2", source="lever")])
        assert count(engine) == 0

    def test_failure_midway_rolls_back_the_whole_snapshot(self, engine, monkeypatch):
        ingest(engine, [make_job("1")])
        from jobradar import repository
        real = repository._find_original
        calls = {"n": 0}

        def flaky(conn, column, value):
            calls["n"] += 1
            if calls["n"] > 2:
                raise RuntimeError("db hiccup")
            return real(conn, column, value)

        monkeypatch.setattr(repository, "_find_original", flaky)
        with pytest.raises(RuntimeError):
            ingest(engine, [make_job("1", description="changed"), make_job("2", title="A"),
                            make_job("3", title="B")])
        # Nothing from the failed snapshot persisted: no new rows, no update.
        assert count(engine) == 1
        assert row(engine, "1").description == "Build backend services."

        monkeypatch.setattr(repository, "_find_original", real)
        r = ingest(engine, [make_job("1", description="changed"), make_job("2", title="A"),
                            make_job("3", title="B")])
        assert (r.new, r.changed) == (2, 1)  # the retry does the work exactly once

import pytest
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError

from jobradar import cli
from jobradar.companies import CompanySpec, list_companies, sync_companies
from jobradar.db import companies as companies_t


def spec(name, board=None, **kw):
    return CompanySpec(name=name, collector="greenhouse", board=board or name.lower(), **kw)


def sync(engine, specs):
    with engine.begin() as conn:
        return sync_companies(conn, specs)


def rows(engine, **kw):
    with engine.connect() as conn:
        return list_companies(conn, **kw)


class TestSync:
    def test_first_sync_adds_everything(self, engine):
        r = sync(engine, [spec("Stripe", priority=1), spec("Figma")])
        assert (r.added, r.updated, r.disabled, r.unchanged) == (["Stripe", "Figma"], [], [], 0)
        assert [(c.name, c.board, c.priority, c.enabled) for c in rows(engine)] == [
            ("Stripe", "stripe", 1, True), ("Figma", "figma", 2, True)]

    def test_resync_is_idempotent(self, engine):
        specs = [spec("Stripe"), spec("Figma")]
        sync(engine, specs)
        r = sync(engine, specs)
        assert not r.changed and r.unchanged == 2

    @pytest.mark.parametrize("change", [{"priority": 1}, {"enabled": False}, {"name": "Stripe Inc"}])
    def test_changes_are_updated_in_place(self, engine, change):
        sync(engine, [spec("Stripe", board="stripe")])
        before = rows(engine, enabled_only=False)[0]
        r = sync(engine, [spec(**{"name": "Stripe", "board": "stripe", **change})])
        after = rows(engine, enabled_only=False)[0]
        assert r.updated and after.id == before.id and after.updated_at > before.updated_at
        for k, v in change.items():
            assert getattr(after, k) == v

    def test_removed_from_file_is_disabled_not_deleted(self, engine):
        sync(engine, [spec("Stripe"), spec("Figma")])
        r = sync(engine, [spec("Stripe")])
        assert r.disabled == ["Figma"]
        assert [c.name for c in rows(engine)] == ["Stripe"]
        assert {c.name: c.enabled for c in rows(engine, enabled_only=False)} == {"Stripe": True, "Figma": False}
        assert not sync(engine, [spec("Stripe")]).changed  # already disabled: no-op

    def test_readding_re_enables_the_same_row(self, engine):
        sync(engine, [spec("Figma")])
        original_id = rows(engine)[0].id
        sync(engine, [])
        r = sync(engine, [spec("Figma")])
        assert r.updated == ["Figma"] and rows(engine)[0].id == original_id

    def test_empty_file_disables_everything(self, engine):
        sync(engine, [spec("Stripe")])
        assert sync(engine, []).disabled == ["Stripe"]


class TestList:
    @pytest.fixture(autouse=True)
    def seed(self, engine):
        sync(engine, [spec("Stripe", priority=1), spec("Figma", priority=2), spec("Discord", priority=3),
                      spec("Paused", priority=1, enabled=False)])

    def test_ordered_by_priority_then_name_and_enabled_only(self, engine):
        assert [c.name for c in rows(engine)] == ["Stripe", "Figma", "Discord"]

    def test_priority_means_this_level_or_more_important(self, engine):
        assert [c.name for c in rows(engine, priority=1)] == ["Stripe"]
        assert [c.name for c in rows(engine, priority=2)] == ["Stripe", "Figma"]

    def test_filter_by_board_or_name(self, engine):
        assert [c.name for c in rows(engine, names=["figma", "Discord Inc."])] == ["Figma", "Discord"]

    def test_disabled_can_be_listed_explicitly(self, engine):
        assert "Paused" in [c.name for c in rows(engine, enabled_only=False)]


class TestSchema:
    def insert(self, engine, **kw):
        values = dict(name="X", collector="greenhouse", board="x") | kw
        with engine.begin() as conn:
            conn.execute(insert(companies_t).values(**values))

    def test_one_row_per_board(self, engine):
        self.insert(engine)
        with pytest.raises(IntegrityError, match="uq_companies_collector_board"):
            self.insert(engine, name="Other name")

    @pytest.mark.parametrize("kw,constraint", [
        ({"priority": 5}, "ck_companies_priority"),
        ({"board": "Stripe"}, "ck_companies_board_normalized"),
        ({"board": ""}, "ck_companies_board_normalized"),
    ])
    def test_constraints(self, engine, kw, constraint):
        with pytest.raises(IntegrityError, match=constraint):
            self.insert(engine, **kw)


@pytest.mark.parametrize("fail_fetches", [False, True])
def test_three_fifty_board_cycles_rotate_across_fresh_connections(engine, fail_fetches):
    from jobradar.collectors.base import CollectorError
    from jobradar.pipeline import run_all
    from tests.integration.test_pipeline import Fake

    sync(engine, [spec(f"Company {i:03}", board=f"c{i:03}") for i in range(150)])
    batches = []
    for _ in range(3):
        # A cold worker must use durable attempt history, not an in-memory cache.
        engine.dispose()
        selected = rows(engine, limit=50)
        boards = {c.board for c in selected}
        assert len(boards) == 50
        assert all(boards.isdisjoint(previous) for previous in batches)
        reports = run_all(engine, [Fake(c.board,
            error=CollectorError("source unavailable") if fail_fetches else None)
            for c in selected])
        assert all(report.ok is (not fail_fetches) for report in reports)
        batches.append(boards)

    assert len(set.union(*batches)) == 150
    engine.dispose()
    # Only after every board was attempted may the oldest batch repeat.
    assert {c.board for c in rows(engine, limit=50)} == batches[0]


class TestCli:
    def test_sync_dry_run_writes_nothing(self, engine, db_env, tmp_path, capsys):
        f = tmp_path / "c.toml"
        f.write_text('[[company]]\nname = "Stripe"\ncollector = "greenhouse"\nboard = "stripe"\n')
        assert cli.main(["companies", "sync", str(f), "--dry-run"]) == 0
        assert "[dry run] would have added 1: Stripe" in capsys.readouterr().out
        assert rows(engine) == []

        assert cli.main(["companies", "sync", str(f)]) == 0
        assert "added 1: Stripe" in capsys.readouterr().out
        assert cli.main(["companies", "sync", str(f)]) == 0
        assert "already in sync" in capsys.readouterr().out

    def test_list(self, engine, db_env, capsys):
        sync(engine, [spec("Stripe", priority=1), spec("Paused", enabled=False)])
        cli.main(["companies", "list"])
        out = capsys.readouterr().out
        assert "Stripe" in out and "Paused" not in out and "1 company" in out
        cli.main(["companies", "list", "--all"])
        assert "Paused" in capsys.readouterr().out

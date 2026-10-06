"""CLI behaviour with the database and network stubbed out."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from jobradar import cli
from jobradar.pipeline import RunReport
from jobradar.repository import IngestResult


def company(name, board, collector="greenhouse", priority=2):
    return SimpleNamespace(name=name, board=board, collector=collector, priority=priority, enabled=True)


class FakeEngine:
    @contextmanager
    def connect(self):
        yield object()


@pytest.fixture
def registry(monkeypatch):
    """Stub the registry + pipeline; returns a dict that records what ran."""
    state = {"rows": [], "reports": None, "ran": None}

    def fake_list(conn, *, enabled_only=True, priority=None, names=None):
        state["list_args"] = {"priority": priority, "names": names}
        rows = [r for r in state["rows"] if priority is None or r.priority <= priority]
        if names:
            rows = [r for r in rows if any(cli.matches(r, n) for n in names)]
        return rows

    def fake_run_all(engine, collectors):
        state["ran"] = [(c.source, c.board, c.company) for c in collectors]
        state["clients"] = {id(c._client) for c in collectors}
        return state["reports"] or [RunReport(c.source, c.board, ok=True, result=IngestResult())
                                    for c in collectors]

    monkeypatch.setattr(cli, "make_engine", FakeEngine)
    monkeypatch.setattr(cli, "list_companies", fake_list)
    monkeypatch.setattr(cli, "run_all", fake_run_all)
    return state


def test_requires_a_command():
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2


@pytest.mark.parametrize("flag,value", [("--limit", "0"), ("--limit", "-1"), ("--days", "0")])
def test_sync_rejects_nonpositive_budgets(flag, value):
    with pytest.raises(SystemExit) as exc:
        cli.main(["applications", "sync", flag, value])
    assert exc.value.code == 2


@pytest.mark.parametrize("argv", [["companies"], ["collect", "--priority", "4"], ["companies", "explode"]])
def test_invalid_arguments_exit_2(argv):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2


def test_collect_all_uses_registry_names_and_shares_one_client(registry, capsys):
    registry["rows"] = [company("Stripe", "stripe", priority=1), company("Figma", "figma")]
    assert cli.main(["collect"]) == 0
    assert registry["ran"] == [("greenhouse", "stripe", "Stripe"), ("greenhouse", "figma", "Figma")]
    assert len(registry["clients"]) == 1  # connection pooling across boards


def test_collect_passes_priority_filter(registry):
    registry["rows"] = [company("Stripe", "stripe", priority=1), company("Discord", "discord", priority=3)]
    cli.main(["collect", "--priority", "1"])
    assert registry["list_args"]["priority"] == 1
    assert [b for _, b, _ in registry["ran"]] == ["stripe"]


@pytest.mark.parametrize("name", ["stripe", "Stripe", " STRIPE ", "Stripe, Inc."])
def test_collect_by_board_or_company_name(registry, name):
    registry["rows"] = [company("Stripe", "stripe"), company("Figma", "figma")]
    assert cli.main(["collect", name]) == 0
    assert [b for _, b, _ in registry["ran"]] == ["stripe"]


def test_collect_unknown_company_exits_2_without_collecting(registry, capsys):
    registry["rows"] = [company("Stripe", "stripe")]
    assert cli.main(["collect", "stripe", "nopeco"]) == 2
    assert registry["ran"] is None
    assert "nopeco" in capsys.readouterr().err


def test_collect_with_empty_registry_explains_what_to_do(registry, capsys):
    assert cli.main(["collect"]) == 2
    assert "companies sync" in capsys.readouterr().err


def test_collect_exit_code_is_1_if_any_board_failed(registry, capsys):
    registry["rows"] = [company("Stripe", "stripe"), company("Nope", "nope")]
    registry["reports"] = [
        RunReport("greenhouse", "stripe", ok=True, fetched=1, result=IngestResult(new_ids=[1])),
        RunReport("greenhouse", "nope", ok=False, error="CollectorError: HTTP 404"),
    ]
    assert cli.main(["collect"]) == 1
    assert "CollectorError: HTTP 404" in capsys.readouterr().out


def test_companies_sync_with_invalid_file_never_touches_db(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "make_engine", lambda: pytest.fail("DB must not be opened"))
    bad = tmp_path / "companies.toml"
    bad.write_text('[[company]]\nname = "X"\ncollector = "monster"\nboard = "x"\n')
    assert cli.main(["companies", "sync", str(bad)]) == 2
    assert "unknown collector 'monster'" in capsys.readouterr().err


def test_companies_sync_missing_file(capsys, tmp_path):
    assert cli.main(["companies", "sync", str(tmp_path / "missing.toml")]) == 2
    assert "file not found" in capsys.readouterr().err

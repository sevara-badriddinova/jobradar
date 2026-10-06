import tomllib
from pathlib import Path

import pytest

from jobradar.collectors import COLLECTORS, GreenhouseCollector, build_collector
from jobradar.companies import CompanyFileError, CompanySpec, load_company_file, parse_companies

REPO_ROOT = Path(__file__).parents[2]


def parse(toml: str):
    return parse_companies(tomllib.loads(toml))


def test_minimal_entry_gets_defaults():
    (spec,) = parse('[[company]]\nname = " Stripe "\ncollector = "Greenhouse"\nboard = " STRIPE "\n')
    assert spec == CompanySpec(name="Stripe", collector="greenhouse", board="stripe", priority=2, enabled=True)


def test_empty_file_is_an_empty_registry():
    assert parse("") == []


@pytest.mark.parametrize("entry,message", [
    ('name = ""\ncollector = "greenhouse"\nboard = "x"', "must not be blank"),
    ('name = "X"\ncollector = "monster"\nboard = "x"', "unknown collector 'monster'"),
    ('name = "X"\ncollector = "greenhouse"\nboard = "boards.greenhouse.io/x"', "bare board slug"),
    ('name = "X"\ncollector = "greenhouse"\nboard = ""', "bare board slug"),
    ('name = "X"\ncollector = "greenhouse"\nboard = "x"\npriority = 0', "greater than or equal to 1"),
    ('name = "X"\ncollector = "greenhouse"\nboard = "x"\npriority = 4', "less than or equal to 3"),
    ('name = "X"\ncollector = "greenhouse"\nboard = "x"\nprority = 1', "prority"),  # typo
    ('name = "X"\ncollector = "greenhouse"', "board"),                               # missing
])
def test_invalid_entries_are_rejected_with_a_clear_message(entry, message):
    with pytest.raises(CompanyFileError, match=message):
        parse(f"[[company]]\n{entry}\n")


def test_all_errors_reported_together():
    with pytest.raises(CompanyFileError) as exc:
        parse('[[company]]\nname = "A"\ncollector = "nope"\nboard = "a"\n'
              '[[company]]\nname = "B"\ncollector = "greenhouse"\nboard = "b"\npriority = 9\n')
    assert "A:" in str(exc.value) and "B:" in str(exc.value)


def test_duplicate_board_is_rejected():
    with pytest.raises(CompanyFileError, match="duplicate of Stripe"):
        parse('[[company]]\nname = "Stripe"\ncollector = "greenhouse"\nboard = "stripe"\n'
              '[[company]]\nname = "Stripe 2"\ncollector = "greenhouse"\nboard = "STRIPE"\n')


def test_company_must_be_an_array_of_tables():
    with pytest.raises(CompanyFileError, match=r"\[\[company\]\]"):
        parse('company = "stripe"\n')


def test_invalid_toml(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text("[[company]\nname=")
    with pytest.raises(CompanyFileError, match="invalid TOML"):
        load_company_file(f)


def test_shipped_companies_file_is_valid():
    specs = load_company_file(REPO_ROOT / "companies.toml")
    assert len(specs) >= 5
    assert all(s.collector in COLLECTORS for s in specs)


def test_build_collector():
    c = build_collector("greenhouse", "stripe", company="Stripe")
    assert isinstance(c, GreenhouseCollector) and (c.board, c.company) == ("stripe", "Stripe")
    with pytest.raises(ValueError, match="unknown collector"):
        build_collector("monster", "x")

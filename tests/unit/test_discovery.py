import pytest

from jobradar.companies import CompanyFileError, CompanySpec, parse_companies
from jobradar.discovery import parse_directory


HEADER = "platform,token,company,board_url\n"


def test_registry_expands_preserves_manual_settings_and_reports_gaps():
    existing = [CompanySpec(name="Acme", collector="greenhouse", board="acme", enabled=False)]
    specs, report = parse_directory(HEADER +
        "greenhouse,acme,acme,https://job-boards.greenhouse.io/acme\n"
        "ashby,newco,NewCo,https://jobs.ashbyhq.com/newco\n"
        "lever,euro,Euro,https://jobs.eu.lever.co/euro\n"
        "workday,unknown,Unknown,https://example.com/unknown\n", existing)
    assert len(specs) == 3
    assert next(s for s in specs if s.board == "acme").enabled is False
    assert next(s for s in specs if s.name == "Euro").board == "eu:euro"
    assert report["unsupported_directory_boards"] == {}
    assert report["invalid_rows"] == 1


@pytest.mark.parametrize("body", ["broken csv", HEADER,
    HEADER + "greenhouse,bad,Bad,https://attacker.example/bad\n"])
def test_invalid_directory_cannot_replace_registry(body):
    with pytest.raises(CompanyFileError):
        parse_directory(body, [])


def test_misspelled_registry_cannot_disable_all_companies():
    with pytest.raises(CompanyFileError):
        parse_companies({"compnay": []})


def test_all_five_new_platforms_register_without_sponsorship_inference():
    body = HEADER + '\n'.join([
        'workday,wd1/acme/External,Acme,https://acme.wd1.myworkdayjobs.com/External',
        'workable,acme,Acme,https://apply.workable.com/acme',
        'recruitee,acme,Acme,https://acme.recruitee.com',
        'rippling,acme,Acme,https://ats.rippling.com/acme',
        'personio,acme,Acme,https://acme.jobs.personio.de',
    ])
    specs, report = parse_directory(body, [])
    assert len(specs) == 5
    assert report['unsupported_directory_boards'] == {} and report['invalid_rows'] == 0
    assert report['sponsorship'].startswith('Not inferred')


def test_new_collectors_invalidate_cached_discovery(tmp_path):
    import json
    from datetime import UTC, datetime
    from jobradar.discovery import refresh_directory
    from tests.factories import RecordingTransport
    import httpx
    path = tmp_path / 'companies.toml'
    path.write_text('')
    path.with_suffix('.coverage.json').write_text(json.dumps({'retrieved_at': datetime.now(UTC).isoformat(),
        'collector_platforms': ['ashby', 'greenhouse', 'lever']}))
    t = RecordingTransport(httpx.Response(200, text=HEADER + 'workable,acme,Acme,https://apply.workable.com/acme\n'))
    report = refresh_directory(t.client(), path)
    assert len(t.requests) == 1 and report['registered_boards'] == 1
    assert refresh_directory(t.client(), path) == report and len(t.requests) == 1

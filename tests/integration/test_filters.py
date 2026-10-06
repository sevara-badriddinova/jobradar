from sqlalchemy import update

from jobradar import cli
from jobradar.db import jobs as jobs_t
from jobradar.filters import run_filters
from jobradar.settings import FilterSettings
from tests.factories import make_job
from tests.integration.helpers import ingest, row


def run(engine, settings=FilterSettings(), recheck=False):
    with engine.begin() as conn:
        return run_filters(conn, settings, recheck=recheck)


def seed(engine):
    ingest(engine, [
        make_job("ok", title="Software Engineer, New Grad"),
        make_job("senior", title="Senior Software Engineer"),
        make_job("yoe", title="Backend Engineer", description="7+ years of experience required."),
        make_job("uk", title="Platform Engineer", location="London, United Kingdom"),
    ])


def test_rejects_and_passes_with_reasons(engine):
    seed(engine)
    result = run(engine)
    assert result.passed == 1
    assert dict(result.rejected) == {"title: senior": 1, "experience: requires 7+ years": 1,
                                     "location: London, United Kingdom": 1}
    ok, senior = row(engine, "ok"), row(engine, "senior")
    assert (ok.status, ok.filter_reason, ok.filtered_at is not None) == ("DISCOVERED", None, True)
    assert (senior.status, senior.filter_reason) == ("FILTERED", "title: senior")


def test_each_job_is_filtered_once(engine):
    seed(engine)
    run(engine)
    again = run(engine)
    assert (again.passed, again.rejected.total()) == (0, 0)
    ingest(engine, [make_job("new", title="Data Engineer")], board="other")
    assert run(engine).passed == 1


def test_only_canonical_open_discovered_jobs_are_filtered(engine):
    ingest(engine, [make_job("a", title="Senior Engineer"), make_job("dup", title="Senior Engineer"),
                    make_job("gone", title="Staff Engineer", location="Seattle, WA")])
    with engine.begin() as conn:
        conn.execute(update(jobs_t).where(jobs_t.c.source_job_id == "gone").values(closed_at=jobs_t.c.first_seen_at))
    result = run(engine)
    assert result.rejected.total() == 1  # "dup" is a DUPLICATE, "gone" is closed
    assert row(engine, "dup").status == "DUPLICATE" and row(engine, "gone").filtered_at is None


def test_recheck_applies_new_settings_to_old_decisions(engine):
    seed(engine)
    run(engine)
    loose = FilterSettings(us_only=False, max_years_required=10, title_must_exclude=[])
    result = run(engine, loose, recheck=True)
    assert (result.passed, result.rejected.total()) == (4, 0)
    assert row(engine, "senior").status == "DISCOVERED" and row(engine, "senior").filter_reason is None


def test_recheck_never_touches_jobs_further_along(engine):
    seed(engine)
    with engine.begin() as conn:
        conn.execute(update(jobs_t).where(jobs_t.c.source_job_id == "senior").values(status="APPLIED"))
    run(engine, recheck=True)
    assert row(engine, "senior").status == "APPLIED"


def test_cli(engine, db_env, tmp_path, capsys):
    seed(engine)
    profile = tmp_path / "profile.toml"
    profile.write_text("[filters]\nus_only = false\n")
    assert cli.main(["--profile", str(profile), "filter"]) == 0
    out = capsys.readouterr().out
    assert "2 passed, 2 rejected" in out and "title: senior" in out and "location" not in out

    profile.write_text("[filters]\nus_onlyy = false\n")
    assert cli.main(["--profile", str(profile), "filter"]) == 2
    assert "us_onlyy" in capsys.readouterr().err


def test_filtered_jobs_disappear_from_new(engine, db_env, capsys):
    seed(engine)
    cli.main(["filter"])
    capsys.readouterr()
    cli.main(["new"])
    out = capsys.readouterr().out
    assert "1 new job(s)" in out and "Senior" not in out


def test_sponsorship_filter_and_recheck_of_matched_jobs(engine):
    ingest(engine, [make_job("nospon", title="Backend Engineer", description="We are unable to sponsor visas."),
                    make_job("ok", title="Platform Engineer", description="We sponsor H-1B.")])
    run(engine)  # without the preference both pass...
    with engine.begin() as conn:  # ...and get matched
        conn.execute(update(jobs_t).values(status="MATCHED"))
    with engine.begin() as conn:
        result = run_filters(conn, FilterSettings(), recheck=True, needs_sponsorship=True)
    assert dict(result.rejected) == {'visa: "unable to sponsor"': 1}
    assert row(engine, "nospon").status == "FILTERED"
    assert row(engine, "ok").status == "MATCHED"  # passing keeps its match


def test_recheck_brings_filtered_jobs_back_as_discovered(engine):
    ingest(engine, [make_job("s", title="Senior Engineer")])
    run(engine)
    run(engine, FilterSettings(title_must_exclude=[]), recheck=True)
    assert row(engine, "s").status == "DISCOVERED"

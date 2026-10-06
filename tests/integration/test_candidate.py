import json

import pytest
from sqlalchemy import func, select

from jobradar import candidate, cli
from jobradar.candidate import active_profile, extract_and_store
from jobradar.db import candidate_profiles
from jobradar.llm import LLMError
from tests.factories import FIXTURES, FakeLLM, candidate_profile

RESUME = FIXTURES / "resume.pdf"


@pytest.fixture
def fake_llm(monkeypatch):
    fake = FakeLLM(candidate_profile())
    monkeypatch.setattr(candidate.llm, "ask", fake)
    return fake


def extract(engine, path=RESUME, force=False):
    with engine.begin() as conn:
        return extract_and_store(conn, path, force=force)


def count(engine):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(candidate_profiles)).scalar_one()


def test_first_extraction_is_stored(engine, fake_llm):
    row, reused = extract(engine)
    assert not reused and row.id == 1 and len(fake_llm.calls) == 1
    assert row.profile == candidate_profile().model_dump(mode="json")
    assert (row.prompt_version, row.model) == (candidate.PROMPT_VERSION, candidate.DEFAULT_MODEL)
    assert len(row.resume_sha256) == 64


def test_same_resume_is_reused_without_calling_the_llm(engine, fake_llm):
    extract(engine)
    row, reused = extract(engine)
    assert reused and row.id == 1 and len(fake_llm.calls) == 1 and count(engine) == 1


def test_force_creates_a_new_version(engine, fake_llm):
    extract(engine)
    row, reused = extract(engine, force=True)
    assert not reused and row.id == 2 and len(fake_llm.calls) == 2


def test_changed_resume_or_model_creates_a_new_version(engine, fake_llm, tmp_path, monkeypatch):
    extract(engine)
    other = tmp_path / "resume.txt"
    other.write_text("A different resume")
    assert extract(engine, other)[0].id == 2
    monkeypatch.setenv("JOBRADAR_PROFILE_MODEL", "claude-opus-5-5")
    assert extract(engine, other)[0].id == 3


def test_newest_profile_is_active(engine, monkeypatch):
    monkeypatch.setattr(candidate.llm, "ask", FakeLLM(candidate_profile(languages=["Go"]),
                                                      candidate_profile(languages=["Rust"])))
    extract(engine)
    extract(engine, force=True)
    with engine.connect() as conn:
        assert active_profile(conn).profile["languages"] == ["Rust"]


def test_llm_failure_stores_nothing(engine, monkeypatch):
    monkeypatch.setattr(candidate.llm, "ask", FakeLLM(LLMError("rate limited")))
    with pytest.raises(LLMError):
        extract(engine)
    assert count(engine) == 0


class TestCli:
    def test_extract_then_show(self, engine, db_env, fake_llm, capsys):
        assert cli.main(["profile", "extract", str(RESUME)]) == 0
        out = capsys.readouterr().out
        assert "saved profile v1" in out and "new_grad" in out and "Java, Python, Go" in out

        assert cli.main(["profile", "extract", str(RESUME)]) == 0
        assert "reused profile v1" in capsys.readouterr().out

        assert cli.main(["profile", "show"]) == 0
        out = capsys.readouterr().out
        assert "profile v1" in out and "Distributed KV store" in out and "distributed systems" in out

        assert cli.main(["profile", "show", "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["profile"]["graduation"] == "2026-05" and "interests" in data["preferences"]

    def test_resume_path_defaults_to_preferences(self, engine, db_env, fake_llm, tmp_path, capsys):
        profile = tmp_path / "profile.toml"
        profile.write_text(f'[preferences]\nresume = "{RESUME}"\n')
        assert cli.main(["--profile", str(profile), "profile", "extract"]) == 0

    def test_errors_exit_2_with_a_message(self, engine, db_env, monkeypatch, tmp_path, capsys):
        assert cli.main(["profile", "show"]) == 2
        assert "no profile yet" in capsys.readouterr().err
        assert cli.main(["profile", "extract", str(tmp_path / "missing.pdf")]) == 2
        assert "resume not found" in capsys.readouterr().err
        monkeypatch.setattr(candidate.llm, "ask", FakeLLM(LLMError("invalid x-api-key")))
        assert cli.main(["profile", "extract", str(RESUME)]) == 2
        assert "invalid x-api-key" in capsys.readouterr().err

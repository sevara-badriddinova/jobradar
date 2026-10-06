import pytest

from jobradar import candidate
from jobradar.candidate import ResumeError, extract_profile, read_resume
from jobradar.settings import Preferences, load_settings
from tests.factories import FIXTURES, FakeLLM, candidate_profile


class TestReadResume:
    def test_pdf_all_pages(self):
        text = read_resume(FIXTURES / "resume.pdf")
        assert "B.S. Computer Science" in text and "Teaching Assistant" in text  # page 2 too

    def test_text_file(self, tmp_path):
        f = tmp_path / "resume.md"
        f.write_text("# Resume\nJava")
        assert read_resume(f) == "# Resume\nJava"

    def test_missing(self, tmp_path):
        with pytest.raises(ResumeError, match="not found"):
            read_resume(tmp_path / "nope.pdf")

    def test_pdf_without_text(self):
        with pytest.raises(ResumeError, match="no text found"):
            read_resume(FIXTURES / "blank.pdf")

    def test_not_really_a_pdf(self, tmp_path):
        f = tmp_path / "resume.pdf"
        f.write_text("hello")
        with pytest.raises(ResumeError, match="not a readable PDF"):
            read_resume(f)


class TestExtractProfile:
    def test_asks_for_a_candidate_profile_with_the_resume(self, monkeypatch):
        fake = FakeLLM(candidate_profile())
        monkeypatch.setattr(candidate.llm, "ask", fake)
        assert extract_profile("RESUME TEXT") == candidate_profile()
        (call,) = fake.calls
        assert call["output"] is candidate.CandidateProfile
        assert "<resume>\nRESUME TEXT\n</resume>" in call["prompt"] and "Today is " in call["prompt"]
        assert "no name, email, phone" in call["system"]
        assert call["model"] == candidate.DEFAULT_MODEL

    def test_model_can_be_overridden(self, monkeypatch):
        fake = FakeLLM(candidate_profile())
        monkeypatch.setattr(candidate.llm, "ask", fake)
        monkeypatch.setenv("JOBRADAR_PROFILE_MODEL", "claude-opus-5-5")
        extract_profile("x")
        assert fake.calls[0]["model"] == "claude-opus-5-5"

    def test_profile_schema_has_no_contact_fields(self):
        fields = set(candidate.CandidateProfile.model_fields)
        assert not fields & {"name", "email", "phone", "address", "links"}


class TestPreferences:
    def test_defaults(self, tmp_path):
        assert load_settings(tmp_path / "missing.toml").preferences == Preferences()

    def test_shipped_profile_has_preferences(self):
        prefs = load_settings("profile.toml").preferences
        assert prefs.resume == "resume.pdf" and "distributed systems" in prefs.interests

    def test_typo_rejected(self, tmp_path):
        f = tmp_path / "p.toml"
        f.write_text("[preferences]\ntarget_role = ['x']\n")
        with pytest.raises(Exception, match="target_role"):
            load_settings(f)

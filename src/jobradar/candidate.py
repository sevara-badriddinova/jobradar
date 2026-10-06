"""Your candidate profile: extracted once from your resume, stored with a version.

resume (PDF/text) ──► text ──► LLM structured extraction ──► CandidateProfile ──► candidate_profiles
"""

import hashlib
import os
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from sqlalchemy import insert, select
from sqlalchemy.engine import Connection, Row

from jobradar import llm
from jobradar.db import candidate_profiles

# Bump when SYSTEM or CandidateProfile changes, so `profile extract` re-runs.
PROMPT_VERSION = "1"
DEFAULT_MODEL = "claude-sonnet-5-5"  # a one-off call: worth the stronger model


class ResumeError(Exception):
    """The resume file can't be read as text."""


class Education(BaseModel):
    school: str
    degree: str = Field(description='e.g. "B.S."')
    field: str = Field(description='e.g. "Computer Science"')
    graduation: str | None = Field(description="YYYY-MM, actual or expected")


class WorkExperience(BaseModel):
    company: str
    title: str
    kind: Literal["full_time", "internship", "part_time", "research", "teaching", "other"]
    start: str | None = Field(description="YYYY-MM")
    end: str | None = Field(description='YYYY-MM, or "present"')
    summary: str = Field(description="one or two sentences on what they built and the impact")
    technologies: list[str]


class Project(BaseModel):
    name: str
    summary: str
    technologies: list[str]


class CandidateProfile(BaseModel):
    education: list[Education]
    graduation: str | None = Field(description="YYYY-MM of the most recent (or expected) degree")
    experience_level: Literal["student", "new_grad", "early_career", "mid", "senior"]
    years_of_experience: float = Field(
        description="full-time professional experience after graduation; internships count as 0")
    languages: list[str]
    frameworks: list[str]
    databases: list[str]
    cloud_devops: list[str]
    work_experience: list[WorkExperience]
    projects: list[Project]
    domains: list[str] = Field(description='areas of work, e.g. "distributed systems", "fintech"')
    target_roles: list[str] = Field(description="roles this resume is a good fit for")


SYSTEM = """You turn a software engineering resume into a structured profile.
Rules:
- Use only facts stated in the resume. Don't guess or embellish; leave lists empty when unknown.
- Leave out contact details and identity: no name, email, phone, address or links.
- Dates are YYYY-MM.
- years_of_experience counts only full-time work after graduation; internships, part-time,
  research and teaching count as 0.
- experience_level: "student" if graduating in more than about 6 months, "new_grad" if graduating
  within 6 months or graduated less than a year ago, then "early_career" for up to ~3 years of
  experience, "mid" for ~3-7, "senior" beyond.
- Use the common spelling of technologies (PostgreSQL, Kubernetes, JavaScript, Spring Boot)."""


def read_resume(path: str | Path) -> str:
    path = Path(path)
    if not path.exists():
        raise ResumeError(f"resume not found: {path}")
    if path.suffix.lower() == ".pdf":
        try:
            text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
        except PdfReadError as exc:
            raise ResumeError(f"{path}: not a readable PDF ({exc})") from None
    else:
        text = path.read_text()
    if not text.strip():
        raise ResumeError(f"{path}: no text found (is it a scanned image? export a text PDF or use .txt)")
    return text


def extract_profile(resume_text: str) -> CandidateProfile:
    return llm.ask(
        CandidateProfile, system=SYSTEM,
        prompt=f"Today is {date.today().isoformat()}.\n\n<resume>\n{resume_text}\n</resume>",
        model=model_name(),
    )


def model_name() -> str:
    return os.environ.get("JOBRADAR_PROFILE_MODEL", DEFAULT_MODEL)


def active_profile(conn: Connection) -> Row | None:
    """The newest stored profile, which is the one the matcher uses."""
    return conn.execute(
        select(candidate_profiles).order_by(candidate_profiles.c.id.desc()).limit(1)).first()


def extract_and_store(conn: Connection, resume_path: str | Path, *, force: bool = False) -> tuple[Row, bool]:
    """Extract and save a new profile version. Returns (profile row, reused).

    If the active profile came from the same resume, prompt and model, it's
    reused without calling the LLM (unless force=True).
    """
    text = read_resume(resume_path)
    resume_hash = hashlib.sha256(text.encode()).hexdigest()
    current = active_profile(conn)
    if (not force and current is not None
            and (current.resume_sha256, current.prompt_version, current.model)
            == (resume_hash, PROMPT_VERSION, model_name())):
        return current, True

    profile = extract_profile(text)
    row = conn.execute(insert(candidate_profiles).values(
        resume_sha256=resume_hash, prompt_version=PROMPT_VERSION, model=model_name(),
        profile=profile.model_dump(mode="json"),
    ).returning(candidate_profiles)).one()
    return row, False

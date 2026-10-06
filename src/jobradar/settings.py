"""Your personal settings, from profile.toml (edited by you, kept in git).

[filters] tunes the cheap deterministic hard filters; [preferences] tells the
AI matcher what the resume can't. A missing file means "use the defaults".
"""

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class SettingsError(Exception):
    """profile.toml is invalid."""


class FilterSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    us_only: bool = True
    # Reject a job whose *minimum* stated experience is above this many years.
    max_years_required: int = Field(default=4, ge=0)
    # Title must contain at least one of these (empty list = no requirement).
    title_must_include: list[str] = [
        "engineer", "engineering", "developer", "software", "swe", "sde", "programmer",
        "member of technical staff",
    ]
    # Title must contain none of these (whole words/phrases, case-insensitive).
    title_must_exclude: list[str] = [
        # seniority
        "senior", "sr", "staff", "principal", "lead", "manager", "director", "head of",
        "vp", "vice president", "architect", "distinguished", "fellow", "iii", "iv",
        # not a new-grad full-time role
        "intern", "internship",
        # not software engineering
        "qa", "quality assurance", "test engineer", "sdet", "sales engineer", "solutions engineer",
        "support engineer", "customer engineer", "field engineer", "mechanical", "electrical",
        "civil", "hardware", "manufacturing", "aerodynamics", "aerostructures", "recruiter",
    ]


class Preferences(BaseModel):
    """What you want, beyond what's on your resume. Given to the AI matcher."""

    model_config = ConfigDict(extra="forbid")

    resume: str = "resume.pdf"  # default for `jobradar profile extract`
    target_roles: list[str] = []
    interests: list[str] = []
    avoid: list[str] = []
    needs_visa_sponsorship: bool | None = None
    notes: str = ""


class NotificationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    immediate_min_score: float = Field(default=80, ge=0, le=100)  # alert right away
    digest_min_score: float = Field(default=70, ge=0, le=100)     # collected into the digest

    @model_validator(mode="after")
    def _ordered(self) -> "NotificationSettings":
        if self.digest_min_score > self.immediate_min_score:
            raise ValueError("digest_min_score must not be above immediate_min_score")
        return self


class NotionSettings(BaseModel):
    """Tracker column names; credentials belong in the environment."""

    model_config = ConfigDict(extra="forbid")
    company_property: str = Field(default="Company", min_length=1)
    role_property: str = Field(default="Role", min_length=1)
    status_property: str = Field(default="Status", min_length=1)
    date_property: str | None = None
    status_map: dict[str, Literal["APPLIED", "REJECTED", "INTERVIEW", "OFFER", "SKIP"]] = {
        "Applied": "APPLIED", "Rejected": "REJECTED", "Interview": "INTERVIEW",
        "Offer": "OFFER", "Not applied": "SKIP", "To apply": "SKIP", "Saved": "SKIP",
    }

    @model_validator(mode="after")
    def _unambiguous_statuses(self) -> "NotionSettings":
        keys = [key.strip().casefold() for key in self.status_map]
        if any(not key for key in keys) or len(keys) != len(set(keys)):
            raise ValueError("Notion status names must be nonblank and unique ignoring case")
        return self


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filters: FilterSettings = FilterSettings()
    preferences: Preferences = Preferences()
    notifications: NotificationSettings = NotificationSettings()
    notion: NotionSettings = NotionSettings()


def load_settings(path: str | Path = "profile.toml") -> Settings:
    try:
        data = tomllib.loads(Path(path).read_text())
    except FileNotFoundError:
        return Settings()
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(f"{path}: invalid TOML: {exc}") from None
    try:
        return Settings.model_validate(data)
    except ValidationError as exc:
        raise SettingsError(f"{path}: {exc}") from None

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


# The default title exclusions before Oct 28, 2026, in their original order.
# filters.run_filters recognises a stored filter signature made with this list:
# adding exclusions can only reject more jobs, so it rechecks just the jobs that
# are not filtered yet instead of running a full recheck. Keep this unchanged.
TITLE_EXCLUDE_BEFORE_20261028 = (
    "senior", "sr", "staff", "principal", "lead", "manager", "director", "head of",
    "vp", "vice president", "architect", "distinguished", "fellow", "iii", "iv",
    "intern", "internship",
    "qa", "quality assurance", "test engineer", "sdet", "sales engineer", "solutions engineer",
    "support engineer", "customer engineer", "field engineer", "mechanical", "electrical",
    "civil", "hardware", "manufacturing", "aerodynamics", "aerostructures", "recruiter",
)

# Added Oct 28, 2026: non-software "engineer" titles that reached AI scoring and
# scored near zero. Checked against production: none of the 586 roles the user
# applied to, and no scored job above 45.5, contains one of these. Deliberately
# absent: "co op" (software co-ops are wanted), "design engineer" (Software Design
# Engineer), "project engineer", "silicon", "production engineer" (an SRE title).
TITLE_EXCLUDE_ADDED_20261028 = (
    "field service", "field applications", "applications engineer",
    "quality engineer", "supplier quality", "quality control",
    "process engineer", "process engineering", "process development",
    "controls engineer", "plant engineer", "plant", "commissioning",
    "utility engineer", "utilities", "validation engineer",
    "post silicon", "asic", "fpga", "rtl", "physical design", "design verification",
    "signal integrity", "rf", "optical", "photonics", "analog", "cqv", "design liaison",
    "construction", "planning engineer", "structural", "chemical", "environmental", "hvac",
    "facilities", "maintenance engineer", "technician", "presales", "pre sales",
    "product support", "talent network", "talent community", "ai trainer", "cad",
    "substation", "power systems",
)


class FilterSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    us_only: bool = True
    max_posting_age_days: int | None = Field(default=None, ge=1)
    # Also fresh: first seen by JobRadar within max_posting_age_days, published
    # by the employer within this many days, and not part of a board's initial
    # scan. None keeps the strict employer-date rule only.
    max_discovery_age_days: int | None = Field(default=None, ge=1)
    require_explicit_sponsorship: bool = False
    # Reject a job whose *minimum* stated experience is above this many years.
    max_years_required: int = Field(default=4, ge=0)
    # Title must contain at least one of these (empty list = no requirement).
    title_must_include: list[str] = [
        "engineer", "engineering", "developer", "software", "swe", "sde", "programmer",
        "member of technical staff",
    ]
    # Title must contain none of these (whole words/phrases, case-insensitive):
    # seniority, internships, and roles that are not software engineering.
    title_must_exclude: list[str] = [*TITLE_EXCLUDE_BEFORE_20261028, *TITLE_EXCLUDE_ADDED_20261028]


    @model_validator(mode="after")
    def _discovery_covers_posting_window(self):
        if (self.max_discovery_age_days is not None and self.max_posting_age_days is not None
                and self.max_discovery_age_days < self.max_posting_age_days):
            raise ValueError("max_discovery_age_days must be at least max_posting_age_days")
        return self


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

    delivery: Literal["alerts", "queue"] = "alerts"

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


class IngestionSettings(BaseModel):
    """What gets stored at all (separate from the alert filters' freshness window).

    A posting that is *new to the database* is not stored when its provider's
    publication date is trustworthy and older than `max_age_days` (plus a one-day
    margin for time zones and date-only feeds). Known jobs, close detection,
    undated and future-dated postings are unaffected. None disables the window.
    """
    model_config = ConfigDict(extra="forbid")

    max_age_days: int | None = Field(default=30, ge=1)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filters: FilterSettings = FilterSettings()
    preferences: Preferences = Preferences()
    notifications: NotificationSettings = NotificationSettings()
    notion: NotionSettings = NotionSettings()
    ingestion: IngestionSettings = IngestionSettings()

    @model_validator(mode="after")
    def _ingestion_covers_alert_window(self):
        window = self.ingestion.max_age_days
        alerts = self.filters.max_discovery_age_days or self.filters.max_posting_age_days
        if window is not None and alerts is not None and window < alerts:
            raise ValueError("[ingestion] max_age_days must be at least [filters] max_posting_age_days "
                             "(and max_discovery_age_days), "
                             "or eligible postings would never be stored")
        return self


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

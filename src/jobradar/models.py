"""The single internal representation of a job posting.

Every collector converts its source-specific payload into this model; nothing
downstream (dedup, filters, matcher, notifier) ever sees source-specific shapes.
"""

from datetime import datetime
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Job(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: str  # "greenhouse", "lever", "ashby", ...
    source_job_id: NonBlank  # the source's own stable ID for the posting
    company: NonBlank
    title: NonBlank
    location: Annotated[str | None, AfterValidator(lambda v: (v or "").strip() or None)]
    url: NonBlank
    description: str  # plain text, HTML already stripped
    posted_at: datetime | None
    # Original payload, persisted so a normalizer bug can be fixed and
    # re-applied without re-fetching from the source.
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)


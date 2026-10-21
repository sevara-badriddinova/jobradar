"""Small SQL projections for metadata embedded in large collector payloads."""

from sqlalchemy import case, func
from sqlalchemy.dialects.postgresql import JSONB

from jobradar.db import jobs


def _object(**fields):
    return func.jsonb_build_object(*[item for pair in fields.items() for item in pair], type_=JSONB)


def location_raw():
    """Preserve every collector's locations() input without HTML/descriptions."""
    raw = jobs.c.raw
    return case({
        "greenhouse": _object(offices=raw["offices"]),
        "lever": _object(categories=_object(allLocations=raw["categories"]["allLocations"]),
                         country=raw["country"]),
        "ashby": _object(address=raw["address"], secondaryLocations=raw["secondaryLocations"]),
        "workday": _object(jobPostingInfo=_object(country=raw["jobPostingInfo"]["country"],
            additionalLocations=raw["jobPostingInfo"]["additionalLocations"],
            jobRequisitionLocation=raw["jobPostingInfo"]["jobRequisitionLocation"])),
        "workable": _object(locations=raw["locations"]),
        "recruitee": _object(locations=raw["locations"], country=raw["country"], country_code=raw["country_code"]),
        "rippling": _object(workLocations=raw["workLocations"],
            publicationMetadata=_object(jobLocation=raw["publicationMetadata"]["jobLocation"])),
        "personio": _object(additionalOffices=raw["additionalOffices"],
            publicationMetadata=_object(jobLocation=raw["publicationMetadata"]["jobLocation"])),
        "amazon": _object(normalized_location=raw["normalized_location"], location=raw["location"],
                          country_code=raw["country_code"], locations=raw["locations"]),
        "oracle": _object(PrimaryLocation=raw["PrimaryLocation"], PrimaryLocationCountry=raw["PrimaryLocationCountry"],
            secondaryLocations=raw["secondaryLocations"],
            detail=_object(PrimaryLocationCountry=raw["detail"]["PrimaryLocationCountry"],
                           secondaryLocations=raw["detail"]["secondaryLocations"],
                           workLocation=raw["detail"]["workLocation"],
                           otherWorkLocations=raw["detail"]["otherWorkLocations"])),
        "eightfold": _object(locations=raw["locations"], standardizedLocations=raw["standardizedLocations"],
            location=raw["location"],
            detail=_object(locations=raw["detail"]["locations"], location=raw["detail"]["location"],
                           standardizedLocations=raw["detail"]["standardizedLocations"])),
    }, value=jobs.c.source, else_=_object()).label("raw")


def display_columns():
    """Queue rendering needs dates and labels, never the posting body."""
    return [jobs.c.id, jobs.c.source, jobs.c.company, jobs.c.title, jobs.c.location,
            jobs.c.url, jobs.c.status, jobs.c.closed_at, jobs.c.match_score,
            jobs.c.posted_at, jobs.c.first_seen_at, jobs.c.discovered_in_backfill,
            _object(first_published=jobs.c.raw["first_published"],
                    updated_at=jobs.c.raw["updated_at"]).label("raw")]

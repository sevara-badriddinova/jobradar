"""Rippling public job board and detail API. Creation time is not publication."""

from urllib.parse import quote

from jobradar.collectors.base import CollectorError, JobCollector, parse_iso_datetime
from jobradar.collectors.public import hydrate, location_strings, posting_metadata, require_records, slug
from jobradar.models import Job
from jobradar.text import html_to_text


class RipplingCollector(JobCollector):
    source = "rippling"

    def collect(self):
        self.complete_snapshot = True
        base = f"https://api.rippling.com/platform/api/ats/v1/board/{slug(self.board)}/jobs"
        records = require_records(self.fetch_json(base))

        def detail(item):
            job_id = slug(item["uuid"])
            raw = self.fetch_json(base + "/" + quote(job_id, safe=""))
            if not isinstance(raw, dict) or raw.get("uuid") != job_id:
                raise CollectorError("invalid Rippling detail identity")
            metadata = posting_metadata(raw.get("jsonLd"))
            if not parse_iso_datetime(metadata.get("datePosted")):
                # Public HTML may supply JobPosting metadata even when API jsonLd
                # is null. Missing metadata stays undated (createdOn is not used).
                page = self.fetch_data(f"https://ats.rippling.com/{self.board}/jobs/{job_id}", as_text=True)
                fallback = posting_metadata(page)
                if parse_iso_datetime(fallback.get("datePosted")):
                    metadata = fallback
            return {**raw, "publicationMetadata": metadata}
        rows = hydrate(self, records, detail)
        return self.normalize_all([r for r in rows if not r.get("unlistedFromSearch", False)])

    def normalize(self, raw):
        description = raw.get("description") or {}
        if isinstance(description, dict):
            description = "\n\n".join(v for v in description.values() if isinstance(v, str))
        return Job(source=self.source, source_job_id=raw["uuid"], company=self.company or self.board,
                   title=raw["name"], location=", ".join(location_strings(raw.get("workLocations"))),
                   url=f"https://ats.rippling.com/{self.board}/jobs/{raw['uuid']}",
                   description=html_to_text(description),
                   posted_at=parse_iso_datetime(raw.get("publicationMetadata", {}).get("datePosted")), raw=raw)

    @staticmethod
    def locations(raw):
        return location_strings([raw.get("workLocations"), raw.get("publicationMetadata", {}).get("jobLocation")])

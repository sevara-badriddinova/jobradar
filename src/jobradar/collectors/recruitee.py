"""Recruitee public Careers Site API: all published offers in one response."""

from jobradar.collectors.base import JobCollector, parse_iso_datetime
from jobradar.collectors.public import location_strings, require_records, slug
from jobradar.models import Job
from jobradar.text import html_to_text


class RecruiteeCollector(JobCollector):
    source = "recruitee"

    def collect(self):
        self.complete_snapshot = True
        payload = self.fetch_json(f"https://{slug(self.board)}.recruitee.com/api/offers/")
        records = require_records(payload, "offers")
        return self.normalize_all([r for r in records if r.get("status", "published") == "published"])

    def normalize(self, raw):
        published = raw.get("published_at")
        if isinstance(published, str):
            published = published.removesuffix(" UTC") + ("+00:00" if published.endswith(" UTC") else "")
        return Job(source=self.source, source_job_id=str(raw["id"]), company=self.company or self.board,
                   title=raw["title"], location=raw.get("location"), url=raw["careers_url"],
                   description="\n\n".join(filter(None, [html_to_text(raw.get("description")),
                                                          html_to_text(raw.get("requirements"))])),
                   posted_at=parse_iso_datetime(published), raw=raw)

    @staticmethod
    def locations(raw):
        return location_strings([raw.get("locations"), raw.get("country"), raw.get("country_code")])

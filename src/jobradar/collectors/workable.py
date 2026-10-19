"""Workable public widget feed, with complete posting descriptions."""

from jobradar.collectors.base import JobCollector, parse_iso_datetime
from jobradar.collectors.public import location_strings, require_records, slug
from jobradar.models import Job
from jobradar.text import html_to_text


class WorkableCollector(JobCollector):
    source = "workable"

    def collect(self):
        self.complete_snapshot = True
        payload = self.fetch_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug(self.board)}",
                                  {"details": "true"})
        return self.normalize_all(require_records(payload, "jobs"))

    def normalize(self, raw):
        return Job(source=self.source, source_job_id=raw["shortcode"], company=self.company or self.board,
                   title=raw["title"], location=", ".join(location_strings(
                       [raw.get("city"), raw.get("state"), raw.get("country")])), url=raw["url"],
                   description=html_to_text(raw.get("description")),
                   posted_at=parse_iso_datetime(raw.get("published_on")), raw=raw)

    @staticmethod
    def locations(raw):
        return location_strings(raw.get("locations"))

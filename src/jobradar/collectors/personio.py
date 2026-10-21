"""Personio's public English XML feed plus JobPosting publication metadata."""

import random
import xml.etree.ElementTree as ET

from jobradar.collectors.base import CollectorError, JobCollector, parse_iso_datetime
from jobradar.collectors.public import hydrate, location_strings, posting_metadata, slug
from jobradar.models import Job
from jobradar.text import html_to_text


def xml_record(node):
    if not list(node):
        return (node.text or "").strip()
    result = {}
    for child in node:
        value = xml_record(child)
        if child.tag in result:
            previous = result[child.tag]
            result[child.tag] = (previous if isinstance(previous, list) else [previous]) + [value]
        else:
            result[child.tag] = value
    return result


def descriptions(raw):
    sections = (raw.get("jobDescriptions") or {}).get("jobDescription", [])
    if isinstance(sections, dict):
        sections = [sections]
    return "\n\n".join(html_to_text(s.get("name", "") + "\n" + s.get("value", "")) for s in sections)


REVERIFY = 12  # with 30-60 min revisits, each stored date is re-checked every ~6-12 h


class PersonioCollector(JobCollector):
    source = "personio"
    reuses_known_details = "publicationMetadata"
    # The feed carries title and description; details only date a posting.
    # Rejected titles need no date. (No early digest: the random re-verify
    # below is how a republication under the same ID is noticed.)
    title_first = True

    def host(self):
        # Default is .de; .com is a distinct region, never silently substituted.
        region, board = ("com", self.board[4:]) if self.board.startswith("com:") else ("de", self.board)
        return f"https://{slug(board)}.jobs.personio.{region}"

    def collect(self):
        self.complete_snapshot = True
        body = self.fetch_data(self.host() + "/xml", params={"language": "en"}, as_text=True)
        try:
            if "<!DOCTYPE" in body.upper() or "<!ENTITY" in body.upper():
                raise ValueError("XML declarations are not permitted")
            root = ET.fromstring(body)
            if root.tag != "workzag-jobs" or any(n.tag != "position" for n in root):
                raise ValueError("unexpected Personio XML root or entry")
            records = [xml_record(n) for n in root]
        except (ET.ParseError, ValueError) as exc:
            raise CollectorError("invalid Personio feed") from exc

        def detail(raw):
            job_id = raw["id"]
            if not job_id.isdigit():
                raise CollectorError("invalid Personio job id")
            # The XML feed carries title and description; the detail page only
            # dates the posting. Reuse a stored date instead of 1-2 requests per
            # job per poll, but re-verify a random 1/REVERIFY share each poll so a
            # republication under the same ID is still picked up within hours.
            stored = self.known.get(self.board + ":" + job_id, {}).get("publicationMetadata") or {}
            if not self.needs_detail(raw.get("name")):
                return {**raw, "publicationMetadata": stored} if stored else raw
            if parse_iso_datetime(stored.get("datePosted")) and random.random() >= 1 / REVERIFY:
                return {**raw, "publicationMetadata": stored}
            url = self.host() + "/job/" + job_id
            try:
                page = self.fetch_data(url, params={"language": "en"}, as_text=True)
                metadata = posting_metadata(page)
            except CollectorError:
                # Some boards have no English detail page even though their
                # English XML feed lists the job. The original locale can date it.
                metadata = {}
            if not parse_iso_datetime(metadata.get("datePosted")):
                fallback = posting_metadata(self.fetch_data(url, as_text=True))
                if parse_iso_datetime(fallback.get("datePosted")):
                    metadata = fallback
            return {**raw, "publicationMetadata": metadata}
        return self.normalize_all(hydrate(self, records, detail))

    def normalize(self, raw):
        return Job(source=self.source, source_job_id=self.board + ":" + raw["id"],
                   company=self.company or self.board, title=raw["name"], location=raw.get("office"),
                   url=self.host() + "/job/" + raw["id"], description=descriptions(raw),
                   posted_at=parse_iso_datetime(raw.get("publicationMetadata", {}).get("datePosted")), raw=raw)

    @staticmethod
    def locations(raw):
        return location_strings([(raw.get("additionalOffices") or {}).get("office"),
                                 raw.get("publicationMetadata", {}).get("jobLocation")])

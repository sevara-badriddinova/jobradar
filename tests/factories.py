"""Test data builders shared by unit and integration tests."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from jobradar.models import Job

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def make_job(sid: str = "1", *, company: str = "Acme",
             title: str = "Software Engineer, New Grad", location: str | None = "New York, NY",
             url: str | None = None, description: str = "Build backend services.",
             source: str = "greenhouse", posted_at: datetime | None = None) -> Job:
    return Job(
        source=source, source_job_id=sid, company=company, title=title, location=location,
        url=url or f"https://boards.greenhouse.io/acme/jobs/{sid}",
        description=description,
        posted_at=posted_at or datetime(2026, 10, 1, tzinfo=timezone.utc),
        raw={"id": sid, "title": title},
    )


def greenhouse_job(job_id: int, **overrides: Any) -> dict[str, Any]:
    """A raw Greenhouse API job object, shaped like the real response."""
    raw = {
        "id": job_id,
        "internal_job_id": job_id + 1000,
        "title": f"Software Engineer {job_id}",
        "company_name": "Acme",
        "location": {"name": "New York, NY"},
        "absolute_url": f"https://acme.com/careers?gh_jid={job_id}",
        "updated_at": "2026-10-01T12:00:00-04:00",
        "first_published": "2026-09-30T09:00:00-04:00",
        "requisition_id": f"R-{job_id}",
        "content": "&lt;p&gt;Build things.&lt;/p&gt;",
    }
    raw.update(overrides)
    return raw


def lever_posting(posting_id: str, **overrides: Any) -> dict[str, Any]:
    """A raw Lever postings-API object, shaped like the real response."""
    raw = {
        "id": posting_id,
        "text": f"Software Engineer {posting_id}",
        "hostedUrl": f"https://jobs.lever.co/acme/{posting_id}",
        "applyUrl": f"https://jobs.lever.co/acme/{posting_id}/apply",
        "createdAt": 1790000000000,  # 2026-09-21T14:13:20Z
        "categories": {"commitment": "Full-time", "location": "New York, NY", "team": "Engineering",
                       "allLocations": ["New York, NY"]},
        "workplaceType": "hybrid",
        "country": "US",
        "description": "<div>Build things.</div>",
        "descriptionPlain": "Build things.",
        "lists": [],
        "additional": "",
        "additionalPlain": "",
    }
    raw.update(overrides)
    return raw


def ashby_job(job_id: str, **overrides: Any) -> dict[str, Any]:
    """A raw Ashby posting-API job object, shaped like the real response."""
    raw = {
        "id": job_id,
        "title": f"Software Engineer {job_id}",
        "department": "Engineering",
        "team": "Backend",
        "employmentType": "FullTime",
        "location": "New York, NY",
        "secondaryLocations": [],
        "publishedAt": "2026-09-30T13:00:00.000+00:00",
        "isListed": True,
        "isRemote": False,
        "workplaceType": "Hybrid",
        "address": {"postalAddress": {"addressLocality": "New York", "addressRegion": "NY",
                                      "addressCountry": "USA"}},
        "jobUrl": f"https://jobs.ashbyhq.com/acme/{job_id}",
        "applyUrl": f"https://jobs.ashbyhq.com/acme/{job_id}/application",
        "descriptionHtml": "<p>Build things.</p>",
        "descriptionPlain": "Build things.",
    }
    raw.update(overrides)
    return raw


def candidate_profile(**overrides: Any):
    """A realistic CandidateProfile, as the LLM would return it."""
    from jobradar.candidate import CandidateProfile
    data = {
        "education": [{"school": "State University", "degree": "B.S.", "field": "Computer Science",
                       "graduation": "2026-05"}],
        "graduation": "2026-05",
        "experience_level": "new_grad",
        "years_of_experience": 0,
        "languages": ["Java", "Python", "Go"],
        "frameworks": ["Spring Boot"],
        "databases": ["PostgreSQL", "Redis"],
        "cloud_devops": ["AWS", "Kubernetes", "Docker"],
        "work_experience": [{"company": "Acme Corp", "title": "Software Engineering Intern",
                             "kind": "internship", "start": "2025-06", "end": "2025-08",
                             "summary": "Built a Java/Spring Boot service on PostgreSQL.",
                             "technologies": ["Java", "Spring Boot", "PostgreSQL"]}],
        "projects": [{"name": "Distributed KV store", "summary": "Raft-replicated key-value store.",
                      "technologies": ["Go", "Raft", "Docker"]}],
        "domains": ["distributed systems", "backend"],
        "target_roles": ["Backend Engineer", "Software Engineer"],
    }
    return CandidateProfile.model_validate(data | overrides)


def assessment(**overrides: Any):
    """A MatchAssessment, as the LLM would return it."""
    from jobradar.matcher import MatchAssessment
    data = {"eligible": True, "ineligible_reason": None, "role_match": 8, "technical_match": 7,
            "experience_match": 9, "graduation_match": True, "missing_skills": ["Kafka"],
            "reasons": ["New-grad role", "Java/Spring match"], "concerns": []}
    return MatchAssessment.model_validate(data | overrides)


class FakeLLM:
    """Stands in for jobradar.llm.ask: records every call, replays answers.

    `answers` items are model instances, exceptions (raised), or callables
    (output_type, prompt) -> instance. The last item repeats.
    """

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, output: type, *, system: str, prompt: str, model: str, max_tokens: int = 4096):
        self.calls.append({"output": output, "system": system, "prompt": prompt, "model": model,
                           "max_tokens": max_tokens})
        answer = self.answers[min(len(self.calls), len(self.answers)) - 1]
        if isinstance(answer, BaseException):
            raise answer
        return answer(output, prompt) if callable(answer) and not hasattr(answer, "model_dump") else answer


class RecordingTransport(httpx.MockTransport):
    """MockTransport that replays a sequence of responses and records requests.

    Each item is an httpx.Response, an exception instance (raised), or a
    callable(request) -> Response. The last item repeats once exhausted.
    """

    def __init__(self, *responses: Any) -> None:
        self.requests: list[httpx.Request] = []
        self._responses = list(responses)
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        idx = min(len(self.requests) - 1, len(self._responses) - 1)
        item = self._responses[idx]
        if isinstance(item, Exception):
            raise item
        if callable(item) and not isinstance(item, httpx.Response):
            return item(request)
        return item

    def client(self) -> httpx.Client:
        return httpx.Client(transport=self)

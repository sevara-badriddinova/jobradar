"""Expand discovery from public board addresses, without inferring sponsorship.

The directory is address discovery only. Every job is fetched from the ATS and
must pass the role-specific sponsorship policy before scoring or notification.
Unsupported platforms are reported, never counted as monitored employers.
"""

import csv
import hashlib
import io
import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from jobradar.companies import CompanyFileError, CompanySpec, load_company_file
from jobradar.collectors.base import CollectorError
from jobradar.collectors.workday import encode_board

DIRECTORY_URL = "https://kalebconfer-sys.github.io/job-board-directory/job-board-directory.csv"
HOSTS = {"greenhouse": {"job-boards.greenhouse.io", "boards.greenhouse.io"},
         "ashby": {"jobs.ashbyhq.com"}, "lever": {"jobs.lever.co", "jobs.eu.lever.co"},
         "workable": {"apply.workable.com"}, "rippling": {"ats.rippling.com"}}
SUPPORTED = set(HOSTS) | {"workday", "recruitee", "personio"}


def directory_board(platform: str, token: str, address: str) -> str:
    """Validate vendor hosts and token/path agreement; never trust CSV api_url."""
    url = urlparse(address)
    if (url.scheme != "https" or url.username or url.password or url.port
            or url.query or url.fragment):
        raise ValueError("invalid board address")
    if platform == "workday":
        shard, tenant, site = token.split("/")
        if (url.hostname != f"{tenant.lower()}.{shard.lower()}.myworkdayjobs.com"
                or url.path.strip("/") != site):
            raise ValueError("Workday address mismatch")
        return encode_board(shard.lower(), tenant.lower(), site)
    token = token.lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", token):
        raise ValueError("invalid board slug")
    if platform == "recruitee":
        valid = url.hostname == f"{token}.recruitee.com" and url.path in ("", "/")
    elif platform == "personio":
        valid = url.hostname in {f"{token}.jobs.personio.de", f"{token}.jobs.personio.com"} and url.path in ("", "/")
    else:
        valid = url.hostname in HOSTS[platform] and url.path.strip("/").lower() == token
    if not valid:
        raise ValueError("board address mismatch")
    if url.hostname == "jobs.eu.lever.co":
        return "eu:" + token
    if platform == "personio" and url.hostname.endswith(".com"):
        return "com:" + token
    return token


def parse_directory(body: str, existing: list[CompanySpec]) -> tuple[list[CompanySpec], dict]:
    reader = csv.DictReader(io.StringIO(body))
    if not {"platform", "token", "company", "board_url"}.issubset(reader.fieldnames or []):
        raise CompanyFileError("directory is missing required columns")
    merged = {(s.collector, s.board): s for s in existing}
    supported, unsupported, invalid = Counter(), Counter(), 0
    for row in reader:
        platform, token = row["platform"].strip().lower(), row["token"].strip()
        if platform not in SUPPORTED:
            unsupported[platform] += 1
            continue
        try:
            board = directory_board(platform, token, row["board_url"])
        except (ValueError, CollectorError):
            invalid += 1
            continue
        name = row["company"].strip() or token
        spec = CompanySpec(name=name, collector=platform, board=board, priority=3)
        merged.setdefault((platform, board), spec)  # retain manual names/priorities/disabled state
        supported[platform] += 1
    if not supported:
        raise CompanyFileError("directory contained no usable board addresses; registry unchanged")
    report = {"source": DIRECTORY_URL, "collector_platforms": sorted(SUPPORTED),
              "retrieved_at": datetime.now(UTC).isoformat(),
              "sha256": hashlib.sha256(body.encode()).hexdigest(),
              "supported_directory_boards": dict(supported), "unsupported_directory_boards": dict(unsupported),
              "invalid_rows": invalid, "registered_boards": len(merged),
              "sponsorship": "Not inferred. Each posting must explicitly offer support.",
              "coverage": "Public directory coverage; not a complete list of all employers."}
    return sorted(merged.values(), key=lambda s: (s.priority, s.name.casefold(), s.collector, s.board)), report


def refresh_directory(client, path="companies.toml", *, force=False) -> dict:
    path = Path(path)
    report_path = path.with_suffix(".coverage.json")
    if report_path.exists() and not force:
        report = json.loads(report_path.read_text())
        if (report.get("collector_platforms") == sorted(SUPPORTED)
                and (datetime.now(UTC) - datetime.fromisoformat(report["retrieved_at"])).total_seconds() < 86400):
            return report
    response = client.get(DIRECTORY_URL, timeout=45)
    response.raise_for_status()
    specs, report = parse_directory(response.text, load_company_file(path))
    text = "# Public board discovery; sponsorship is verified per posting, not per employer.\n"
    for spec in specs:
        text += "\n[[company]]\n" + "\n".join(
            f"{key} = {json.dumps(value)}" for key, value in spec.model_dump().items()) + "\n"
    temp = path.with_suffix(".toml.tmp")
    temp.write_text(text)
    temp.replace(path)
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    return report

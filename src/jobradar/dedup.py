"""Deterministic identity keys used for deduplication.

Level 1: (source, source_job_id)          -> enforced by a UNIQUE constraint
Level 2: canonical_url(url)               -> same posting reached via another source/ID
Level 3: fingerprint(company, title, loc) -> same role re-listed under a new ID
Level 4: semantic similarity              -> later step, only for ambiguous cases

All functions here are pure, so they're cheap, testable and reproducible.
"""

import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query params that only describe *how you got to* a page, not *which* page.
# Identifying params such as Greenhouse's gh_jid are deliberately kept.
_TRACKING_PARAMS = {
    "gh_src", "source", "src", "ref", "referrer", "lever-source", "lever-origin",
    "gclid", "fbclid", "mc_cid", "mc_eid", "trk", "trackingid",
}  # plus every utm_* parameter
_LEGAL_SUFFIXES = {"inc", "llc", "ltd", "corp", "corporation", "co", "technologies"}


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    if scheme == "http":
        scheme = "https"
    host = (parts.hostname or "").removeprefix("www.")
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/") or "/"
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if k.lower() not in _TRACKING_PARAMS and not k.lower().startswith("utm_")
    )
    return urlunsplit((scheme, host, path, urlencode(query), ""))


def normalize_key(value: str | None) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace."""
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = re.sub(r"[^a-z0-9]+", " ", value.lower())
    return value.strip()


def company_key(company: str) -> str:
    return " ".join(w for w in normalize_key(company).split() if w not in _LEGAL_SUFFIXES)


def fingerprint(company: str, title: str, location: str | None) -> str:
    material = "|".join((company_key(company), normalize_key(title), normalize_key(location)))
    return hashlib.sha256(material.encode()).hexdigest()


def description_hash(description: str) -> str:
    return hashlib.sha256(" ".join(description.split()).lower().encode()).hexdigest()

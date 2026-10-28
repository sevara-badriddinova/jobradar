"""Hard filters: cheap, deterministic rules that run before any AI.

Ambiguous role requirements ("Remote", no stated experience) pass through.
An enabled posting-age cutoff requires a known publication date.
"""

import logging
import re
import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, case, func, not_, or_, select, text, true, update
from sqlalchemy.engine import Connection
from sqlalchemy.dialects.postgresql import insert

from jobradar.collectors import COLLECTORS
from jobradar.db import jobs, app_state
from jobradar import geo
from jobradar.dedup import normalize_key
from jobradar.queue import drop_tasks
from jobradar.settings import TITLE_EXCLUDE_BEFORE_20261028, FilterSettings
from jobradar.recency import discovered_recently, publication_reason, recent_jobs
from jobradar.query_fields import location_raw
from jobradar.sponsorship_review import UNKNOWN_REASON, hold, kept_fingerprints, release_held, resolve

log = logging.getLogger(__name__)

# Reason for a pruned job (content emptied by prune.py) that the rules reach again.
PRUNED_REASON = "content pruned: posting text no longer stored"

# --- title ---------------------------------------------------------------------

# Phrases that contain an excluded word but aren't what the word means
# ("Member of Technical Staff" is not a Staff-level role).
_NEUTRAL_PHRASES = ("member of technical staff",)


def _has_phrase(text: str, phrase: str) -> bool:
    return f" {normalize_key(phrase)} " in f" {text} "


def title_reason(title: str, settings: FilterSettings) -> str | None:
    text = normalize_key(title)
    if settings.title_must_include and not any(_has_phrase(text, p) for p in settings.title_must_include):
        return "title: not a software engineering role"
    for phrase in _NEUTRAL_PHRASES:
        text = f" {text} ".replace(f" {phrase} ", " ").strip()
    excluded = next((p for p in settings.title_must_exclude if _has_phrase(text, p)), None)
    return f"title: {excluded}" if excluded else None


# --- years of experience --------------------------------------------------------

_NUMBER_WORDS = {w: n for n, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve".split())}
_NUM = r"(\d{1,2}|" + "|".join(_NUMBER_WORDS) + r")"
# "5+ years", "5 or more years", "3-5 years", "at least five years" ... followed,
# within the same sentence, by "experience".
_YEARS = re.compile(
    _NUM + r"\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*" + _NUM + r"\s*\+?\s*)?(?:or more\s+)?(?:years?|yrs?)\b"
    r"[^.\n;]{0,60}?\bexperience",
    re.IGNORECASE,
)
_OPTIONAL = re.compile(r"\b(prefer|preferred|preferably|nice to have|bonus|ideally|a plus)\b", re.IGNORECASE)


def required_years(description: str) -> int | None:
    """Smallest required years of experience stated, ignoring "preferred" ones.

    The smallest wins on purpose: "3+ years required, 5+ preferred" is a 3.
    """
    found = []
    for sentence in re.split(r"(?<=[.;\n])", description):
        if _OPTIONAL.search(sentence):
            continue
        for match in _YEARS.finditer(sentence):
            value = match.group(1).lower()
            found.append(int(value) if value.isdigit() else _NUMBER_WORDS[value])
    return min(found, default=None)


# --- visa sponsorship ---------------------------------------------------------------

# Phrases that rule out a candidate who needs H-1B (or any) sponsorship. Each
# needs an explicit refusal or requirement; "visa sponsorship is available" and
# EEO boilerplate ("without regard to ... citizenship status") don't match.
_SPONSOR = r"(?:visa |h-?1b |employment |immigration |work )?sponsor(?:ship|ing)?"
_NO_SPONSORSHIP = re.compile("|".join([
    r"\b(?:unable|not able|cannot|can ?not|can't|won't|will not|do not|does not|don't|doesn't|"
    r"are not|is not|not)\s+(?:be\s+)?(?:able\s+to\s+|in a position to\s+)?"
    r"(?:provide|offer|support|sponsor)(?:ing)?\s+(?:any\s+|an?\s+)?" + _SPONSOR,
    r"\b(?:unable|not able|cannot|can ?not|can't|won't|will not|do not|does not|don't|not in a position)"
    r"\s+(?:to\s+)?sponsor\b",
    r"\bwithout\s+(?:the\s+)?(?:need\s+for\s+|requiring\s+|requirement\s+of\s+)?(?:current\s+or\s+future\s+|"
    r"present\s+or\s+future\s+|now\s+or\s+in\s+the\s+future\s+)?(?:employer\s+)?" + _SPONSOR,
    r"\bnot\s+(?:be\s+)?eligible\s+for\s+" + _SPONSOR,
    r"\b" + _SPONSOR + r"\s+(?:is\s+|will\s+)?not\s+(?:be\s+)?(?:available|offered|provided|possible|supported)",
    r"\bno\s+" + _SPONSOR + r"\b",
    r"\b" + _SPONSOR + r"\s*:\s*(?:no|not available|none)\b",
    r"\b(?:u\.?\s?s\.?|united states)\s+citizen(?:ship)?\s+(?:is\s+|are\s+)?(?:required|only|mandatory)",
    r"\bmust\s+be\s+(?:a\s+)?(?:u\.?\s?s\.?|united states)\s+(?:citizen|person)",
    r"\b(?:u\.?\s?s\.?|united states)\s+citizens\s+only",
    r"\b(?:security|secret|top secret|ts/sci|ts)\s+clearance\s+(?:is\s+)?(?:required|needed|mandatory)",
    r"\b(?:must|required to|need to|will need to)\s+(?:have|hold|possess|obtain|be able to obtain|maintain)\s+"
    r"(?:an?\s+)?(?:active\s+|current\s+)?(?:u\.?s\.?\s+)?(?:government\s+)?(?:security|secret|top secret|ts/sci)\s+clearance",
    r"\bitar\b[^.\n]{0,80}\bu\.?s\.?\s+persons?\b|\bu\.?s\.?\s+persons?\b[^.\n]{0,80}\bitar\b",
]), re.IGNORECASE)


def sponsorship_reason(text: str) -> str | None:
    """Why a candidate who needs sponsorship can't take this job, or None."""
    for match in _NO_SPONSORSHIP.finditer(text):
        # Export-license sponsorship is not employment/visa sponsorship. Scope
        # this exception to the object immediately following this match, never
        # to the paragraph: a separate visa refusal must still win.
        tail = text[match.end():]
        if (re.search(r"\bsponsor(?:ship|ing)?$", match.group(0), re.I)
                and re.match(r"\s+(?:for\s+)?(?:an?\s+)?export[- ]licen[cs]e\b", tail, re.I)
                and not re.search(r"\b(?:visa|h-?1b|immigration|employment|work)\b", match.group(0), re.I)):
            continue
        return f"visa: \"{' '.join(match.group(0).split())}\""
    return None


def sponsorship_evidence(text: str) -> str | None:
    """Return affirmative, role-local support wording, never employer history.

    Conservative by design: questions, hypothetical availability, historical
    claims, E-Verify alone, and negative/transfer-only statements do not qualify.
    Explicit STEM OPT support qualifies separately from an H-1B promise.
    """
    flat = " ".join(text.split())
    # Do not take the positive opening sentence of a conditional sponsorship
    # paragraph in isolation (a common careers-page boilerplate pattern).
    if re.search(r"\b(?:not|cannot|can't|aren't|aren’t|unable)\b[^.!?]{0,100}"
                 r"\bsponsor(?:ship)?\b[^.!?]{0,70}\b(?:every|all|this)\s+(?:role|position|candidate)", flat, re.I):
        return None
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        sentence = " ".join(sentence.split())
        if not sentence or "?" in sentence or sponsorship_reason(sentence):
            continue
        if re.search(r"\b(not|no|cannot|can't|unable|unavailable|historically|previously|may|might)\b|"
                     r"\b(?:transfer|transfers|existing visa holders)\s+only\b|"
                     r"\b(?:some|select|other|certain)\s+(?:roles|positions)\b|case.by.case|subject to|depending on",
                     sentence, re.I):
            continue
        offer = (r"\b(?:we|the company|our company)\s+(?:will\s+|can\s+|do\s+)?"
                 r"(?:offer|provide|support)\s+(?:employment[- ]based\s+|work\s+|immigration\s+|visa\s+|h-?1b\s+)+sponsorship\b"
                 r"|\b(?:visa|h-?1b|immigration|employment)\s+sponsorship\s*:?\s*(?:is\s+)?(?:available|provided|offered|supported)\b"
                 r"|\bthis\s+(?:role|position)\s+is\s+eligible\s+for\s+(?:h-?1b\s+)?visa\s+sponsorship\b"
                 r"|\b(?:we\s+)(?:will\s+|can\s+|do\s+)?sponsor\s+(?:work\s+visas|visas|h-?1b|qualified\s+candidates|eligible\s+candidates)\b"
                 r"|\b(?:offer|provide|support|welcome|accept)\s+(?:candidates\s+(?:on|with)\s+)?stem[ -]opt\b"
                 r"|\bstem[ -]opt\s+(?:candidates\s+)?(?:are\s+)?(?:welcome|supported|accepted|eligible)\b")
        if re.search(offer, sentence, re.I):
            return sentence
    return None


# --- location -------------------------------------------------------------------

_US_STATES = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas", "CA": "california",
    "CO": "colorado", "CT": "connecticut", "DE": "delaware", "FL": "florida", "GA": "georgia",
    "HI": "hawaii", "ID": "idaho", "IL": "illinois", "IN": "indiana", "IA": "iowa",
    "KS": "kansas", "KY": "kentucky", "LA": "louisiana", "ME": "maine", "MD": "maryland",
    "MA": "massachusetts", "MI": "michigan", "MN": "minnesota", "MS": "mississippi",
    "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada", "NH": "new hampshire",
    "NJ": "new jersey", "NM": "new mexico", "NY": "new york", "NC": "north carolina",
    "ND": "north dakota", "OH": "ohio", "OK": "oklahoma", "OR": "oregon", "PA": "pennsylvania",
    "RI": "rhode island", "SC": "south carolina", "SD": "south dakota", "TN": "tennessee",
    "TX": "texas", "UT": "utah", "VT": "vermont", "VA": "virginia", "WA": "washington",
    "WV": "west virginia", "WI": "wisconsin", "WY": "wyoming", "DC": "district of columbia",
}
_US_WORDS = {"us", "usa", "u s", "united states", "united states of america", "america",
             "san francisco", "bay area", "silicon valley", "nyc", "seattle", "boston", "austin",
             "chicago", "los angeles", "denver", "atlanta", "miami", "palo alto", "mountain view",
             "menlo park", "sunnyvale", "san jose", "redmond", "brooklyn", "philadelphia",
             "pittsburgh", "san diego", "portland", "dallas", "houston", "salt lake city",
             *_US_STATES.values()}
_NON_US_WORDS = {
    "canada", "united kingdom", "uk", "england", "scotland", "ireland", "germany", "france",
    "netherlands", "spain", "portugal", "italy", "switzerland", "sweden", "denmark", "norway",
    "finland", "poland", "austria", "belgium", "czechia", "romania", "israel", "india",
    "singapore", "japan", "korea", "south korea", "china", "hong kong", "taiwan", "australia",
    "new zealand", "brazil", "mexico", "argentina", "colombia", "chile", "uae",
    "united arab emirates", "saudi arabia", "vietnam", "philippines", "indonesia", "malaysia",
    "thailand", "nigeria", "kenya", "south africa", "egypt", "turkey", "emea", "apac", "latam",
    "europe", "london", "dublin", "toronto", "vancouver", "montreal", "ottawa", "berlin",
    "munich", "paris", "amsterdam", "madrid", "barcelona", "lisbon", "stockholm", "copenhagen",
    "zurich", "warsaw", "tel aviv", "bangalore", "bengaluru", "hyderabad", "pune", "mumbai",
    "delhi", "tokyo", "seoul", "sydney", "melbourne", "sao paulo", "mexico city", "dubai",
    "ho chi minh", "beijing", "shanghai", "manila", "jakarta",
}
_US_STATE_CODE = re.compile(r",\s*(" + "|".join(_US_STATES) + r")\b")  # "Austin, TX"
_CA_PROVINCE_CODE = re.compile(r",\s*(ON|BC|QC|AB|MB|NS|NB|SK|NL|PE)\b")  # "Toronto, ON"


_US_WORDS.add("new england")      # the US region, not England
# Regions containing "america" that are not the US ("Remote - Latin America").
_NOT_US_AMERICA = re.compile(r"\b(?:latin|south|central) america\b")
_NON_US_WORDS |= {"latin america", "south america", "central america"} | geo.COUNTRIES | geo.CITIES
_NON_US_PATTERN = re.compile(r"(?:^| )(?:" + "|".join(sorted(map(re.escape, _NON_US_WORDS), key=len, reverse=True))
                             + r")(?: |$)")
# Workday's structured prefix: "DEU - Saxony - Dresden", "IND - HR - NOIDA", "PHL Quezon City".
_ISO3_PREFIX = re.compile(r"([A-Z]{3})(?:\s*[-,]\s*\S|\s+[A-Z]|\s*$)")
# Labels that may include the US, so they never prove a job is abroad.
_GENERIC_PLACE = re.compile(r"\b(?:remote|anywhere|hybrid|flexible|virtual|distributed|various|multiple|"
                            r"locations?|wfh|work from home|home based|homebased|global|worldwide)\b")


def _iso3(location: str) -> str | None:
    match = _ISO3_PREFIX.match(location.strip())
    return match[1] if match and match[1] in geo.ISO3 else None


def _place(location: str) -> str:
    """'us', 'non-us' or 'unknown' for one location string. Any US signal wins."""
    if re.fullmatch(r"[A-Z]{2}", location.strip()):  # ISO-2 country code (Lever)
        return "us" if location.strip() == "US" else "non-us"
    text = normalize_key(location)
    us_text = _NOT_US_AMERICA.sub(" ", text)
    if _US_STATE_CODE.search(location) or any(_has_phrase(us_text, w) for w in _US_WORDS):
        return "us"
    if code := _iso3(location):
        return "us" if code in geo.US_ISO3 else "non-us"
    if _CA_PROVINCE_CODE.search(location) or _NON_US_PATTERN.search(text):
        return "non-us"
    return "unknown"


def _is_country(location: str) -> bool:
    """A country-level field (name, ISO-2 or ISO-3 code), not just a city."""
    stripped = location.strip()
    return (bool(re.fullmatch(r"[A-Z]{2}", stripped)) or _iso3(stripped) is not None
            or normalize_key(stripped) in geo.COUNTRIES)


def location_reason(locations: list[str]) -> str | None:
    """Reject only when the job is clearly outside the US.

    Any US location passes, and so does a generic label ("Remote", "Hybrid",
    "2 Locations") that may include the US. Otherwise every location being
    outside the US rejects, and so does a non-US *country* field next to place
    names we cannot classify: Workday's "DEU - Saxony - Dresden" with country
    "Germany", Workable's "Ha Noi" with "Vietnam"/"VN". A city we don't know next
    to a foreign *city* ("Raleigh" + "London") still passes.
    """
    places = [(loc, _place(loc)) for loc in locations]
    if not places or any(p == "us" for _, p in places):
        return None
    abroad = [loc for loc, p in places if p == "non-us"]
    unknown = [loc for loc, p in places if p == "unknown"]
    if not abroad or any(_GENERIC_PLACE.search(normalize_key(loc)) for loc in unknown):
        return None
    if unknown and not any(_is_country(loc) for loc in abroad):
        return None
    return f"location: {locations[0]}"


# --- putting it together ----------------------------------------------------------

def reject_reason(*, source: str, title: str, location: str | None, description: str,
                  raw: dict, settings: FilterSettings, needs_sponsorship: bool = False,
                  posted_at=None, first_seen_at=None, backfill: bool = False) -> str | None:
    """Why this job should be rejected, or None if it passes every hard filter."""
    if reason := publication_reason(posted_at, settings.max_posting_age_days, first_seen_at=first_seen_at,
                                    backfill=backfill, discovery_days=settings.max_discovery_age_days):
        return reason
    if reason := title_reason(title, settings):
        return reason
    years = required_years(description)
    if years is not None and years > settings.max_years_required:
        return f"experience: requires {years}+ years"
    if needs_sponsorship and (reason := sponsorship_reason(description)):
        return reason                                   # explicit refusal: rejected
    if settings.us_only:
        extra = COLLECTORS[source].locations(raw) if source in COLLECTORS else []
        if reason := location_reason([loc for loc in [location, *extra] if loc]):
            return reason
    if settings.require_explicit_sponsorship and not sponsorship_evidence(description):
        # Neither support nor refusal: held for the user's review (no AI, no
        # alert). Last, so only jobs that pass every other filter are held.
        return UNKNOWN_REASON
    return None


def _signature(policy: dict, needs_sponsorship: bool, *, version: int) -> str:
    return hashlib.sha256(json.dumps({"filters": policy, "needs_sponsorship": needs_sponsorship,
                                      "version": version}, sort_keys=True).encode()).hexdigest()


def _tightened_title_signatures(policy: dict, needs_sponsorship: bool) -> set[str]:
    """Signatures of the same policy with an earlier, smaller default title
    exclusion list, in its original order (the current list must extend it)."""
    current = policy.get("title_must_exclude") or []
    earlier = [list(TITLE_EXCLUDE_BEFORE_20261028)]
    return {_signature({**policy, "title_must_exclude": old}, needs_sponsorship, version=6)
            for old in earlier if old != current and set(old) <= set(current)}


def recheck_titles(conn: Connection, settings: FilterSettings) -> tuple[int, Counter]:
    """Apply the title rules to open jobs that are not filtered (passed, matched
    but unsent, reposted) and reject the ones that now fail. Their queued AI
    tasks are dropped. Uses the (status, first_seen_at) index; no heap scan."""
    rows = conn.execute(select(jobs.c.id, jobs.c.title).where(
        jobs.c.closed_at.is_(None), jobs.c.status.in_(["DISCOVERED", "REPOSTED", "MATCHED"]))).all()
    decisions: dict[str, list[int]] = {}
    for row in rows:
        reason = title_reason(row.title, settings)
        if reason:
            decisions.setdefault(reason, []).append(row.id)
    rejected = Counter()
    for reason, ids in decisions.items():
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            conn.execute(update(jobs).where(jobs.c.id.in_(chunk)).values(
                status="FILTERED", filter_reason=reason, filtered_at=func.now()))
            resolve(conn, chunk, reason)
            drop_tasks(conn, "match", chunk)
        rejected[reason] += len(ids)
    return len(rows), rejected


@dataclass
class FilterResult:
    passed: int = 0
    rejected: Counter = field(default_factory=Counter)  # reason -> count
    passed_ids: list[int] = field(default_factory=list)


def run_filters(conn: Connection, settings: FilterSettings, *, recheck: bool = False,
                needs_sponsorship: bool = False, only_ids: list[int] | None = None) -> FilterResult:
    """Filter new canonical open jobs.

    recheck=True re-evaluates earlier decisions too (use it after changing
    [filters] or [preferences] in profile.toml): newly failing jobs, including
    already-matched ones that haven't been sent yet, become FILTERED, and
    previously filtered jobs that now pass go back to DISCOVERED.
    An enabled posting-age cutoff is rechecked every run, including NOTIFIED
    jobs, while application facts and review choices are retained.
    """
    # The discovery window is left out on purpose: it only widens freshness for
    # postings first seen in the last 72 h, which migration 0014 re-queues, and
    # a full recheck (several heap scans of jobs, one transaction) is not worth
    # that on the VM's disk. Including it (even unset) would force one on deploy.
    # require_explicit_sponsorship is left out too: it only decides whether a
    # posting that is silent about sponsorship is held for review or scored.
    # Turning it off releases exactly the held rows (below); turning it on holds
    # new postings and send-time eligibility blocks unsent silent ones. Neither
    # needs a full recheck.
    policy = settings.model_dump(exclude={"max_discovery_age_days", "require_explicit_sponsorship"})
    signature = _signature(policy, needs_sponsorship, version=6)
    previous = conn.execute(select(app_state.c.value).where(app_state.c.key == "filter_context")).scalar_one_or_none()
    # Version 5 signatures included require_explicit_sponsorship. Accept either
    # value of it, so this upgrade alone does not force a full recheck.
    legacy = {_signature({**policy, "require_explicit_sponsorship": value}, needs_sponsorship, version=5)
              for value in (True, False)}
    # Title exclusions were only added since the stored signature: that can only
    # reject more jobs, so re-checking titles of the jobs not filtered yet is
    # enough. A full recheck (heap scans, row locks held for minutes on the VM)
    # is reserved for real policy changes.
    titles_tightened = (not recheck and previous is not None and previous != signature
                        and previous not in legacy
                        and previous in _tightened_title_signatures(policy, needs_sponsorship))
    recheck = recheck or (previous != signature and previous not in legacy and not titles_tightened)
    result = FilterResult()
    if titles_tightened:
        checked, rejected = recheck_titles(conn, settings)
        result.rejected.update(rejected)
        log.info("filters: title exclusions added; %d unfiltered open job(s) rechecked by title, "
                 "%d now rejected", checked, rejected.total())
    if not settings.require_explicit_sponsorship:
        released = release_held(conn)
        if released:
            log.info("filters: %d job(s) held for a sponsorship review released for scoring", len(released))
            if only_ids is not None:
                only_ids = [*only_ids, *released]
    statuses = ["DISCOVERED", "REPOSTED", "FILTERED", "MATCHED"]
    if settings.max_posting_age_days is not None:
        statuses.append("NOTIFIED")
    active = and_(jobs.c.closed_at.is_(None), jobs.c.status.in_(statuses))
    if only_ids is not None and not recheck:
        # Queue workers: evaluate exactly the claimed jobs, as a recheck would
        # (they were explicitly queued). A policy change still forces the full
        # pass below, so every job sees the new rules exactly once.
        active = and_(active, jobs.c.id.in_(list(only_ids)))
        recheck = True
    now = datetime.now(UTC)
    if not recheck:
        # Incremental passes touch only the few rows in the small partial
        # indexes (0009/0012), but PostgreSQL cannot estimate the OR-heavy
        # predicates and chose a sequential scan of the whole jobs heap
        # (~700 MB, minutes of disk on the VM) every sweep. Forbid that here;
        # a full recheck (policy change) still scans normally.
        conn.execute(text("SET LOCAL enable_seqscan = off"))
    if settings.max_posting_age_days is not None:
        # Expiration changes with time, content eligibility does not. Reject
        # expired/missing dates server-side without transferring any job bodies.
        reasons = [
            ("posting date: unavailable", jobs.c.posted_at.is_(None)),
            ("posting date: in the future", jobs.c.posted_at > now),
            (f"posting date: older than {settings.max_posting_age_days} days",
             and_(jobs.c.posted_at < now - timedelta(days=settings.max_posting_age_days),
                  not_(discovered_recently(settings.max_posting_age_days,
                                           settings.max_discovery_age_days, now)))),
        ]
        # Once rejected for an old/missing date, a row needs no daily heap
        # scan. Ingestion clears filtered_at after date/content changes, and
        # policy changes deliberately recheck everything. Future dates remain
        # eligible for the moving time window.
        # Content rejections (title/visa/experience/location) are final until
        # the posting or the policy changes (ingestion clears filtered_at; a
        # policy change rechecks everything), so they are never relabelled as
        # expired: that rewrote thousands of rows a day for nothing and kept
        # them in the expiry index. This set is a subset of ix_jobs_expiry_active's
        # predicate (0012), so that small index still serves these UPDATEs.
        expiry_candidates = or_(jobs.c.status != "FILTERED", jobs.c.filtered_at.is_(None),
            jobs.c.filter_reason.is_(None), jobs.c.filter_reason == "posting date: in the future")
        for reason, predicate in reasons:
            count = conn.execute(update(jobs).where(active, predicate,
                true() if recheck else expiry_candidates,
                or_(jobs.c.status != "FILTERED", jobs.c.filter_reason.is_distinct_from(reason),
                    jobs.c.filtered_at.is_(None))).values(
                    status="FILTERED", filter_reason=reason, filtered_at=func.now())).rowcount
            if count:
                result.rejected[reason] += count
    if recheck:
        pending = and_(active, recent_jobs(settings.max_posting_age_days, now,
                                           settings.max_discovery_age_days))
    else:
        # Exactly the ix_jobs_pending_active predicate, nothing else: a posted_at
        # range here made PostgreSQL walk the date index over every recent open
        # job (~30k heap pages per sweep in production) to find a handful of
        # pending rows. The publication window is checked in Python instead.
        pending = and_(active, or_(jobs.c.filtered_at.is_(None),
                                   jobs.c.filter_reason == "posting date: in the future"))
    decisions: dict[str | None, list[int]] = {}
    # Read small metadata first: excluded titles never need a description.
    cursor = 0
    while True:
        rows = conn.execute(select(jobs.c.id, jobs.c.title, jobs.c.posted_at, jobs.c.first_seen_at,
                                   jobs.c.discovered_in_backfill)
                            .where(pending, jobs.c.id > cursor).order_by(jobs.c.id).limit(250)).all()
        if not rows:
            break
        cursor = rows[-1].id
        body_ids = []
        for row in rows:
            reason = (publication_reason(row.posted_at, settings.max_posting_age_days, now,
                                         first_seen_at=row.first_seen_at, backfill=row.discovered_in_backfill,
                                         discovery_days=settings.max_discovery_age_days)
                      or title_reason(row.title, settings))
            if reason:
                decisions.setdefault(reason, []).append(row.id)
            else:
                body_ids.append(row.id)
        if body_ids:
            query = select(jobs.c.id, jobs.c.source, jobs.c.title, jobs.c.location, jobs.c.fingerprint,
                           jobs.c.description, location_raw(), jobs.c.posted_at, jobs.c.first_seen_at,
                           jobs.c.discovered_in_backfill, jobs.c.raw_digest).where(jobs.c.id.in_(body_ids))
            held = []
            for row in conn.execute(query):
                # A pruned job has no text to judge. Pruning only takes jobs no
                # posting-date rule can accept, so this is reached only if the
                # rules were widened later; never pass such a job to the AI.
                reason = PRUNED_REASON if row.raw_digest is not None else reject_reason(
                    source=row.source, title=row.title, location=row.location,
                    description=row.description, raw=row.raw, settings=settings,
                    needs_sponsorship=needs_sponsorship, posted_at=row.posted_at,
                    first_seen_at=row.first_seen_at, backfill=row.discovered_in_backfill)
                if reason == UNKNOWN_REASON:
                    held.append(row)
                else:
                    decisions.setdefault(reason, []).append(row.id)
            kept = kept_fingerprints(conn, [r.fingerprint for r in held])
            for row in held:   # the user kept this role from the review queue: normal pipeline
                decisions.setdefault(None if row.fingerprint in kept else UNKNOWN_REASON, []).append(row.id)

    for reason, ids in decisions.items():
        # Passing keeps MATCHED jobs matched; only FILTERED ones come back as DISCOVERED.
        status = case((jobs.c.status == "FILTERED", "DISCOVERED"), else_=jobs.c.status) if reason is None else "FILTERED"
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            conn.execute(update(jobs).where(jobs.c.id.in_(chunk)).values(
                status=status, filter_reason=reason, filtered_at=func.now()))
            if reason == UNKNOWN_REASON:
                hold(conn, chunk)
            else:
                resolve(conn, chunk, reason or "passed")
        if reason is None:
            result.passed = len(ids)
            result.passed_ids = list(ids)
        else:
            result.rejected[reason] += len(ids)
    if not recheck:
        conn.execute(text("SET LOCAL enable_seqscan = DEFAULT"))
    log.info("filters: %d passed, %d rejected", result.passed, result.rejected.total())
    conn.execute(insert(app_state).values(key="filter_context", value=signature)
        .on_conflict_do_update(index_elements=[app_state.c.key], set_={"value": signature}))
    return result

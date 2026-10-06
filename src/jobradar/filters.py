"""Hard filters: cheap, deterministic rules that run before any AI.

They reject only when confident. Missing a good job costs more than one extra
AI call, so anything ambiguous ("Remote", no stated experience) passes through.
"""

import logging
import re
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import case, func, select, update
from sqlalchemy.engine import Connection

from jobradar.collectors import COLLECTORS
from jobradar.db import jobs
from jobradar.dedup import normalize_key
from jobradar.settings import FilterSettings

log = logging.getLogger(__name__)

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
    match = _NO_SPONSORSHIP.search(text)
    return f"visa: \"{' '.join(match.group(0).split())}\"" if match else None


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


def _place(location: str) -> str:
    """'us', 'non-us' or 'unknown' for one location string."""
    if re.fullmatch(r"[A-Z]{2}", location.strip()):  # ISO-2 country code (Lever)
        return "us" if location.strip() == "US" else "non-us"
    text = normalize_key(location)
    if _US_STATE_CODE.search(location) or any(_has_phrase(text, w) for w in _US_WORDS):
        return "us"
    if _CA_PROVINCE_CODE.search(location) or any(_has_phrase(text, w) for w in _NON_US_WORDS):
        return "non-us"
    return "unknown"


def location_reason(locations: list[str]) -> str | None:
    """Reject only if every known location is outside the US; "Remote" alone passes."""
    places = [_place(loc) for loc in locations]
    if places and all(p == "non-us" for p in places):
        return f"location: {locations[0]}"
    return None


# --- putting it together ----------------------------------------------------------

def reject_reason(*, source: str, title: str, location: str | None, description: str,
                  raw: dict, settings: FilterSettings, needs_sponsorship: bool = False) -> str | None:
    """Why this job should be rejected, or None if it passes every hard filter."""
    if reason := title_reason(title, settings):
        return reason
    years = required_years(description)
    if years is not None and years > settings.max_years_required:
        return f"experience: requires {years}+ years"
    if needs_sponsorship and (reason := sponsorship_reason(description)):
        return reason
    if settings.us_only:
        extra = COLLECTORS[source].locations(raw) if source in COLLECTORS else []
        return location_reason([loc for loc in [location, *extra] if loc])
    return None


@dataclass
class FilterResult:
    passed: int = 0
    rejected: Counter = field(default_factory=Counter)  # reason -> count


def run_filters(conn: Connection, settings: FilterSettings, *, recheck: bool = False,
                needs_sponsorship: bool = False) -> FilterResult:
    """Filter new canonical open jobs.

    recheck=True re-evaluates earlier decisions too (use it after changing
    [filters] or [preferences] in profile.toml): newly failing jobs, including
    already-matched ones that haven't been sent yet, become FILTERED, and
    previously filtered jobs that now pass go back to DISCOVERED.
    """
    query = select(jobs.c.id, jobs.c.source, jobs.c.title, jobs.c.location, jobs.c.description,
                   jobs.c.raw).where(jobs.c.closed_at.is_(None))
    if recheck:
        query = query.where(jobs.c.status.in_(["DISCOVERED", "FILTERED", "MATCHED"]))
    else:
        query = query.where(jobs.c.status == "DISCOVERED", jobs.c.filtered_at.is_(None))

    result = FilterResult()
    decisions: dict[str | None, list[int]] = {}
    for row in conn.execute(query):
        reason = reject_reason(source=row.source, title=row.title, location=row.location,
                               description=row.description, raw=row.raw, settings=settings,
                               needs_sponsorship=needs_sponsorship)
        decisions.setdefault(reason, []).append(row.id)

    for reason, ids in decisions.items():
        # Passing keeps MATCHED jobs matched; only FILTERED ones come back as DISCOVERED.
        status = case((jobs.c.status == "FILTERED", "DISCOVERED"), else_=jobs.c.status) if reason is None else "FILTERED"
        conn.execute(update(jobs).where(jobs.c.id.in_(ids)).values(
            status=status, filter_reason=reason, filtered_at=func.now()))
        if reason is None:
            result.passed = len(ids)
        else:
            result.rejected[reason] = len(ids)
    log.info("filters: %d passed, %d rejected", result.passed, result.rejected.total())
    return result

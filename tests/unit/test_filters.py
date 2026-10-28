import pytest

from jobradar.collectors import AshbyCollector, GreenhouseCollector, LeverCollector
from jobradar.filters import location_reason, reject_reason, required_years, sponsorship_reason, title_reason
from jobradar.settings import FilterSettings, Settings, SettingsError, load_settings
from tests.factories import ashby_job, greenhouse_job, lever_posting

DEFAULTS = FilterSettings()


class TestTitle:
    @pytest.mark.parametrize("title", [
        "Software Engineer, New Grad", "Software Engineer II", "Backend Developer",
        "Member of Technical Staff", "Forward Deployed Engineer", "SWE - Early Career",
        "Machine Learning Engineer", "Leading Edge Developer", "Software Engineer (Remote)",
    ])
    def test_passes(self, title):
        assert title_reason(title, DEFAULTS) is None

    @pytest.mark.parametrize("title,reason", [
        ("Senior Software Engineer", "senior"), ("Sr. Backend Engineer", "sr"),
        ("Staff Engineer", "staff"), ("Principal Engineer", "principal"),
        ("Engineering Manager", "manager"), ("Head of Engineering", "head of"),
        ("Software Engineer III", "iii"), ("Software Engineer Intern", "intern"),
        ("QA Engineer", "qa"), ("Solutions Engineer", "solutions engineer"),
        ("Aerodynamics & Performance Engineer (R5732)", "aerodynamics"),
        ("Software Engineer, Backend (Senior)", "senior"),
    ])
    def test_rejects_with_the_matching_word(self, title, reason):
        assert title_reason(title, DEFAULTS) == f"title: {reason}"

    @pytest.mark.parametrize("title", ["Product Designer", "Account Executive", "Data Analyst"])
    def test_non_engineering_titles(self, title):
        assert title_reason(title, DEFAULTS) == "title: not a software engineering role"

    def test_staff_inside_member_of_technical_staff_is_neutral_but_other_words_still_count(self):
        assert title_reason("Senior Member of Technical Staff", DEFAULTS) == "title: senior"

    def test_lists_are_configurable(self):
        settings = FilterSettings(title_must_include=[], title_must_exclude=["designer"])
        assert title_reason("Senior Product Manager", settings) is None
        assert title_reason("Product Designer", settings) == "title: designer"


class TestRequiredYears:
    @pytest.mark.parametrize("text,years", [
        ("Requirements: 5+ years of professional experience.", 5),
        ("5 or more years of software experience", 5),
        ("At least five years of industry experience", 5),
        ("3-5 years of experience", 3),
        ("3 – 5 yrs experience building APIs", 3),
        ("0-2 years of experience", 0),
        ("Minimum of 7 years experience", 7),
        ("1+ yrs experience. 6+ years experience in X.", 1),  # smallest wins
    ])
    def test_found(self, text, years):
        assert required_years(text) == years

    @pytest.mark.parametrize("text", [
        "",
        "We have been around for 10 years. You have experience with Go.",  # different sentences
        "Bonus: 5+ years experience with Kafka.",
        "5+ years of experience preferred.",
        "Ideally 6 years of experience in fintech.",
        "Our 401k vests over 4 years.",
    ])
    def test_not_found_or_optional(self, text):
        assert required_years(text) is None

    def test_preferred_sentence_ignored_but_required_one_kept(self):
        assert required_years("2+ years of experience required. 7+ years of experience preferred.") == 2


class TestLocation:
    @pytest.mark.parametrize("locations", [
        ["New York, NY"], ["Austin, TX"], ["Remote (US)"], ["Remote - United States"], ["Hybrid - Bay Area"],
        ["Remote"], [], ["Remote (Canada)", "Remote (US)"], ["London, United Kingdom", "US"],
        ["Somewhere Unknown"], ["Remote in Europe", "Remote"],
    ])
    def test_passes(self, locations):
        assert location_reason(locations) is None

    @pytest.mark.parametrize("locations", [
        ["London, United Kingdom"], ["London, United Kingdom", "GB"], ["Toronto, ON"], ["Dublin"],
        ["Singapore, Singapore", "SG"], ["Remote in Europe"], ["Bengaluru, India"], ["Remote (Canada)"],
    ])
    def test_rejects_when_every_location_is_outside_the_us(self, locations):
        assert location_reason(locations) == f"location: {locations[0]}"

    @pytest.mark.parametrize("locations", [
        # Workday ISO-3 prefixes and structured country fields next to unknown cities
        ["DEU - Saxony - Dresden", "Germany"], ["IND - HR - NOIDA"], ["BGR - SOFIA"], ["PHL Quezon City - UP Ayala"],
        ["Kronberg", "Germany"], ["Qingzhou, Shandong", "China"], ["DLF CYBERCITY 12B", "India"],
        ["Ha Noi", "Hà Nội", "Vietnam", "VN"], ["Koudekerk aan den Rijn, Zuid-Holland", "Netherlands", "NL"],
        ["BUCHAREST, , ROMANIA"], ["Athens, Attica, Greece"], ["München"], ["Prague"], ["Remote - Latin America"],
    ])
    def test_rejects_jobs_abroad_named_by_country_fields(self, locations):
        assert location_reason(locations) == f"location: {locations[0]}"

    @pytest.mark.parametrize("locations", [
        ["USA - Texas - Austin", "United States of America"], ["Client Site - USA - CA - "], ["US, Louisville, CO"],
        ["Mountain View, 650 Castro"], ["Raleigh", "London"], ["2 Locations", "India"], ["Remote", "Germany"],
        ["Jersey City"], ["Remote - New England"], ["Melbourne, FL"], ["Athens, GA"], ["PRI - San Juan"],
    ])
    def test_us_generic_or_ambiguous_places_still_pass(self, locations):
        assert location_reason(locations) is None

    def test_state_codes_need_the_comma_form(self):
        # "in" / "or" / "me" are English words, not Indiana/Oregon/Maine.
        assert location_reason(["Remote in Europe or Asia"]) is not None


class TestCollectorLocations:
    def test_greenhouse_offices(self):
        raw = greenhouse_job(1, offices=[{"name": "NYC", "location": "New York, NY"}, {"name": "Dublin"}, "junk"])
        assert GreenhouseCollector.locations(raw) == ["New York, NY", "Dublin"]

    def test_lever_all_locations_and_country(self):
        raw = lever_posting("a", country="GB", categories={"location": "London", "allLocations": ["London", "Paris"]})
        assert LeverCollector.locations(raw) == ["London", "Paris", "GB"]
        assert LeverCollector.locations({"categories": "junk"}) == []

    def test_ashby_secondary_locations_and_countries(self):
        raw = ashby_job("a", secondaryLocations=[
            {"location": "Remote (US)", "address": {"postalAddress": {"addressCountry": "United States"}}},
            "junk"])
        assert AshbyCollector.locations(raw) == ["Remote (US)", "USA", "United States"]
        assert AshbyCollector.locations({}) == []


class TestRejectReason:
    def check(self, **kw):
        job = dict(source="greenhouse", title="Software Engineer", location="New York, NY",
                   description="", raw={}, settings=DEFAULTS) | kw
        return reject_reason(**job)

    def test_passes(self):
        assert self.check() is None

    def test_title_checked_first(self):
        assert self.check(title="Senior Engineer", location="London, UK") == "title: senior"

    def test_experience(self):
        assert self.check(description="5+ years of experience.") == "experience: requires 5+ years"
        assert self.check(description="4+ years of experience.") is None  # threshold is 4

    def test_location_uses_the_collectors_extra_locations(self):
        london_only = lever_posting("a", country="GB", categories={"location": "London"})
        london_and_ny = lever_posting("a", country="GB", categories={"allLocations": ["London", "New York, NY"]})
        assert self.check(source="lever", location="London", raw=london_only) == "location: London"
        assert self.check(source="lever", location="London", raw=london_and_ny) is None

    def test_us_only_can_be_turned_off(self):
        assert self.check(location="London, UK", settings=FilterSettings(us_only=False)) is None

    def test_unknown_source_still_filters_on_primary_location(self):
        assert self.check(source="workday", location="Paris") == "location: Paris"


class TestSettings:
    def test_missing_file_means_defaults(self, tmp_path):
        assert load_settings(tmp_path / "nope.toml") == Settings()

    def test_shipped_profile_is_valid(self):
        assert load_settings("profile.toml").filters.us_only is True

    def test_override(self, tmp_path):
        f = tmp_path / "p.toml"
        f.write_text("[filters]\nmax_years_required = 2\ntitle_must_exclude = ['senior']\n")
        s = load_settings(f).filters
        assert (s.max_years_required, s.title_must_exclude, s.us_only) == (2, ["senior"], True)

    @pytest.mark.parametrize("body,message", [
        ("[filters]\nmax_years = 2\n", "max_years"),            # typo
        ("[filters]\nmax_years_required = -1\n", "greater than or equal"),
        ("[filterz]\n", "filterz"),
        ("[filters\n", "invalid TOML"),
    ])
    def test_invalid(self, tmp_path, body, message):
        f = tmp_path / "p.toml"
        f.write_text(body)
        with pytest.raises(SettingsError, match=message):
            load_settings(f)


class TestSponsorship:
    @pytest.mark.parametrize("text", [
        "We are unable to sponsor visas for this role.",
        "Visa sponsorship is not available for this position.",
        "Candidates must be authorized to work in the U.S. without the need for current or future sponsorship.",
        "Must be legally authorized to work in the United States without employer sponsorship now or in the future.",
        "This role is not eligible for visa sponsorship.",
        "We do not offer H-1B sponsorship.",
        "We're not able to offer visa sponsorship for this role.",
        "We are not in a position to sponsor visas.",
        "We will not sponsor employment visas.",
        "Company cannot provide sponsorship at this time.",
        "No visa sponsorship.",
        "Visa Sponsorship: No",
        "U.S. citizenship is required.",
        "Applicants must be a US citizen.",
        "Must be able to obtain a security clearance.",
        "Active TS/SCI clearance required.",
        "Must have an active Secret clearance",
        "This position requires access to ITAR-controlled data; applicants must be U.S. persons.",
    ])
    def test_explicit_refusals_are_caught(self, text):
        assert sponsorship_reason(text) is not None

    @pytest.mark.parametrize("text", [
        "We provide equal employment opportunity without regard to race, religion, national origin, citizenship status.",
        "We do not discriminate on the basis of citizenship.",
        "Visa sponsorship is available for this role.",
        "We sponsor H-1B visas and support transfers.",
        "We offer H-1B sponsorship and green card support.",
        "We are open to sponsoring visas.",
        "Our sponsor program connects new hires with mentors.",
        "Will you now or in the future require sponsorship?",
        "",
    ])
    def test_neutral_or_positive_text_passes(self, text):
        assert sponsorship_reason(text) is None

    def test_reason_quotes_the_phrase(self):
        assert sponsorship_reason("Note: we are unable to sponsor\nvisas.") == 'visa: "unable to sponsor"'

    def test_only_applies_when_you_need_sponsorship(self):
        job = dict(source="greenhouse", title="Software Engineer", location="New York, NY",
                   description="We are unable to sponsor visas.", raw={}, settings=DEFAULTS)
        assert reject_reason(**job) is None
        assert reject_reason(**job, needs_sponsorship=True) == 'visa: "unable to sponsor"'


# --- Oct 28 additions: titles seen in production ----------------------------------------------

@pytest.mark.parametrize("title", [
    "Software Engineer Co-op", "Software Engineer Full Stack / Backend I (Co-op) - United States",
    "SONiC Software Design Engineer- New College Grad 2025", "Site Reliability Engineer",
    "Production Engineer", "Systems Software Engineer, Silicon Bringup", "Platform Software Engineer 2",
    "Backend Engineer, Data Ingestion", "Software Developer - ERP Applications",
    "Member of Technical Staff (Software Engineer, GPU Cluster Infrastructure)",
])
def test_oct28_exclusions_keep_software_roles(title):
    assert title_reason(title, DEFAULTS) is None


@pytest.mark.parametrize("title,phrase", [
    ("Field Service Engineer II (C2)", "field service"),
    ("Supplier Quality Engineer II", "quality engineer"),
    ("(USA) Process Engineer", "process engineer"),
    ("Controls Engineer", "controls engineer"),
    ("ASIC Design Verification Engineer II", "asic"),
    ("RF Engineer I - New Grad (R6108)", "rf"),
    ("Systems Engineering Technician", "technician"),
    ("Associate Utility Engineer", "utility engineer"),
    ("T&D Planning Engineer", "planning engineer"),
    ("CAD Engineer - AI Trainer", "ai trainer"),
    ("Presales System Engineer - K12 and Local Government - Utah", "presales"),
    ("Data Centre - Commissioning Engineer (Mission Critical)", "commissioning"),
])
def test_oct28_exclusions_reject_non_software_engineering(title, phrase):
    assert title_reason(title, DEFAULTS) == f"title: {phrase}"

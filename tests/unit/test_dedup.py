import pytest

from jobradar import dedup


class TestCanonicalUrl:
    def test_strips_tracking_keeps_identifying_params(self):
        a = dedup.canonical_url("HTTP://www.Acme.com/careers/?utm_source=x&gh_src=li&gh_jid=7001#apply")
        assert a == "https://acme.com/careers?gh_jid=7001"

    def test_different_job_ids_stay_different(self):
        assert dedup.canonical_url("https://acme.com/c?gh_jid=1") != dedup.canonical_url("https://acme.com/c?gh_jid=2")

    def test_query_param_order_is_irrelevant(self):
        assert dedup.canonical_url("https://x.com/j?b=2&a=1") == dedup.canonical_url("https://x.com/j?a=1&b=2")

    @pytest.mark.parametrize("param", ["utm_anything", "UTM_SOURCE", "lever-source", "gclid", "Ref"])
    def test_tracking_params_removed_case_insensitively(self, param):
        assert dedup.canonical_url(f"https://x.com/j?{param}=v&id=9") == "https://x.com/j?id=9"

    def test_path_normalization(self):
        assert dedup.canonical_url("https://x.com//jobs///123/") == "https://x.com/jobs/123"
        assert dedup.canonical_url("https://x.com") == "https://x.com/"

    def test_default_ports_dropped_custom_ports_kept(self):
        assert dedup.canonical_url("https://x.com:443/j") == "https://x.com/j"
        assert dedup.canonical_url("https://x.com:8443/j") == "https://x.com:8443/j"

    def test_idempotent(self):
        once = dedup.canonical_url("http://www.x.com/a/?utm_source=1&b=2")
        assert dedup.canonical_url(once) == once


class TestKeys:
    def test_normalize_key_lowercases_and_strips_accents_and_punctuation(self):
        assert dedup.normalize_key("  Café — Engineer (II)! ") == "cafe engineer ii"
        assert dedup.normalize_key(None) == ""

    @pytest.mark.parametrize("name", ["Acme", "Acme, Inc.", "ACME LLC", "Acme Corp", "acme technologies"])
    def test_company_key_ignores_legal_suffixes(self, name):
        assert dedup.company_key(name) == "acme"

    def test_fingerprint_is_stable_across_formatting(self):
        a = dedup.fingerprint("Acme, Inc.", "Software Engineer - New Grad", "New York, NY")
        b = dedup.fingerprint("acme", "software engineer  new grad", "new york ny")
        assert a == b and len(a) == 64

    @pytest.mark.parametrize("change", [
        {"company": "Other"}, {"title": "Data Engineer"}, {"location": "Seattle, WA"}, {"location": None},
    ])
    def test_fingerprint_changes_with_any_component(self, change):
        base = {"company": "Acme", "title": "SWE", "location": "NYC"}
        assert dedup.fingerprint(**base) != dedup.fingerprint(**{**base, **change})

    def test_fingerprint_fields_cannot_bleed_into_each_other(self):
        # Separator prevents ("ab","c") colliding with ("a","bc").
        assert dedup.fingerprint("ab", "c", None) != dedup.fingerprint("a", "bc", None)

    def test_description_hash_ignores_whitespace_and_case(self):
        assert dedup.description_hash("Build  services.\n\nIn Java") == dedup.description_hash("build services. in java")

    def test_description_hash_detects_real_changes(self):
        assert dedup.description_hash("Uses Java") != dedup.description_hash("Uses Kafka")

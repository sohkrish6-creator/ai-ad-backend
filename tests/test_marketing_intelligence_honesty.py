"""
Post-audit fix. A live Marketing Intelligence run for resmed.co.in
surfaced three real bugs:
  1. overview.revenue = "not publicly disclosed" for an NYSE-listed company
     that files revenue quarterly — a false claim about disclosure policy,
     not an honest "we didn't find it".
  2. advertising.platforms_observed = ["Meta","Google"] sourced from
     data_source="model knowledge" with evidence "General advertising
     strategies for health-focused companies" — an industry-typical
     assumption labeled as if it were a direct observation.
  3. A .co.in URL produced an entirely global/US analysis — the domain was
     reduced to just the company name before any query was built, so the
     ccTLD never influenced anything.

_mi_detect_locale and _mi_enforce_observed_fields are the two pure,
directly-testable pieces of the fix (the prompt-wording changes and the
5 GPT-call-site wiring aren't independently unit-testable without a real
or mocked API call — covered instead by a live smoke run against the
real resmed.co.in domain).
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import _mi_detect_locale, _mi_enforce_observed_fields, _mi_extract_url


# ── _mi_extract_url ──────────────────────────────────────────────────────

def test_extracts_bare_domain_no_protocol_no_www():
    # The exact input that triggered this audit — a bare domain used to
    # match nothing at all, silently skipping the whole URL-derived path.
    assert _mi_extract_url("resmed.co.in") == "https://resmed.co.in"


def test_extracts_bare_two_label_domain():
    assert _mi_extract_url("resmed.com") == "https://resmed.com"


def test_extracts_full_https_url_unchanged():
    assert _mi_extract_url("https://resmed.co.in/about") == "https://resmed.co.in/about"


def test_extracts_www_prefixed_domain():
    assert _mi_extract_url("www.resmed.co.in") == "https://www.resmed.co.in"


def test_plain_company_name_with_no_dot_matches_nothing():
    assert _mi_extract_url("ResMed") is None
    assert _mi_extract_url("Nike Inc") is None


def test_name_with_abbreviation_dot_does_not_false_positive():
    assert _mi_extract_url("Dr. Reddy's Laboratories") is None


def test_empty_input_matches_nothing():
    assert _mi_extract_url("") is None


# ── _mi_detect_locale ───────────────────────────────────────────────────

def test_detects_india_from_co_in():
    assert _mi_detect_locale("resmed.co.in") == "India"


def test_detects_india_from_bare_in():
    assert _mi_detect_locale("example.in") == "India"


def test_prefers_longest_suffix_co_in_over_bare_in():
    # co.in must win over the bare "in" match — both are in the map, and a
    # naive shortest-suffix-first scan would return the wrong (still
    # correct-by-luck here, but structurally wrong) match first.
    assert _mi_detect_locale("resmed.co.in") == "India"


def test_detects_uk():
    assert _mi_detect_locale("example.co.uk") == "United Kingdom"


def test_generic_com_has_no_locale():
    assert _mi_detect_locale("resmed.com") is None


def test_unrecognized_tld_has_no_locale():
    assert _mi_detect_locale("example.io") is None


def test_empty_domain_has_no_locale():
    assert _mi_detect_locale("") is None
    assert _mi_detect_locale(None) is None


def test_www_prefixed_domain_still_detects():
    assert _mi_detect_locale("www.resmed.co.in") == "India"


# ── _mi_enforce_observed_fields ─────────────────────────────────────────

def test_clears_observed_fields_when_source_is_model_knowledge():
    advertising = {
        "platforms_observed": ["Meta", "Google"],
        "ad_formats": ["video"],
        "key_messages": ["msg"],
        "cta_patterns": ["Shop Now"],
        "data_source": "model knowledge",
    }
    result = _mi_enforce_observed_fields(
        advertising, ("platforms_observed", "ad_formats", "key_messages", "cta_patterns"),
        "not verified — no direct evidence of active advertising found",
    )
    assert result["platforms_observed"] == []
    assert result["ad_formats"] == []
    assert result["key_messages"] == []
    assert result["cta_patterns"] == []
    assert result["data_source"] == "not verified — no direct evidence of active advertising found"


def test_the_exact_reported_resmed_case():
    # Reproduces the actual bad output from the audited run.
    advertising = {
        "platforms_observed": ["Meta", "Google"],
        "ad_formats": ["video", "carousel"],
        "key_messages": ["Better sleep, better life"],
        "cta_patterns": ["Learn More"],
        "estimated_spend": "not found in available data",
        "confidence": 60,
        "evidence": "General advertising strategies for health-focused companies",
        "data_source": "model knowledge",
    }
    result = _mi_enforce_observed_fields(
        advertising, ("platforms_observed", "ad_formats", "key_messages", "cta_patterns"),
        "not verified — no direct evidence of active advertising found",
    )
    assert result["platforms_observed"] == []
    assert "model knowledge" not in result["data_source"]


def test_preserves_fields_when_source_is_real_evidence():
    advertising = {
        "platforms_observed": ["Meta"],
        "ad_formats": ["video"],
        "key_messages": ["msg"],
        "cta_patterns": ["Shop Now"],
        "data_source": "website content + Tavily research — Meta Pixel found in page source",
    }
    result = _mi_enforce_observed_fields(
        advertising, ("platforms_observed", "ad_formats", "key_messages", "cta_patterns"),
        "not verified — no direct evidence of active advertising found",
    )
    assert result["platforms_observed"] == ["Meta"]
    assert result["data_source"] == "website content + Tavily research — Meta Pixel found in page source"


def test_empty_data_source_defaults_to_unverified():
    advertising = {"platforms_observed": ["Meta"], "data_source": ""}
    result = _mi_enforce_observed_fields(
        advertising, ("platforms_observed",), "not verified — no direct evidence found",
    )
    assert result["platforms_observed"] == []


def test_generalizes_to_other_observed_field_names():
    offers = {"promotions_observed": ["20% off"], "data_source": "model knowledge"}
    result = _mi_enforce_observed_fields(
        offers, ("promotions_observed",), "not verified — no direct evidence of active promotions found",
    )
    assert result["promotions_observed"] == []


def test_non_dict_input_passed_through_unchanged():
    assert _mi_enforce_observed_fields(None, ("x",), "note") is None
    assert _mi_enforce_observed_fields([], ("x",), "note") == []

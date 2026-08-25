"""
Post-audit fix. A live ResMed India Marketing Intelligence report had six of
eighteen sections self-reporting non-zero confidence with every finding
field empty and an honest "not verified" data_source — the model has no
reliable introspection into whether it found anything, so guard_report()
never trusts the self-reported number and instead caps it against a
deterministic ceiling computed from real payload population + data_source
honesty. See confidence_guard.py's module docstring for the full incident
and design rationale.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from confidence_guard import guard_report


def _resmed_advertising_section():
    # The exact real-world example from the incident report.
    return {
        "platforms_observed": [], "ad_formats": [], "key_messages": [], "cta_patterns": [],
        "estimated_spend": "not found in available data",
        "confidence": 60,
        "data_source": "not verified — no direct evidence found",
    }


def _well_evidenced_section():
    return {
        "platforms_observed": ["Meta", "Google"],
        "ad_formats": ["video", "carousel"],
        "key_messages": ["Save on premium plans", "Free trial available"],
        "cta_patterns": ["Book Now", "Get Started"],
        "estimated_spend": "₹5-10L/month (rough estimate)",
        "confidence": 70,
        "evidence": "Meta Ad Library shows 12 active ads; site uses gtag.js and fbq() tracking",
        "data_source": "Meta Ad Library + website tracking scripts observed directly",
    }


def test_the_exact_reported_bug_is_fixed():
    report = {"sections": {"advertising": _resmed_advertising_section()}}
    guard_report(report)
    section = report["sections"]["advertising"]
    assert section["confidence"] == 0
    assert section["data_label"] == "NO_DATA"
    assert section["confidence_reported"] == 60  # original preserved, never lost


def test_no_sections_key_returns_report_unchanged():
    report = {"success": True, "company_name": "Acme"}
    result = guard_report(report)
    assert result == {"success": True, "company_name": "Acme"}
    assert "_quality" not in result


def test_sections_not_a_dict_is_a_safe_noop():
    report = {"sections": "not a dict"}
    result = guard_report(report)
    assert result["sections"] == "not a dict"
    assert "_quality" not in result


def test_well_evidenced_section_is_labeled_verified_and_lightly_capped():
    report = {"sections": {"advertising": _well_evidenced_section()}}
    guard_report(report)
    section = report["sections"]["advertising"]
    assert section["data_label"] == "VERIFIED"
    assert section["confidence"] == 70  # 70 <= VERIFIED ceiling (90) — unchanged


def test_polite_empty_string_counts_as_empty_not_populated():
    # "not found in available data" is a non-empty string — a naive
    # `if not value` check would treat it as populated. This is exactly
    # the second-order trap the incident report called out.
    section = {
        "estimated_spend": "not found in available data",
        "confidence": 55,
        "data_source": "research",
    }
    report = {"sections": {"advertising": section}}
    guard_report(report)
    # Only payload field is the polite-empty string -> ratio 0 -> NO_DATA
    assert report["sections"]["advertising"]["data_label"] == "NO_DATA"
    assert report["sections"]["advertising"]["confidence"] == 0


def test_guard_never_raises_confidence():
    # A deliberately LOW self-report on an otherwise well-evidenced section
    # must stay low — the guard only ever lowers, never raises.
    section = _well_evidenced_section()
    section["confidence"] = 5
    report = {"sections": {"advertising": section}}
    guard_report(report)
    assert report["sections"]["advertising"]["confidence"] == 5
    assert report["sections"]["advertising"]["data_label"] == "VERIFIED"  # label is honest even though confidence wasn't raised to match


def test_partial_population_with_unverified_source_is_unverified_not_no_data():
    section = {
        "platforms_observed": ["Meta"],  # one real finding
        "ad_formats": [], "key_messages": [], "cta_patterns": [],
        "estimated_spend": "not found in available data",
        "confidence": 50,
        "data_source": "model knowledge",
    }
    report = {"sections": {"advertising": section}}
    guard_report(report)
    section = report["sections"]["advertising"]
    assert section["data_label"] == "UNVERIFIED"
    assert section["confidence"] == 25  # UNVERIFIED ceiling, not the NO_DATA ceiling of 0


def test_idempotent_second_call_does_not_further_lower_confidence():
    report = {"sections": {"advertising": _resmed_advertising_section()}}
    guard_report(report)
    first_pass = dict(report["sections"]["advertising"])
    guard_report(report)
    second_pass = report["sections"]["advertising"]
    assert second_pass["confidence"] == first_pass["confidence"]
    assert second_pass["confidence_reported"] == first_pass["confidence_reported"] == 60
    assert second_pass["data_label"] == first_pass["data_label"]


def test_idempotent_on_a_verified_section_too():
    report = {"sections": {"advertising": _well_evidenced_section()}}
    guard_report(report)
    first = dict(report["sections"]["advertising"])
    guard_report(report)
    second = report["sections"]["advertising"]
    assert second["confidence"] == first["confidence"] == 70
    assert second["confidence_reported"] == 70


def test_mutates_in_place_and_returns_the_same_object():
    report = {"sections": {"advertising": _resmed_advertising_section()}}
    result = guard_report(report)
    assert result is report
    assert result["sections"]["advertising"] is report["sections"]["advertising"]


def test_non_dict_sub_sections_are_skipped_without_crashing():
    # Mirrors MI's real shape: revenue_timeline/unique_stories/competitors
    # are lists, not dicts — must not raise, must not be touched.
    report = {"sections": {
        "advertising": _resmed_advertising_section(),
        "revenue_timeline": [{"period": "FY2023", "revenue_or_metric": "₹52,000 crore", "confidence": 80}],
        "unique_stories": [],
    }}
    guard_report(report)
    assert report["sections"]["revenue_timeline"] == [{"period": "FY2023", "revenue_or_metric": "₹52,000 crore", "confidence": 80}]
    assert report["sections"]["unique_stories"] == []
    assert report["_quality"]["sections_guarded"] == 1  # only advertising had a top-level confidence


def test_dict_sub_sections_with_no_confidence_key_are_skipped():
    # Mirrors MI's real 'social' section (nested per-platform dicts, SIE's
    # own data_label/note vocabulary, no numeric confidence) and 'swot'
    # (plain strengths/weaknesses arrays, no self-report fields at all).
    report = {"sections": {
        "advertising": _resmed_advertising_section(),
        "social": {"instagram": {"data_label": "NOT_VERIFIED", "note": "no data"}},
        "swot": {"strengths": ["strong brand"], "weaknesses": [], "opportunities": [], "threats": []},
    }}
    guard_report(report)
    assert report["sections"]["social"] == {"instagram": {"data_label": "NOT_VERIFIED", "note": "no data"}}
    assert report["sections"]["swot"]["strengths"] == ["strong brand"]
    assert "confidence" not in report["sections"]["swot"]
    assert report["_quality"]["sections_guarded"] == 1


def test_preexisting_self_reported_data_label_is_preserved_not_dropped():
    # MI's audience section prompt already asks the model for its own
    # free-text data_label (e.g. "ESTIMATED from research") — overwriting
    # data_label with the guard's own value is correct, but the original
    # must not silently vanish.
    section = {
        "primary_demographic": "Adults 25-45, urban India",
        "confidence": 65,
        "evidence": "LinkedIn company page + Crunchbase profile",
        "data_source": "LinkedIn + Crunchbase",
        "data_label": "ESTIMATED from research",
    }
    report = {"sections": {"audience": section}}
    guard_report(report)
    result = report["sections"]["audience"]
    assert result["data_label_self_reported"] == "ESTIMATED from research"
    assert result["data_label"] in ("VERIFIED", "INFERRED")  # now one of the guard's own controlled labels


def test_quality_block_needs_review_true_when_any_section_is_no_data_or_unverified():
    report = {"sections": {
        "advertising": _resmed_advertising_section(),
        "audience": _well_evidenced_section(),
    }}
    guard_report(report)
    assert report["_quality"]["needs_review"] is True
    assert report["_quality"]["sections_by_label"]["NO_DATA"] == 1
    assert report["_quality"]["sections_by_label"]["VERIFIED"] == 1


def test_quality_block_needs_review_false_when_all_sections_verified():
    report = {"sections": {
        "advertising": _well_evidenced_section(),
        "audience": _well_evidenced_section(),
    }}
    guard_report(report)
    assert report["_quality"]["needs_review"] is False
    assert report["_quality"]["overall_confidence"] == 70
    assert report["_quality"]["sections_guarded"] == 2


# ── research_ran: caller-supplied ground truth overrides self-reported text ──
# A live run with no TAVILY_API_KEY configured showed sections claiming
# data_source: "Tavily research" when no research had actually run — the
# model labeling its own training knowledge as research. Text-matching
# against data_source can't catch this (the string looks exactly like real
# evidence); only a fact the CALLER computed from real pipeline state can.

def test_research_ran_false_forces_unverified_even_with_convincing_data_source():
    section = _well_evidenced_section()
    section["data_source"] = "Tavily research"  # exactly the false claim from the incident
    section["research_ran"] = False  # ground truth: no research actually ran
    report = {"sections": {"overview": section}}
    guard_report(report)
    result = report["sections"]["overview"]
    assert result["data_label"] == "UNVERIFIED"
    assert result["confidence"] == 25  # UNVERIFIED ceiling, not left at the well-evidenced 70


def test_research_ran_true_does_not_force_a_label_still_uses_normal_heuristic():
    section = _well_evidenced_section()
    section["research_ran"] = True
    report = {"sections": {"advertising": section}}
    guard_report(report)
    # research_ran=True doesn't grant automatic trust — it just doesn't
    # veto; a genuinely well-evidenced section is still VERIFIED as before.
    assert report["sections"]["advertising"]["data_label"] == "VERIFIED"


def test_research_ran_absent_falls_back_to_text_heuristic_unchanged():
    # Callers that don't supply research_ran at all (any future caller of
    # guard_report, or a section this codebase doesn't yet wire it for)
    # must behave exactly as before this fix — the exact original bug case.
    report = {"sections": {"advertising": _resmed_advertising_section()}}
    guard_report(report)
    assert report["sections"]["advertising"]["data_label"] == "NO_DATA"
    assert report["sections"]["advertising"]["confidence"] == 0


def test_research_ran_is_not_treated_as_payload():
    # The fact itself must not count toward "how much of this section is
    # populated" — it's metadata about provenance, not a finding.
    section = {
        "estimated_spend": "not found in available data",
        "confidence": 50,
        "data_source": "not verified",
        "research_ran": False,
    }
    report = {"sections": {"advertising": section}}
    guard_report(report)
    assert report["sections"]["advertising"]["data_label"] == "NO_DATA"  # still 0/1 real payload fields, not 1/2


def test_research_ran_survives_idempotent_second_pass():
    section = _well_evidenced_section()
    section["data_source"] = "Tavily research"
    section["research_ran"] = False
    report = {"sections": {"overview": section}}
    guard_report(report)
    first = dict(report["sections"]["overview"])
    guard_report(report)
    second = report["sections"]["overview"]
    assert second["confidence"] == first["confidence"] == 25
    assert second["data_label"] == first["data_label"] == "UNVERIFIED"

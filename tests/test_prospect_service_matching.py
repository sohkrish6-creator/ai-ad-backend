"""
Post-audit fix. Prospect Discovery let GPT invent both the "detected
weakness" and "recommended service" from scratch, unconstrained by what
the tenant actually sells — which is how "Website Development" got
suggested for nearly every no-website prospect even when a tenant's
services_offered excluded it. _prospect_gap_and_angle is the one function
both the on-screen card and the DOCX/Excel export now go through, reusing
_voice_match_service (Revenue Engine's own enforcement) rather than a
second, drift-prone implementation.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import _prospect_gap_and_angle


def _evidence(type_, confidence):
    return {"type": type_, "value": "test", "confidence": confidence, "page": "test", "detected_at": "2026-01-01"}


def test_no_website_maps_to_website_development_when_offered():
    weaknesses = ["no_website", "low_review_count"]
    evidence = [_evidence("no_website", 0.98), _evidence("low_review_count", 0.95)]
    gap, angle = _prospect_gap_and_angle(weaknesses, evidence, ["website_development"])
    assert gap == "No Website"
    assert angle == "Website Development"


def test_the_exact_reported_bug_no_website_but_service_not_offered():
    # The exact reported case: tenant doesn't sell website_development.
    # Strongest gap is still "no website" (honestly reported), but the
    # angle must fall through to a weakness the tenant CAN actually sell.
    weaknesses = ["no_website", "low_review_count", "poor_reviews"]
    evidence = [_evidence("no_website", 0.98), _evidence("low_review_count", 0.95), _evidence("poor_reviews", 0.90)]
    gap, angle = _prospect_gap_and_angle(weaknesses, evidence, ["reputation_management"])
    assert gap == "No Website"  # still the real, strongest detected issue
    assert angle == "Reputation Management"  # but the pitch respects what's actually sold
    assert angle != "Website Development"


def test_no_matching_service_at_all_returns_no_service_fit():
    weaknesses = ["no_website"]
    evidence = [_evidence("no_website", 0.98)]
    gap, angle = _prospect_gap_and_angle(weaknesses, evidence, ["email_marketing"])
    assert gap == "No Website"
    assert angle == "No service fit"


def test_empty_services_offered_always_no_service_fit():
    weaknesses = ["no_website", "poor_reviews"]
    evidence = [_evidence("no_website", 0.98), _evidence("poor_reviews", 0.9)]
    gap, angle = _prospect_gap_and_angle(weaknesses, evidence, [])
    assert angle == "No service fit"


def test_no_weaknesses_detected_at_all():
    gap, angle = _prospect_gap_and_angle([], [], ["seo"])
    assert gap == "None detected"
    assert angle == "No service fit"


def test_picks_highest_confidence_weakness_as_the_gap_not_first_in_list():
    weaknesses = ["no_cta", "no_website"]  # deliberately out of confidence order
    evidence = [_evidence("no_cta", 0.55), _evidence("no_website", 0.98)]
    gap, _ = _prospect_gap_and_angle(weaknesses, evidence, [])
    assert gap == "No Website"  # 0.98 > 0.55, regardless of list order


def test_angle_can_differ_from_gap_but_both_are_real_detected_weaknesses():
    # tenant sells SEO (fits weak_seo_title) but not website_development —
    # angle should land on the real, lower-confidence weakness that fits.
    weaknesses = ["no_website", "weak_seo_title"]
    evidence = [_evidence("no_website", 0.98), _evidence("weak_seo_title", 0.70)]
    gap, angle = _prospect_gap_and_angle(weaknesses, evidence, ["seo"])
    assert gap == "No Website"
    assert angle == "SEO"


def test_unknown_weakness_code_falls_back_to_the_raw_code_as_label():
    gap, angle = _prospect_gap_and_angle(["some_future_code"], [_evidence("some_future_code", 0.9)], [])
    assert gap == "some_future_code"  # no crash, no fabricated pretty label
    assert angle == "No service fit"

"""
Post-audit fix, same bug class as test_prospect_service_matching.py's
_apply_no_service_fit_guard, one screen over. outreach_ai() (standalone
/outreach-ai) built its GPT context from generic memory tables with zero
awareness of voice_settings.services_offered_json — a live report found the
exact same failure mode already fixed in Prospect Discovery was reachable
here too: nothing stopped GPT from pitching "website development" (or any
other unsold service) in a cold email, WhatsApp message, call script, or
proposal opener.

Unlike Prospect Discovery, the pitch here isn't one structured field — it's
a multi-field free-text kit. _outreach_offered_service_violations scans
every text field for offer-shaped phrases naming a service outside
services_offered; _apply_outreach_service_fit_guard neutralizes whatever it
finds. Both are pure functions over plain dicts — no DB, no GPT call.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import (
    _voice_services_offered_line,
    _outreach_offered_service_violations,
    _apply_outreach_service_fit_guard,
    _OUTREACH_VIOLATION_REPLACEMENT,
)


def _kit(**overrides):
    base = {
        "cold_email": {"subject": "Quick question", "body": "Hi there, noticed a gap.", "ps_line": "Let's chat.", "why_it_works": "curiosity"},
        "linkedin_message": {"connection_request": "Loved your recent post.", "follow_up_message": "Would love to connect.", "why_it_works": "warm"},
        "whatsapp": {"message_1_pain": "Noticed your reviews are low.", "message_2_proof": "We've helped others like you.", "follow_up_day3": "Following up!", "follow_up_day7": "Last try!"},
        "instagram_dm": {"opener": "Love your feed!", "follow_up": "Still here if useful.", "why_it_works": "casual"},
        "call_script": {"opener_10sec": "Hi, quick one for you.", "pain_question": "What's your biggest marketing challenge?", "value_statement": "We help businesses grow.", "close": "Free Tuesday at 3pm?"},
        "objection_handling": [
            {"objection": "We already have someone", "response": "Totally get it, mind if I ask what's working?"},
        ],
        "follow_up_sequence": {"day1": "Send email", "day3": "WhatsApp touch", "day7": "Value share", "day14": "Final close"},
        "proposal_opener": "Here's how we can help you grow.",
        "confidence": 70,
    }
    base.update(overrides)
    return base


# ── _voice_services_offered_line ─────────────────────────────────────────────

def test_services_offered_line_lists_real_labels():
    line = _voice_services_offered_line(["social_media_management", "paid_ads"])
    assert "Social Media Management" in line
    assert "Paid Ads" in line
    assert "Website Development" not in line


def test_services_offered_line_empty_says_stay_generic():
    line = _voice_services_offered_line([])
    assert "no services configured" in line.lower()


# ── _outreach_offered_service_violations / _apply_outreach_service_fit_guard ──

def test_the_exact_reported_bug_website_development_pitched_when_not_offered():
    # Tenant sells social media + paid ads only — never web development.
    services_offered = ["social_media_management", "paid_ads"]
    kit = _kit()
    kit["call_script"]["value_statement"] = "We can build you a website that converts visitors into leads."
    violations = _outreach_offered_service_violations(kit, services_offered)
    assert any(label == "call_script.value_statement" for _, label, _ in violations)

    removed = _apply_outreach_service_fit_guard(kit, services_offered)
    assert ("call_script.value_statement", "build you a website") in removed
    assert kit["call_script"]["value_statement"] == _OUTREACH_VIOLATION_REPLACEMENT


def test_website_development_pitch_caught_in_cold_email_body():
    kit = _kit()
    kit["cold_email"]["body"] = "We'd love to help you with website development for your business."
    removed = _apply_outreach_service_fit_guard(kit, ["social_media_management", "paid_ads"])
    assert any(label == "cold_email.body" for label, _ in removed)
    assert kit["cold_email"]["body"] == _OUTREACH_VIOLATION_REPLACEMENT


def test_website_development_pitch_caught_in_proposal_opener():
    kit = _kit(proposal_opener="Let's start by helping you redesign your website from the ground up.")
    removed = _apply_outreach_service_fit_guard(kit, ["social_media_management", "paid_ads"])
    assert any(label == "proposal_opener" for label, _ in removed)
    assert kit["proposal_opener"] == _OUTREACH_VIOLATION_REPLACEMENT


def test_website_development_pitch_caught_in_objection_handling_response():
    kit = _kit()
    kit["objection_handling"][0]["response"] = "No worries — we could also design your website while we're at it."
    removed = _apply_outreach_service_fit_guard(kit, ["social_media_management", "paid_ads"])
    assert any(label == "objection_handling[0].response" for label, _ in removed)
    assert kit["objection_handling"][0]["response"] == _OUTREACH_VIOLATION_REPLACEMENT


def test_pain_point_mention_of_the_prospects_own_website_is_not_a_violation():
    # "your website" alone (no offer verb) is a legitimate weakness
    # observation, not a pitch — must survive untouched.
    kit = _kit()
    kit["whatsapp"]["message_1_pain"] = "I noticed your website has no clear call-to-action anywhere."
    removed = _apply_outreach_service_fit_guard(kit, ["social_media_management", "paid_ads"])
    assert removed == []
    assert kit["whatsapp"]["message_1_pain"] == "I noticed your website has no clear call-to-action anywhere."


def test_a_service_the_tenant_does_offer_is_never_flagged():
    kit = _kit()
    kit["call_script"]["value_statement"] = "We can manage your social media and run your ads end to end."
    removed = _apply_outreach_service_fit_guard(kit, ["social_media_management", "paid_ads"])
    assert removed == []
    assert kit["call_script"]["value_statement"] == "We can manage your social media and run your ads end to end."


def test_multiple_unsold_services_all_get_caught():
    services_offered = ["social_media_management"]
    kit = _kit()
    kit["cold_email"]["body"] = "We'll run your Google Ads campaign for you."
    kit["call_script"]["value_statement"] = "We can also handle email marketing campaigns for you."
    removed = _apply_outreach_service_fit_guard(kit, services_offered)
    labels = {label for label, _ in removed}
    assert "cold_email.body" in labels
    assert "call_script.value_statement" in labels


def test_empty_services_offered_flags_any_named_service():
    kit = _kit()
    kit["call_script"]["value_statement"] = "We specialize in SEO services for businesses like yours."
    removed = _apply_outreach_service_fit_guard(kit, [])
    assert any(label == "call_script.value_statement" for label, _ in removed)


def test_clean_kit_with_no_offered_services_pitched_is_completely_untouched():
    services_offered = ["social_media_management", "paid_ads"]
    kit = _kit()
    original = _kit()
    removed = _apply_outreach_service_fit_guard(kit, services_offered)
    assert removed == []
    assert kit == original


def test_violations_scan_is_a_safe_noop_on_missing_fields():
    kit = {"cold_email": {}, "confidence": 80}
    violations = _outreach_offered_service_violations(kit, ["seo"])
    assert violations == []

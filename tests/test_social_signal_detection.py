"""
Post-audit fix. A healthy-looking business (working website, decent
reviews) used to read as "no detected gaps" even with zero real social
media presence — _detect_voice_weaknesses had no signal for it at all, and
only 1 of the tenant's 4 configured services (paid_ads, via
missing_tracking) had a real detection path. no_cta was also mapped to the
wrong service (content_creation, which this tenant sells as reels/
photography/videography — a missing homepage CTA has nothing to do with
that) — remapped to website_development, the same category as
no_website/site_unreachable.

Two new signals, both deterministic (no GPT judgement call):
- no_social_links_on_website: reuses _sie_extract_social_links (built for
  Social Intelligence Engine) against the homepage HTML already fetched
  for weakness detection — zero extra cost, lives in the shared
  _detect_voice_weaknesses so Revenue Engine gets it too.
- weak_social_presence: same extractor, applied to the Tavily social-search
  text Prospect Discovery already fetches per business — Prospect
  Discovery only (Revenue Engine's batch pipeline never calls Tavily).

Both are wired into the existing _VOICE_WEAKNESS_SERVICE_MAP /
_prospect_gap_and_angle / _voice_match_service / _apply_no_service_fit_guard
pipeline exactly like every other weakness — the "must map to a configured
service, must respect the no-service-fit guard" requirement falls out of
that existing machinery automatically, verified directly below.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import (
    _detect_voice_weaknesses,
    _sie_extract_social_links,
    _VOICE_WEAKNESS_SERVICE_MAP,
    _prospect_gap_and_angle,
    _voice_match_service,
    _apply_no_service_fit_guard,
)

_CLEAN_HTML = (
    "<html><head><title>Petite Patisserie — Fine French Pastry</title>"
    '<meta name="description" content="Award-winning French patisserie serving fresh croissants and cakes daily.">'
    '<script>gtag("config", "G-XXXX")</script>'
    "</head><body><a href='/order'>Order Now</a></body></html>"
)

_HTML_WITH_INSTAGRAM = _CLEAN_HTML.replace(
    "</body>", '<a href="https://instagram.com/petitepatisserie">Follow us</a></body>'
)


def _fetch(html):
    return {"fetch_status": "ok", "detail": "", "html": html}


def _prospect(website="https://petitepatisserie.example", rating=4.8, reviews=742, status="OPERATIONAL"):
    return {"website": website, "google_rating": rating, "total_reviews": reviews, "business_status": status}


# ── no_social_links_on_website (_detect_voice_weaknesses) ───────────────────

def test_the_exact_reported_case_website_no_social_links_flags_the_gap():
    # Petite Patisserie: 4.8 rating, 742 reviews, has a website — used to
    # come back "None detected". A clean site with no social link anywhere
    # in the HTML must now flag no_social_links_on_website.
    weaknesses, evidence = _detect_voice_weaknesses(_prospect(), _fetch(_CLEAN_HTML))
    assert "no_social_links_on_website" in weaknesses
    ev = next(e for e in evidence if e["type"] == "no_social_links_on_website")
    assert ev["confidence"] > 0
    assert ev["page"] == "homepage"


def test_a_real_social_link_on_the_homepage_clears_the_flag():
    weaknesses, _ = _detect_voice_weaknesses(_prospect(), _fetch(_HTML_WITH_INSTAGRAM))
    assert "no_social_links_on_website" not in weaknesses


def test_no_website_at_all_does_not_also_fire_the_social_link_check():
    # Homepage HTML is never fetched when there's no website — the social
    # check lives inside the `if website:` branch and must not run at all,
    # not fire a false "no social links" on top of the real no_website gap.
    weaknesses, _ = _detect_voice_weaknesses(_prospect(website=""), _fetch(""))
    assert "no_website" in weaknesses
    assert "no_social_links_on_website" not in weaknesses


def test_unreachable_site_does_not_fire_the_social_link_check_either():
    weaknesses, _ = _detect_voice_weaknesses(
        _prospect(), {"fetch_status": "unreachable", "detail": "timeout", "html": ""}
    )
    assert "site_unreachable" in weaknesses
    assert "no_social_links_on_website" not in weaknesses


# ── _sie_extract_social_links reuse sanity (the shared extractor itself) ────

def test_extractor_finds_a_real_instagram_link():
    assert "instagram" in _sie_extract_social_links('<a href="https://instagram.com/foo">IG</a>')


def test_extractor_ignores_share_widget_urls():
    # Same ignore-list this function already uses for Social Intelligence
    # Engine — a Facebook "plugins" widget link is not the business's own page.
    assert _sie_extract_social_links('<a href="https://facebook.com/plugins/like.php">Like</a>') == {}


# ── no_cta remapped to website_development, not content_creation ────────────

def test_no_cta_maps_to_website_development_not_content_creation():
    assert _VOICE_WEAKNESS_SERVICE_MAP["no_cta"] == "website_development"


def test_no_cta_pitch_goes_to_website_development_when_offered():
    gap, angle = _prospect_gap_and_angle(
        ["no_cta"],
        [{"type": "no_cta", "value": "test", "confidence": 0.55, "page": "homepage", "detected_at": "2026-01-01"}],
        ["website_development"],
    )
    assert angle == "Website Development"


def test_no_cta_no_longer_pitches_content_creation():
    gap, angle = _prospect_gap_and_angle(
        ["no_cta"],
        [{"type": "no_cta", "value": "test", "confidence": 0.55, "page": "homepage", "detected_at": "2026-01-01"}],
        ["content_creation"],
    )
    assert angle == "No service fit"


# ── The core requirement: configured vs not, warm/hot vs forced cold ────────

def _evidence(w_type, confidence=0.65):
    return {"type": w_type, "value": "test", "confidence": confidence, "page": "homepage", "detected_at": "2026-01-01"}


def _scored_prospect(recommended_service, opportunity_score=82, classification="hot"):
    return {
        "name": "Petite Patisserie", "recommended_service": recommended_service,
        "opportunity_score": opportunity_score, "suggested_opening_line": "Hi! We noticed...",
        "classification": classification,
    }


def test_social_gap_scores_warm_or_hot_for_a_tenant_that_sells_social_media_management():
    weaknesses = ["no_social_links_on_website"]
    evidence = [_evidence("no_social_links_on_website")]
    gap, angle = _prospect_gap_and_angle(weaknesses, evidence, ["social_media_management"])
    assert angle == "Social Media Management"

    # As if GPT had already scored it hot before the guard runs.
    prospects = [_scored_prospect(angle, opportunity_score=82, classification="hot")]
    _apply_no_service_fit_guard(prospects)
    assert prospects[0]["classification"] == "hot"
    assert prospects[0]["opportunity_score"] == 82
    assert prospects[0]["suggested_opening_line"] == "Hi! We noticed..."


def test_same_social_gap_still_scores_cold_for_a_tenant_that_does_not_sell_it():
    weaknesses = ["no_social_links_on_website"]
    evidence = [_evidence("no_social_links_on_website")]
    gap, angle = _prospect_gap_and_angle(weaknesses, evidence, ["paid_ads"])
    assert angle == "No service fit"

    prospects = [_scored_prospect(angle, opportunity_score=82, classification="hot")]
    _apply_no_service_fit_guard(prospects)
    assert prospects[0]["classification"] == "cold"
    assert prospects[0]["opportunity_score"] <= 25
    assert prospects[0]["suggested_opening_line"] == ""


def test_weak_social_presence_scores_warm_or_hot_when_configured():
    weaknesses = ["weak_social_presence"]
    evidence = [_evidence("weak_social_presence", confidence=0.6)]
    gap, angle = _prospect_gap_and_angle(weaknesses, evidence, ["social_media_management"])
    assert angle == "Social Media Management"

    prospects = [_scored_prospect(angle, opportunity_score=70, classification="warm")]
    _apply_no_service_fit_guard(prospects)
    assert prospects[0]["classification"] == "warm"
    assert prospects[0]["opportunity_score"] == 70


def test_weak_social_presence_still_cold_when_not_configured():
    weaknesses = ["weak_social_presence"]
    evidence = [_evidence("weak_social_presence", confidence=0.6)]
    gap, angle = _prospect_gap_and_angle(weaknesses, evidence, ["email_marketing"])
    assert angle == "No service fit"

    prospects = [_scored_prospect(angle, opportunity_score=70, classification="warm")]
    _apply_no_service_fit_guard(prospects)
    assert prospects[0]["classification"] == "cold"

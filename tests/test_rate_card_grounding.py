"""
Post-audit fix. expected_ltv (Prospect Discovery) and estimated_roi
(Revenue Engine — shown on-screen as "Expected LTV" on RevenueEnginePipeline
and "Est. ROI" on VoiceOutreachReview) were both GPT-invented rupee figures
with zero connection to what the tenant actually charges — same bug, three
screens. Both dropped from their prompts/schemas; the real guarantee is
deterministic: _ground_expected_ltv grounds the figure in the tenant's real
revenue_rate_cards price for the prospect's matched service, or reports
"not configured" — never a fabricated number, never a silent blank.

_fetch_rate_card_map is DB-touching (real local-SQLite integration test,
this codebase's convention — see test_leads_tenancy.py). _ground_expected_ltv
and _cap_closing_probability are pure — direct unit tests, no mocking.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from main import (
    _fetch_rate_card_map,
    _ground_expected_ltv,
    _cap_closing_probability,
    engine,
)

TENANT_A = "test-tenant-a-ratecard"
TENANT_B = "test-tenant-b-ratecard"


def _cleanup():
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM revenue_rate_cards WHERE user_id IN (:a, :b)"), {"a": TENANT_A, "b": TENANT_B})


def setup_function():
    _cleanup()


def teardown_function():
    _cleanup()


# ── _fetch_rate_card_map (real DB) ───────────────────────────────────────────

def test_fetch_rate_card_map_empty_for_tenant_with_no_rate_card():
    assert _fetch_rate_card_map(TENANT_A) == {}


def test_fetch_rate_card_map_empty_for_falsy_user_id():
    assert _fetch_rate_card_map("") == {}
    assert _fetch_rate_card_map(None) == {}


def test_fetch_rate_card_map_returns_real_prices():
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO revenue_rate_cards (user_id, service_key, price_micros, currency, unit, updated_at) "
            "VALUES (:uid, 'paid_ads', 25000000000, 'INR', 'month', '2026-01-01')"
        ), {"uid": TENANT_A})
    rate_card = _fetch_rate_card_map(TENANT_A)
    assert rate_card == {"paid_ads": (25000000000, "INR", "month")}


def test_fetch_rate_card_map_is_tenant_scoped():
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO revenue_rate_cards (user_id, service_key, price_micros, currency, unit, updated_at) "
            "VALUES (:uid, 'seo', 15000000000, 'INR', 'month', '2026-01-01')"
        ), {"uid": TENANT_B})
    # Tenant A's rate card must not see Tenant B's price.
    assert _fetch_rate_card_map(TENANT_A) == {}
    assert "seo" in _fetch_rate_card_map(TENANT_B)


# ── _ground_expected_ltv ──────────────────────────────────────────────────────

def test_the_core_guarantee_no_rate_card_never_produces_a_number():
    # This is the exact scenario the four frontend surfaces (ProspectDiscovery
    # card, its copy-to-clipboard text, RevenueEnginePipeline, VoiceOutreachReview)
    # all depend on: a tenant with nothing configured gets None, never a
    # fabricated figure, for ANY matched service.
    empty_rate_card = _fetch_rate_card_map(TENANT_A)  # real empty table
    for service_key in ("paid_ads", "seo", "website_development", "social_media_management"):
        ltv, configured = _ground_expected_ltv(service_key, empty_rate_card)
        assert ltv is None
        assert configured is False


def test_no_matched_service_is_not_configured():
    ltv, configured = _ground_expected_ltv(None, {"paid_ads": (25000000000, "INR", "month")})
    assert ltv is None
    assert configured is False


def test_matched_service_with_no_price_set_is_not_configured():
    # Tenant has SOME prices set, just not for this particular service.
    ltv, configured = _ground_expected_ltv("seo", {"paid_ads": (25000000000, "INR", "month")})
    assert ltv is None
    assert configured is False


def test_matched_service_with_a_real_price_is_grounded():
    ltv, configured = _ground_expected_ltv("paid_ads", {"paid_ads": (25000000000, "INR", "month")})
    assert configured is True
    assert ltv == "₹25,000/month"


def test_price_formatting_uses_real_micros_conversion():
    ltv, configured = _ground_expected_ltv("seo", {"seo": (7500000000, "INR", "month")})
    assert configured is True
    assert ltv == "₹7,500/month"


def test_non_inr_currency_is_labeled_not_assumed_rupees():
    ltv, configured = _ground_expected_ltv("seo", {"seo": (100000000, "USD", "month")})
    assert configured is True
    assert ltv == "USD 100/month"


def test_unit_is_preserved_not_hardcoded_to_month():
    ltv, configured = _ground_expected_ltv("content_creation", {"content_creation": (5000000000, "INR", "project")})
    assert configured is True
    assert "project" in ltv


# ── _cap_closing_probability ─────────────────────────────────────────────────

def test_a_cold_prospect_never_shows_a_high_closing_probability():
    # The exact bug this guards against: a prospect scored cold (well
    # under 50) must never display a confident-sounding closing chance.
    capped = _cap_closing_probability("75%", 25)
    assert capped == "25%"


def test_closing_probability_within_the_score_is_left_alone():
    capped = _cap_closing_probability("40%", 80)
    assert capped == "40%"


def test_closing_probability_exactly_at_the_score_is_left_alone():
    capped = _cap_closing_probability("60%", 60)
    assert capped == "60%"


def test_missing_opportunity_score_caps_to_zero():
    capped = _cap_closing_probability("75%", None)
    assert capped == "0%"


def test_non_numeric_raw_value_is_returned_unchanged_not_a_crash():
    assert _cap_closing_probability("unknown", 80) == "unknown"
    assert _cap_closing_probability(None, 80) is None


def test_zero_opportunity_score_caps_any_positive_probability_to_zero():
    assert _cap_closing_probability("15%", 0) == "0%"

"""
Google Ads Automated Optimization Loop (Phase 1: approval-only) — rule
threshold tests. Every rule is a pure function: real numbers in, a
recommendation dict or None out, no Ads API call, no GPT/Claude call. These
tests exercise every threshold boundary explicitly (>50%/<=50%,
>30%/<=30%, the guardrail caps binding vs not binding) since a rule that's
off-by-one on its own threshold is exactly the kind of bug that would
either spam false recommendations or silently miss real ones.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import (
    _gads_rule_raise_cpc, _gads_rule_raise_budget, _gads_rule_low_search_volume,
    _gads_rule_disapproved_ads, _gads_rule_conversion_tracking, _gads_rule_underdelivery,
    _gads_rule_negative_keyword, _gads_negative_keyword_candidates,
    _gads_compute_outcome_comparison,
)

MAX_CPC_CAP = 50_000_000   # ₹50
MAX_BUDGET_CAP = 500_000_000  # ₹500
STEP_PCT = 30


# ── raise_cpc ─────────────────────────────────────────────────────────────

def test_raise_cpc_fires_when_rank_lost_share_exceeds_50_pct():
    rec = _gads_rule_raise_cpc("skin clinic jaipur", "customers/1/adGroupCriteria/2~3", 5_000_000, 20_000_000, 0.51, MAX_CPC_CAP, STEP_PCT)
    assert rec is not None
    assert rec["type"] == "raise_cpc"
    assert rec["severity"] == "high"


def test_raise_cpc_does_not_fire_at_exactly_50_pct():
    rec = _gads_rule_raise_cpc("x", "rn", 5_000_000, 20_000_000, 0.5, MAX_CPC_CAP, STEP_PCT)
    assert rec is None


def test_raise_cpc_does_not_fire_below_threshold():
    rec = _gads_rule_raise_cpc("x", "rn", 5_000_000, 20_000_000, 0.2, MAX_CPC_CAP, STEP_PCT)
    assert rec is None


def test_raise_cpc_does_not_fire_when_rank_lost_share_is_none():
    rec = _gads_rule_raise_cpc("x", "rn", 5_000_000, 20_000_000, None, MAX_CPC_CAP, STEP_PCT)
    assert rec is None


def test_raise_cpc_proposed_value_is_capped_at_max_bid_change_pct():
    # current=10, step cap=13 (30%), top-of-page=30 (no bind — and current
    # is 33% of it, above the 25% direct-jump threshold), CPC cap=50 (no bind)
    rec = _gads_rule_raise_cpc("x", "rn", 10_000_000, 30_000_000, 0.9, MAX_CPC_CAP, STEP_PCT)
    assert int(rec["proposed_value"]) == 13_000_000
    assert rec["supporting_metrics"]["direct_jump_to_estimate"] is False


def test_raise_cpc_proposed_value_is_capped_at_top_of_page_bid_when_lower_than_step():
    # step cap=13, top-of-page=11 (binds), CPC cap=50 (no bind)
    rec = _gads_rule_raise_cpc("x", "rn", 10_000_000, 11_000_000, 0.9, MAX_CPC_CAP, STEP_PCT)
    assert int(rec["proposed_value"]) == 11_000_000


def test_raise_cpc_proposed_value_is_capped_at_user_cpc_cap_when_lowest():
    rec = _gads_rule_raise_cpc("x", "rn", 40_000_000, 100_000_000, 0.9, MAX_CPC_CAP, STEP_PCT)
    assert int(rec["proposed_value"]) == MAX_CPC_CAP  # step would be 52M, cap wins at 50M


def test_raise_cpc_never_fires_if_already_at_or_above_the_cap():
    rec = _gads_rule_raise_cpc("x", "rn", MAX_CPC_CAP, 100_000_000, 0.9, MAX_CPC_CAP, STEP_PCT)
    assert rec is None  # capped proposal == current, no real increase possible


def test_raise_cpc_missing_top_of_page_estimate_still_works_with_just_the_step_and_user_cap():
    rec = _gads_rule_raise_cpc("x", "rn", 10_000_000, None, 0.9, MAX_CPC_CAP, STEP_PCT)
    assert int(rec["proposed_value"]) == 13_000_000


# ── raise_cpc: direct jump when far below the top-of-page estimate ─────────

def test_raise_cpc_jumps_directly_to_estimate_when_current_bid_is_under_25pct_of_it():
    # current=2M is 20% of top-of-page=10M — under the 25% threshold, so this
    # should jump straight to 10M instead of a 30% step (which would only be 2.6M).
    rec = _gads_rule_raise_cpc("x", "rn", 2_000_000, 10_000_000, 0.9, MAX_CPC_CAP, STEP_PCT)
    assert int(rec["proposed_value"]) == 10_000_000
    assert rec["supporting_metrics"]["direct_jump_to_estimate"] is True
    assert "[Direct jump]" in rec["reason"]


def test_raise_cpc_direct_jump_is_still_capped_by_the_user_cpc_cap():
    # Same as above, but the user's CPC cap (6M) is lower than the top-of-page
    # estimate (10M) — the cap must still win.
    rec = _gads_rule_raise_cpc("x", "rn", 2_000_000, 10_000_000, 0.9, 6_000_000, STEP_PCT)
    assert int(rec["proposed_value"]) == 6_000_000
    assert rec["supporting_metrics"]["direct_jump_to_estimate"] is True


def test_raise_cpc_does_not_direct_jump_at_exactly_25pct_of_the_estimate():
    # current=2.5M is EXACTLY 25% of top-of-page=10M — "below 25%" excludes
    # the boundary, so this should fall back to the normal 30% step.
    rec = _gads_rule_raise_cpc("x", "rn", 2_500_000, 10_000_000, 0.9, MAX_CPC_CAP, STEP_PCT)
    assert rec["supporting_metrics"]["direct_jump_to_estimate"] is False
    assert int(rec["proposed_value"]) == 3_250_000  # 2.5M * 1.30
    assert "[Direct jump]" not in rec["reason"]


def test_raise_cpc_direct_jump_supporting_metrics_carry_the_raw_micros_value():
    # The apply-time guardrail re-check reads this exact field back out of
    # supporting_metrics, so it must be the raw int, not just the rounded
    # INR display value.
    rec = _gads_rule_raise_cpc("x", "rn", 2_000_000, 10_000_000, 0.9, MAX_CPC_CAP, STEP_PCT)
    assert rec["supporting_metrics"]["top_of_page_bid_high_micros"] == 10_000_000


def test_raise_cpc_zero_current_bid_is_not_a_crash():
    assert _gads_rule_raise_cpc("x", "rn", 0, 20_000_000, 0.9, MAX_CPC_CAP, STEP_PCT) is None


# ── raise_budget ──────────────────────────────────────────────────────────

def test_raise_budget_fires_above_30_pct_threshold():
    rec = _gads_rule_raise_budget("customers/1/campaignBudgets/2", 10_000_000, 0.31, MAX_BUDGET_CAP, STEP_PCT)
    assert rec is not None
    assert rec["type"] == "raise_budget"


def test_raise_budget_does_not_fire_at_exactly_30_pct():
    rec = _gads_rule_raise_budget("rn", 10_000_000, 0.3, MAX_BUDGET_CAP, STEP_PCT)
    assert rec is None


def test_raise_budget_proposed_value_capped_at_step_pct():
    rec = _gads_rule_raise_budget("rn", 100_000_000, 0.5, MAX_BUDGET_CAP, STEP_PCT)
    assert int(rec["proposed_value"]) == 130_000_000


def test_raise_budget_proposed_value_capped_at_user_budget_cap():
    rec = _gads_rule_raise_budget("rn", 480_000_000, 0.5, MAX_BUDGET_CAP, STEP_PCT)
    assert int(rec["proposed_value"]) == MAX_BUDGET_CAP  # step would exceed 500M cap


def test_raise_budget_never_fires_at_or_above_cap():
    rec = _gads_rule_raise_budget("rn", MAX_BUDGET_CAP, 0.9, MAX_BUDGET_CAP, STEP_PCT)
    assert rec is None


# ── low_search_volume → add_phrase_match ─────────────────────────────────

def test_low_search_volume_fires_for_an_exact_match_keyword():
    rec = _gads_rule_low_search_volume("rare term", "customers/1/adGroups/2", "EXACT", "LOW_SEARCH_VOLUME")
    assert rec is not None
    assert rec["type"] == "add_phrase_match"


def test_low_search_volume_does_not_fire_for_a_normal_serving_keyword():
    assert _gads_rule_low_search_volume("x", "rn", "EXACT", "SERVING") is None


def test_low_search_volume_does_not_fire_for_an_already_phrase_match_keyword():
    # Nothing to widen — already broader than exact.
    assert _gads_rule_low_search_volume("x", "rn", "PHRASE", "LOW_SEARCH_VOLUME") is None


def test_low_search_volume_does_not_fire_for_broad_match():
    assert _gads_rule_low_search_volume("x", "rn", "BROAD", "LOW_SEARCH_VOLUME") is None


# ── disapproved ads / conversion tracking (alert-only) ───────────────────

def test_disapproved_ads_fires_with_a_real_count():
    rec = _gads_rule_disapproved_ads(2, "12345")
    assert rec is not None
    assert rec["severity"] == "high"
    assert rec["current_value"] == "2"


def test_disapproved_ads_does_not_fire_at_zero():
    assert _gads_rule_disapproved_ads(0, "12345") is None


def test_conversion_tracking_fires_when_zero_active_conversions():
    rec = _gads_rule_conversion_tracking(0, "12345")
    assert rec is not None
    assert rec["type"] == "fix_conversion_tracking"


def test_conversion_tracking_does_not_fire_with_at_least_one_active():
    assert _gads_rule_conversion_tracking(1, "12345") is None


# ── under_delivery_alert ──────────────────────────────────────────────────

def test_underdelivery_fires_for_two_consecutive_low_spend_days():
    daily_budget = 1000 * 1_000_000  # Rs.1000/day
    recent = [50 * 1_000_000, 100 * 1_000_000]  # both well under 20% (Rs.200)
    rec = _gads_rule_underdelivery(recent, daily_budget, "12345")
    assert rec is not None
    assert rec["type"] == "under_delivery_alert"


def test_underdelivery_does_not_fire_if_only_the_most_recent_day_is_low():
    daily_budget = 1000 * 1_000_000
    recent = [900 * 1_000_000, 50 * 1_000_000]  # first day normal, second low
    assert _gads_rule_underdelivery(recent, daily_budget, "12345") is None


def test_underdelivery_does_not_fire_right_at_the_20_pct_threshold():
    daily_budget = 1000 * 1_000_000
    threshold = daily_budget * 0.2
    recent = [threshold, threshold]  # exactly at threshold, not under it
    assert _gads_rule_underdelivery(recent, daily_budget, "12345") is None


def test_underdelivery_needs_at_least_two_days_of_data():
    assert _gads_rule_underdelivery([10 * 1_000_000], 1000 * 1_000_000, "12345") is None
    assert _gads_rule_underdelivery([], 1000 * 1_000_000, "12345") is None


def test_underdelivery_only_looks_at_the_last_two_days_of_a_longer_series():
    daily_budget = 1000 * 1_000_000
    # Earlier days spent fine; only the most recent 2 are under-delivering.
    recent = [900 * 1_000_000, 950 * 1_000_000, 50 * 1_000_000, 60 * 1_000_000]
    rec = _gads_rule_underdelivery(recent, daily_budget, "12345")
    assert rec is not None


# ── negative keyword candidate pre-filter (deterministic, before any Claude call) ──

def test_negative_keyword_candidates_requires_real_spend_and_zero_conversions():
    terms = [
        {"search_term": "relevant term", "cost_micros": 5_000_000, "conversions": 0},
        {"search_term": "converted term", "cost_micros": 5_000_000, "conversions": 1},
        {"search_term": "no spend term", "cost_micros": 0, "conversions": 0},
    ]
    candidates = _gads_negative_keyword_candidates(terms)
    assert [c["search_term"] for c in candidates] == ["relevant term"]


def test_negative_keyword_rule_fires_only_when_claude_judged_not_relevant():
    rec = _gads_rule_negative_keyword("plumber jobs", 5_000_000, "customers/1/adGroups/2", {"relevant": False, "reasoning": "different intent"})
    assert rec is not None
    assert rec["type"] == "add_negative_keyword"
    assert rec["proposed_value"] == "plumber jobs"


def test_negative_keyword_rule_does_not_fire_when_claude_judged_relevant():
    assert _gads_rule_negative_keyword("plumber near me", 5_000_000, "rn", {"relevant": True, "reasoning": "on-topic"}) is None


def test_negative_keyword_rule_defaults_to_not_firing_on_missing_classification():
    # safe_default from _gads_classify_search_term_relevance is relevant=True
    assert _gads_rule_negative_keyword("x", 5_000_000, "rn", {"relevant": True, "reasoning": "could not classify"}) is None


# ── outcome comparison (pure) ────────────────────────────────────────────

def test_outcome_comparison_computes_real_deltas():
    before = {"impressions": 100, "clicks": 10, "cost_micros": 50_000_000, "conversions": 1}
    after = {"impressions": 150, "clicks": 20, "cost_micros": 60_000_000, "conversions": 2}
    result = _gads_compute_outcome_comparison(before, after)
    assert result["impressions_change_pct"] == 50.0
    assert result["clicks_change_pct"] == 100.0
    assert result["conversions_change"] == 1


def test_outcome_flags_worsened_when_clicks_drop_and_cpc_rises():
    before = {"impressions": 100, "clicks": 20, "cost_micros": 40_000_000, "conversions": 2}
    after = {"impressions": 100, "clicks": 5, "cost_micros": 30_000_000, "conversions": 0}
    result = _gads_compute_outcome_comparison(before, after)
    assert result["worsened"] is True


def test_outcome_does_not_flag_worsened_for_a_small_click_dip_alone():
    # Real noise, not a regression — clicks down only slightly, CPC steady, conversions steady.
    before = {"impressions": 100, "clicks": 20, "cost_micros": 40_000_000, "conversions": 2}
    after = {"impressions": 100, "clicks": 19, "cost_micros": 38_000_000, "conversions": 2}
    result = _gads_compute_outcome_comparison(before, after)
    assert result["worsened"] is False


def test_outcome_does_not_flag_worsened_when_clicks_and_conversions_both_improve():
    before = {"impressions": 100, "clicks": 10, "cost_micros": 40_000_000, "conversions": 1}
    after = {"impressions": 200, "clicks": 25, "cost_micros": 80_000_000, "conversions": 3}
    result = _gads_compute_outcome_comparison(before, after)
    assert result["worsened"] is False


def test_outcome_handles_zero_before_clicks_without_a_crash():
    before = {"impressions": 10, "clicks": 0, "cost_micros": 0, "conversions": 0}
    after = {"impressions": 20, "clicks": 5, "cost_micros": 10_000_000, "conversions": 1}
    result = _gads_compute_outcome_comparison(before, after)
    assert result["impressions_change_pct"] == 100.0
    assert result["clicks_change_pct"] is None  # can't compute a % change from a zero base

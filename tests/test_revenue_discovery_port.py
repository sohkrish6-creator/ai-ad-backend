"""
Legacy Prospect Discovery UX ported into the Revenue Engine Pipeline —
industry/city dropdowns, Hot/Warm/Cold tabs, a per-prospect suggested
opening line with Copy, and "Find More". Reuses Revenue Engine's own
_score_voice_prospect_batch/_run_voice_batch_job (extended, not copied)
and its own voice_prospects store — per instruction, legacy's separate
scoring code (_score_batch) was never touched or reused.

_apply_revenue_no_service_fit_guard is the new surface area's defense-in-
depth guard — suggested_opening_line is a new field on an existing,
previously-safe scoring call, so it gets the exact same "never trust the
prompt alone" treatment already proven necessary for Prospect Discovery's
own suggested_opening_line (_apply_no_service_fit_guard) and Outreach AI's
service pitching (_apply_outreach_service_fit_guard) — same bug class,
third occurrence, same fix.

_revenue_previously_discovered_place_ids is a real DB-touching dedup query
— real local-SQLite integration test, this codebase's convention.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from main import (
    engine, _apply_revenue_no_service_fit_guard, _revenue_previously_discovered_place_ids,
    _PROSPECT_NO_SERVICE_FIT_SCORE_CAP,
)

TEST_UID = "test-uid-revenue-discovery-port"


def setup_function():
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM voice_prospects WHERE user_id=:uid"), {"uid": TEST_UID})
        conn.execute(text("DELETE FROM voice_batches WHERE user_id=:uid"), {"uid": TEST_UID})


def teardown_function():
    setup_function()


# ── _apply_revenue_no_service_fit_guard ──────────────────────────────────────

def _to_scan_item(name, matched_service_label=None):
    return {"business_name": name, "matched_service_label": matched_service_label}


def _score(opportunity_score=90, priority="high", suggested_opening_line="Hi! We noticed..."):
    return {
        "business_name": "x", "opportunity_score": opportunity_score, "priority": priority,
        "suggested_opening_line": suggested_opening_line,
    }


def test_no_service_fit_clears_opening_line_caps_score_and_priority():
    to_scan = [_to_scan_item("Hotel A", matched_service_label=None)]
    scores_by_name = {"Hotel A": _score(opportunity_score=92, priority="high")}
    _apply_revenue_no_service_fit_guard(scores_by_name, to_scan)
    s = scores_by_name["Hotel A"]
    assert s["suggested_opening_line"] == ""
    assert s["opportunity_score"] == _PROSPECT_NO_SERVICE_FIT_SCORE_CAP
    assert s["priority"] == "low"


def test_a_real_service_fit_is_left_completely_untouched():
    to_scan = [_to_scan_item("Hotel B", matched_service_label="Website Development")]
    scores_by_name = {"Hotel B": _score(opportunity_score=88, priority="high", suggested_opening_line="Real pitch")}
    _apply_revenue_no_service_fit_guard(scores_by_name, to_scan)
    s = scores_by_name["Hotel B"]
    assert s["suggested_opening_line"] == "Real pitch"
    assert s["opportunity_score"] == 88
    assert s["priority"] == "high"


def test_score_already_below_cap_is_not_raised():
    to_scan = [_to_scan_item("Hotel C", matched_service_label=None)]
    scores_by_name = {"Hotel C": _score(opportunity_score=10, priority="low")}
    _apply_revenue_no_service_fit_guard(scores_by_name, to_scan)
    assert scores_by_name["Hotel C"]["opportunity_score"] == 10


def test_missing_score_entry_is_not_a_crash():
    to_scan = [_to_scan_item("Hotel D", matched_service_label=None)]
    _apply_revenue_no_service_fit_guard({}, to_scan)  # must not raise


def test_mixed_batch_only_no_fit_entries_are_touched():
    to_scan = [
        _to_scan_item("Hotel E", matched_service_label=None),
        _to_scan_item("Hotel F", matched_service_label="SEO"),
    ]
    scores_by_name = {
        "Hotel E": _score(opportunity_score=95, priority="high", suggested_opening_line="invented"),
        "Hotel F": _score(opportunity_score=80, priority="high", suggested_opening_line="real"),
    }
    _apply_revenue_no_service_fit_guard(scores_by_name, to_scan)
    assert scores_by_name["Hotel E"]["suggested_opening_line"] == ""
    assert scores_by_name["Hotel E"]["priority"] == "low"
    assert scores_by_name["Hotel F"]["suggested_opening_line"] == "real"
    assert scores_by_name["Hotel F"]["priority"] == "high"


# ── _revenue_previously_discovered_place_ids ─────────────────────────────────

def _insert_batch(batch_id, industry, city):
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO voice_batches (id, user_id, industry, city, max_prospects, status, progress_pct, "
            "current_step, created_at, channel) "
            "VALUES (:id, :uid, :industry, :city, 15, 'succeeded', 100, 'Done', '2026-01-01', 'revenue_engine')"
        ), {"id": batch_id, "uid": TEST_UID, "industry": industry, "city": city})


def _insert_prospect_for_batch(batch_id, place_id):
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO voice_prospects (batch_id, user_id, place_id, business_name, channel, created_at) "
            "VALUES (:bid, :uid, :pid, 'Test Biz', 'revenue_engine', '2026-01-01')"
        ), {"bid": batch_id, "uid": TEST_UID, "pid": place_id})


def test_no_prior_batches_returns_empty_set():
    assert _revenue_previously_discovered_place_ids(TEST_UID, "hotels", "Jaipur") == set()


def test_place_ids_from_a_prior_batch_for_the_same_segment_are_returned():
    _insert_batch("batch-1", "hotels", "Jaipur")
    _insert_prospect_for_batch("batch-1", "place-a")
    _insert_prospect_for_batch("batch-1", "place-b")
    assert _revenue_previously_discovered_place_ids(TEST_UID, "hotels", "Jaipur") == {"place-a", "place-b"}


def test_a_different_city_does_not_leak_into_the_dedup_set():
    _insert_batch("batch-2", "hotels", "Udaipur")
    _insert_prospect_for_batch("batch-2", "place-c")
    assert _revenue_previously_discovered_place_ids(TEST_UID, "hotels", "Jaipur") == set()


def test_a_different_industry_does_not_leak_into_the_dedup_set():
    _insert_batch("batch-3", "restaurants", "Jaipur")
    _insert_prospect_for_batch("batch-3", "place-d")
    assert _revenue_previously_discovered_place_ids(TEST_UID, "hotels", "Jaipur") == set()

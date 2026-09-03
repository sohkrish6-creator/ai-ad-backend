"""
Revenue Engine Phase 2 (the "Revenue Dashboard"/"Today's Priorities" work).
Covers the three new pieces: compute_next_best_action (validated schema,
deterministic — no GPT), the channel-agnostic suppression table/check, and
_revenue_dashboard_metrics (the 6 header tiles, each either a real computed
number or an explicit "not available" — never a fabricated zero).

DB-touching pieces get a real local-SQLite integration test (matches this
codebase's established split — see test_prospect_scan_history.py's own
docstring on why). Pure logic (urgency, action_type derivation) gets a
direct unit test with hand-built dicts, no DB needed.
"""
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from main import (
    compute_next_best_action, NextBestAction, _next_best_action_urgency,
    _voice_prospect_has_pending_draft, _is_prospect_suppressed,
    _revenue_dashboard_metrics, engine,
)

TEST_UID = "test-uid-revenue-dashboard"
TEST_PLACE_ID = "place-rd-1"


def _prospect(**overrides):
    base = {
        "id": 999001, "business_name": "Test Dental Clinic", "priority": "high",
        "gate_blocked": False, "service_fit": True, "opportunity_score": 82, "need_score": 70,
        "matched_weakness": "no_website", "matched_service": "seo",
        "evidence": [{"type": "no_website", "value": "no website field", "confidence": 0.98}],
        "last_outcome_at": None,
    }
    base.update(overrides)
    return base


# ── compute_next_best_action / schema ────────────────────────────────────────

def test_call_action_is_validated_and_shaped_correctly():
    result = compute_next_best_action(_prospect(), TEST_UID)
    assert result is not None
    NextBestAction(**result)  # re-validates cleanly
    assert result["action_type"] == "CALL"
    assert result["target_route"] == "/revenue-engine/lead/999001"
    assert result["urgency"] == "high"
    assert result["evidence"][0]["type"] == "no_website"


def test_gate_blocked_prospect_is_ignore():
    result = compute_next_best_action(_prospect(gate_blocked=True), TEST_UID)
    assert result["action_type"] == "IGNORE"
    assert result["urgency"] == "low"


def test_no_service_fit_is_ignore_even_with_high_priority():
    result = compute_next_best_action(_prospect(service_fit=False), TEST_UID)
    assert result["action_type"] == "IGNORE"


def test_reason_never_mentions_opens_views_or_reads():
    # No integration in this codebase delivers open/view/read events —
    # the reason text must never imply one did.
    result = compute_next_best_action(_prospect(), TEST_UID)
    banned = ("opened", "viewed", "read the", "clicked")
    assert not any(b in result["reason"].lower() for b in banned)


def test_evidence_confidence_defaults_when_missing():
    result = compute_next_best_action(_prospect(evidence=[{"type": "poor_reviews", "value": "2.1 stars"}]), TEST_UID)
    assert result["evidence"][0]["confidence"] == 0.0


def test_urgency_high_for_stale_high_priority():
    from datetime import datetime, timedelta
    stale = (datetime.utcnow() - timedelta(days=10)).isoformat()
    assert _next_best_action_urgency("FOLLOW_LATER", "high", stale) == "high"


def test_urgency_medium_for_fresh_medium_priority():
    from datetime import datetime
    fresh = datetime.utcnow().isoformat()
    assert _next_best_action_urgency("FOLLOW_LATER", "medium", fresh) == "medium"


def test_urgency_low_for_ignore_regardless_of_priority():
    assert _next_best_action_urgency("IGNORE", "high", None) == "low"


# ── suppression + pending drafts (real DB) ───────────────────────────────────

def _cleanup():
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM prospect_suppressions WHERE user_id=:uid"), {"uid": TEST_UID})
        conn.execute(text("DELETE FROM voice_dnc_list WHERE user_id=:uid"), {"uid": TEST_UID})
        conn.execute(text("DELETE FROM revenue_outreach_drafts WHERE user_id=:uid"), {"uid": TEST_UID})
        conn.execute(text("DELETE FROM revenue_rate_cards WHERE user_id=:uid"), {"uid": TEST_UID})
        conn.execute(text("DELETE FROM voice_prospects WHERE user_id=:uid"), {"uid": TEST_UID})


def setup_function():
    _cleanup()


def teardown_function():
    _cleanup()


def test_suppressed_place_id_blocks_all_channels_by_default():
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO prospect_suppressions (user_id, place_id, channel, reason, source, created_at) "
            "VALUES (:uid, :pid, 'all', 'requested removal', 'manual', '2026-01-01')"
        ), {"uid": TEST_UID, "pid": TEST_PLACE_ID})
    assert _is_prospect_suppressed(TEST_UID, TEST_PLACE_ID, channel="email") is True
    assert _is_prospect_suppressed(TEST_UID, TEST_PLACE_ID, channel="phone") is True


def test_channel_specific_suppression_does_not_block_other_channels():
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO prospect_suppressions (user_id, place_id, channel, reason, source, created_at) "
            "VALUES (:uid, :pid, 'email', 'bounced', 'manual', '2026-01-01')"
        ), {"uid": TEST_UID, "pid": TEST_PLACE_ID})
    assert _is_prospect_suppressed(TEST_UID, TEST_PLACE_ID, channel="email") is True
    assert _is_prospect_suppressed(TEST_UID, TEST_PLACE_ID, channel="phone") is False


def test_voice_dnc_list_still_works_unmigrated():
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO voice_dnc_list (user_id, phone_e164, reason, created_at) VALUES (:uid, :p, 'requested', '2026-01-01')"
        ), {"uid": TEST_UID, "p": "+911234567890"})
    assert _is_prospect_suppressed(TEST_UID, "some-other-place-id", phone_e164="+911234567890") is True


def test_not_suppressed_returns_false():
    assert _is_prospect_suppressed(TEST_UID, "unsuppressed-place-id") is False


def test_pending_draft_detected():
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO revenue_outreach_drafts (prospect_id, user_id, channel, draft_json, status, created_at) "
            "VALUES (999002, :uid, 'email', '{}', 'draft', '2026-01-01')"
        ), {"uid": TEST_UID})
    assert _voice_prospect_has_pending_draft(TEST_UID, 999002) is True


def test_sent_draft_does_not_count_as_pending():
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO revenue_outreach_drafts (prospect_id, user_id, channel, draft_json, status, sent_at, created_at) "
            "VALUES (999003, :uid, 'email', '{}', 'sent', '2026-01-02', '2026-01-01')"
        ), {"uid": TEST_UID})
    assert _voice_prospect_has_pending_draft(TEST_UID, 999003) is False


# ── dashboard metrics ─────────────────────────────────────────────────────────

def _insert_prospect(place_id, priority, opportunity_score, matched_service, last_outcome=None):
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO voice_prospects (batch_id, user_id, place_id, business_name, priority, "
            "opportunity_score, matched_service, gate_blocked, approval_status, service_fit, channel, "
            "last_outcome, created_at) "
            "VALUES ('test-batch', :uid, :pid, 'Test Biz', :pr, :score, :svc, FALSE, 'pending', TRUE, "
            "'revenue_engine', :lo, '2026-01-01')"
        ), {"uid": TEST_UID, "pid": place_id, "pr": priority, "score": opportunity_score, "svc": matched_service, "lo": last_outcome})


def test_pipeline_value_unavailable_with_no_rate_card():
    _insert_prospect("p1", "high", 80, "seo")
    with engine.connect() as conn:
        metrics = _revenue_dashboard_metrics(conn, TEST_UID)
    assert metrics["pipeline_value"]["available"] is False
    assert metrics["pipeline_value"]["missing_module_route"] == "/revenue-engine/settings"


def test_pipeline_value_computed_from_real_rate_card():
    _insert_prospect("p1", "high", 80, "seo")
    _insert_prospect("p2", "medium", 60, "seo")
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO revenue_rate_cards (user_id, service_key, price_micros, updated_at) "
            "VALUES (:uid, 'seo', 25000000000, '2026-01-01')"
        ), {"uid": TEST_UID})
    with engine.connect() as conn:
        metrics = _revenue_dashboard_metrics(conn, TEST_UID)
    assert metrics["pipeline_value"]["available"] is True
    assert metrics["pipeline_value"]["amount_micros"] == 50000000000  # 2 prospects x 25000000000


def test_open_and_high_priority_counts():
    _insert_prospect("p1", "high", 80, "seo")
    _insert_prospect("p2", "medium", 60, "seo")
    _insert_prospect("p3", "high", 90, "seo", last_outcome="not_interested")  # closed-lost, excluded
    with engine.connect() as conn:
        metrics = _revenue_dashboard_metrics(conn, TEST_UID)
    assert metrics["open_opportunities"]["count"] == 2
    assert metrics["high_priority_prospects"]["count"] == 1


def test_suppressed_prospect_excluded_from_metrics_not_just_the_ranked_list():
    # Consistency fix: a business you've been told not to contact must not
    # inflate "Open Opportunities"/"Pipeline Value" either, not just be
    # absent from the ranked Today's Priorities list.
    _insert_prospect("p1", "high", 80, "seo")  # counted
    _insert_prospect(TEST_PLACE_ID, "high", 95, "seo")  # will be suppressed
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO prospect_suppressions (user_id, place_id, channel, reason, source, created_at) "
            "VALUES (:uid, :pid, 'all', 'requested removal', 'manual', '2026-01-01')"
        ), {"uid": TEST_UID, "pid": TEST_PLACE_ID})
        conn.execute(text(
            "INSERT INTO revenue_rate_cards (user_id, service_key, price_micros, updated_at) "
            "VALUES (:uid, 'seo', 10000000000, '2026-01-01')"
        ), {"uid": TEST_UID})
    with engine.connect() as conn:
        metrics = _revenue_dashboard_metrics(conn, TEST_UID)
    assert metrics["open_opportunities"]["count"] == 1
    assert metrics["high_priority_prospects"]["count"] == 1
    assert metrics["pipeline_value"]["amount_micros"] == 10000000000  # only the non-suppressed prospect counted


def test_dnc_listed_prospect_excluded_from_metrics():
    _insert_prospect("p1", "high", 80, "seo")
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE voice_prospects SET phone_e164=:phone WHERE user_id=:uid AND place_id='p1'"
        ), {"phone": "+919999999999", "uid": TEST_UID})
        conn.execute(text(
            "INSERT INTO voice_dnc_list (user_id, phone_e164, reason, created_at) VALUES (:uid, :p, 'requested', '2026-01-01')"
        ), {"uid": TEST_UID, "p": "+919999999999"})
    with engine.connect() as conn:
        metrics = _revenue_dashboard_metrics(conn, TEST_UID)
    assert metrics["open_opportunities"]["count"] == 0


def test_followups_due_counts_unsent_drafts_only():
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO revenue_outreach_drafts (prospect_id, user_id, channel, draft_json, status, created_at) "
            "VALUES (1, :uid, 'email', '{}', 'draft', '2026-01-01')"
        ), {"uid": TEST_UID})
        conn.execute(text(
            "INSERT INTO revenue_outreach_drafts (prospect_id, user_id, channel, draft_json, status, sent_at, created_at) "
            "VALUES (2, :uid, 'email', '{}', 'sent', '2026-01-02', '2026-01-01')"
        ), {"uid": TEST_UID})
    with engine.connect() as conn:
        metrics = _revenue_dashboard_metrics(conn, TEST_UID)
    assert metrics["followups_due"]["count"] == 1


def test_meetings_and_proposals_are_always_explicit_empty_states():
    # No backing data exists anywhere in the schema for either — must never
    # silently become a fabricated number if someone adds an unrelated
    # column later without updating this function.
    with engine.connect() as conn:
        metrics = _revenue_dashboard_metrics(conn, TEST_UID)
    assert metrics["meetings_today"]["available"] is False
    assert metrics["proposals_pending"]["available"] is False

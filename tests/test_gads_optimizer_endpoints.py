"""
Google Ads Automated Optimization Loop — DB-backed logic and HTTP-surface
coverage: settings guardrails, recommendation upsert/dedup, and the
approve/reject/revert endpoints' auth/status-transition rules. Real
TestClient + real SQLite, matching this codebase's established convention
(test_whatsapp_outreach_sessions.py, test_reel_jobs_endpoints.py) — the
live Google Ads mutation itself is covered separately in
test_gads_optimizer_apply_revert.py with a fake network layer, since a
real Ads connection isn't available in this test environment.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jwt as pyjwt
from fastapi.testclient import TestClient
from sqlalchemy import text

from main import app, engine, _get_or_create_gads_automation_settings, _gads_upsert_recommendation

TEST_SECRET = "test-jwt-secret-for-gads-optimizer-tests-only"
TEST_UID = "test-uid-gads-optimizer"
OTHER_UID = "test-uid-gads-optimizer-other"


def _token(sub):
    return pyjwt.encode({"sub": sub}, TEST_SECRET, algorithm="HS256")


client = TestClient(app)
_AUTH = {"Authorization": f"Bearer {_token(TEST_UID)}"}
_OTHER_AUTH = {"Authorization": f"Bearer {_token(OTHER_UID)}"}


def setup_function():
    os.environ["SUPABASE_JWT_SECRET"] = TEST_SECRET
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM gads_automation_settings WHERE user_id IN (:u1, :u2)"), {"u1": TEST_UID, "u2": OTHER_UID})
        conn.execute(text("DELETE FROM gads_recommendations WHERE user_id IN (:u1, :u2)"), {"u1": TEST_UID, "u2": OTHER_UID})


def teardown_function():
    setup_function()


def _rec(**overrides):
    base = {"type": "raise_cpc", "severity": "high", "target_resource": "customers/1/adGroupCriteria/2~3",
            "current_value": "10000000", "proposed_value": "13000000", "reason": "test reason",
            "supporting_metrics": {"x": 1}}
    base.update(overrides)
    return base


# ── automation settings: get-or-create defaults ─────────────────────────────

def test_settings_defaults_are_created_on_first_read():
    settings = _get_or_create_gads_automation_settings(TEST_UID)
    assert settings["max_cpc_cap_micros"] == 50_000_000
    assert settings["max_daily_budget_cap_micros"] == 500_000_000
    assert settings["max_bid_change_pct"] == 30
    assert settings["automation_paused"] is False
    assert settings["auto_apply_enabled"] is False


def test_settings_endpoint_requires_auth():
    assert client.get("/google-ads/automation-settings").status_code == 401
    assert client.patch("/google-ads/automation-settings", json={}).status_code == 401


def test_patch_updates_cpc_cap():
    resp = client.patch("/google-ads/automation-settings", json={"max_cpc_cap_inr": 25}, headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["settings"]["max_cpc_cap_micros"] == 25_000_000


def test_patch_rejects_out_of_range_bid_change_pct():
    resp = client.patch("/google-ads/automation-settings", json={"max_bid_change_pct": 500}, headers=_AUTH)
    assert resp.status_code == 400


def test_patch_rejects_non_positive_caps():
    assert client.patch("/google-ads/automation-settings", json={"max_cpc_cap_inr": -5}, headers=_AUTH).status_code == 400
    assert client.patch("/google-ads/automation-settings", json={"max_daily_budget_cap_inr": 0}, headers=_AUTH).status_code == 400


def test_patch_can_toggle_automation_paused():
    resp = client.patch("/google-ads/automation-settings", json={"automation_paused": True}, headers=_AUTH)
    assert resp.json()["settings"]["automation_paused"] is True
    resp2 = client.patch("/google-ads/automation-settings", json={"automation_paused": False}, headers=_AUTH)
    assert resp2.json()["settings"]["automation_paused"] is False


def test_auto_apply_enabled_can_be_stored_but_phase_1_never_reads_it_to_auto_apply():
    # Phase 2 placeholder — storing the flag is fine, the guarantee is that
    # nothing in the apply path (tested separately) ever consults it.
    resp = client.patch("/google-ads/automation-settings", json={"auto_apply_enabled": True}, headers=_AUTH)
    assert resp.json()["settings"]["auto_apply_enabled"] is True


def test_settings_are_isolated_per_tenant():
    client.patch("/google-ads/automation-settings", json={"max_cpc_cap_inr": 99}, headers=_AUTH)
    other_settings = _get_or_create_gads_automation_settings(OTHER_UID)
    assert other_settings["max_cpc_cap_micros"] == 50_000_000  # untouched, still the default


# ── recommendation upsert / dedup ────────────────────────────────────────────

def test_upsert_creates_a_new_pending_row():
    _gads_upsert_recommendation(TEST_UID, "999", "Test Campaign", _rec())
    with engine.connect() as conn:
        row = conn.execute(text("SELECT status, reason FROM gads_recommendations WHERE user_id=:uid AND campaign_id='999'"), {"uid": TEST_UID}).fetchone()
    assert row[0] == "pending"
    assert row[1] == "test reason"


def test_upsert_refreshes_an_existing_pending_row_instead_of_duplicating():
    _gads_upsert_recommendation(TEST_UID, "999", "Test Campaign", _rec(reason="day 1 reason"))
    _gads_upsert_recommendation(TEST_UID, "999", "Test Campaign", _rec(reason="day 2 reason", proposed_value="14000000"))
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT reason, proposed_value FROM gads_recommendations WHERE user_id=:uid AND campaign_id='999'"), {"uid": TEST_UID}).fetchall()
    assert len(rows) == 1  # no duplicate
    assert rows[0][0] == "day 2 reason"
    assert rows[0][1] == "14000000"


def test_upsert_never_overwrites_an_already_approved_row():
    _gads_upsert_recommendation(TEST_UID, "999", "Test Campaign", _rec(reason="original"))
    with engine.begin() as conn:
        conn.execute(text("UPDATE gads_recommendations SET status='applied' WHERE user_id=:uid AND campaign_id='999'"), {"uid": TEST_UID})
    _gads_upsert_recommendation(TEST_UID, "999", "Test Campaign", _rec(reason="job saw the same issue again"))
    with engine.connect() as conn:
        row = conn.execute(text("SELECT status, reason FROM gads_recommendations WHERE user_id=:uid AND campaign_id='999'"), {"uid": TEST_UID}).fetchone()
    assert row[0] == "applied"  # untouched
    assert row[1] == "original"  # not silently overwritten


def test_different_types_for_the_same_target_are_independent_rows():
    _gads_upsert_recommendation(TEST_UID, "999", "Test Campaign", _rec(type="raise_cpc"))
    _gads_upsert_recommendation(TEST_UID, "999", "Test Campaign", _rec(type="add_negative_keyword", target_resource="customers/1/adGroupCriteria/2~3"))
    with engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM gads_recommendations WHERE user_id=:uid AND campaign_id='999'"), {"uid": TEST_UID}).scalar()
    assert count == 2


# ── recommendations list/reject/approve/revert endpoints ────────────────────

def test_list_requires_auth():
    assert client.get("/google-ads/recommendations").status_code == 401


def test_list_only_returns_the_callers_own_recommendations():
    _gads_upsert_recommendation(TEST_UID, "1", "Mine", _rec())
    _gads_upsert_recommendation(OTHER_UID, "2", "Theirs", _rec())
    resp = client.get("/google-ads/recommendations", headers=_AUTH)
    campaign_ids = [r["campaign_id"] for r in resp.json()["recommendations"]]
    assert "1" in campaign_ids
    assert "2" not in campaign_ids


def test_list_can_filter_by_status():
    _gads_upsert_recommendation(TEST_UID, "1", "C", _rec(type="raise_cpc"))
    _gads_upsert_recommendation(TEST_UID, "1", "C", _rec(type="raise_budget", target_resource="customers/1/campaignBudgets/9"))
    with engine.begin() as conn:
        conn.execute(text("UPDATE gads_recommendations SET status='rejected' WHERE user_id=:uid AND type='raise_budget'"), {"uid": TEST_UID})
    resp = client.get("/google-ads/recommendations?status=pending", headers=_AUTH)
    types = [r["type"] for r in resp.json()["recommendations"]]
    assert types == ["raise_cpc"]


def test_reject_requires_auth():
    assert client.post("/google-ads/recommendations/1/reject").status_code == 401


def test_reject_marks_pending_as_rejected():
    _gads_upsert_recommendation(TEST_UID, "1", "C", _rec())
    with engine.connect() as conn:
        rec_id = conn.execute(text("SELECT id FROM gads_recommendations WHERE user_id=:uid"), {"uid": TEST_UID}).scalar()
    resp = client.post(f"/google-ads/recommendations/{rec_id}/reject", headers=_AUTH)
    assert resp.status_code == 200
    with engine.connect() as conn:
        status = conn.execute(text("SELECT status FROM gads_recommendations WHERE id=:id"), {"id": rec_id}).scalar()
    assert status == "rejected"


def test_reject_404s_for_another_tenants_recommendation():
    _gads_upsert_recommendation(OTHER_UID, "1", "C", _rec())
    with engine.connect() as conn:
        rec_id = conn.execute(text("SELECT id FROM gads_recommendations WHERE user_id=:uid"), {"uid": OTHER_UID}).scalar()
    resp = client.post(f"/google-ads/recommendations/{rec_id}/reject", headers=_AUTH)
    assert resp.status_code == 404


def test_reject_a_nonexistent_id_404s():
    assert client.post("/google-ads/recommendations/999999/reject", headers=_AUTH).status_code == 404


def test_reject_an_already_rejected_recommendation_fails_cleanly():
    _gads_upsert_recommendation(TEST_UID, "1", "C", _rec())
    with engine.connect() as conn:
        rec_id = conn.execute(text("SELECT id FROM gads_recommendations WHERE user_id=:uid"), {"uid": TEST_UID}).scalar()
    client.post(f"/google-ads/recommendations/{rec_id}/reject", headers=_AUTH)
    resp = client.post(f"/google-ads/recommendations/{rec_id}/reject", headers=_AUTH)
    assert resp.status_code == 400


def test_approve_requires_auth():
    assert client.post("/google-ads/recommendations/1/approve").status_code == 401


def test_approve_blocked_when_automation_is_paused():
    _gads_upsert_recommendation(TEST_UID, "1", "C", _rec())
    with engine.connect() as conn:
        rec_id = conn.execute(text("SELECT id FROM gads_recommendations WHERE user_id=:uid"), {"uid": TEST_UID}).scalar()
    client.patch("/google-ads/automation-settings", json={"automation_paused": True}, headers=_AUTH)
    resp = client.post(f"/google-ads/recommendations/{rec_id}/approve", headers=_AUTH)
    assert resp.status_code == 400
    assert "paused" in resp.json()["detail"].lower()


def test_approve_an_already_applied_recommendation_fails_cleanly():
    _gads_upsert_recommendation(TEST_UID, "1", "C", _rec())
    with engine.begin() as conn:
        conn.execute(text("UPDATE gads_recommendations SET status='applied' WHERE user_id=:uid"), {"uid": TEST_UID})
        rec_id = conn.execute(text("SELECT id FROM gads_recommendations WHERE user_id=:uid"), {"uid": TEST_UID}).scalar()
    resp = client.post(f"/google-ads/recommendations/{rec_id}/approve", headers=_AUTH)
    assert resp.status_code == 400


def test_revert_requires_auth():
    assert client.post("/google-ads/recommendations/1/revert").status_code == 401


def test_revert_only_allowed_on_an_applied_recommendation():
    _gads_upsert_recommendation(TEST_UID, "1", "C", _rec())  # still pending
    with engine.connect() as conn:
        rec_id = conn.execute(text("SELECT id FROM gads_recommendations WHERE user_id=:uid"), {"uid": TEST_UID}).scalar()
    resp = client.post(f"/google-ads/recommendations/{rec_id}/revert", headers=_AUTH)
    assert resp.status_code == 400
    assert "applied" in resp.json()["detail"].lower()


def test_revert_404s_for_another_tenants_recommendation():
    _gads_upsert_recommendation(OTHER_UID, "1", "C", _rec())
    with engine.connect() as conn:
        rec_id = conn.execute(text("SELECT id FROM gads_recommendations WHERE user_id=:uid"), {"uid": OTHER_UID}).scalar()
    assert client.post(f"/google-ads/recommendations/{rec_id}/revert", headers=_AUTH).status_code == 404


# ── run-all cron endpoint auth gate ──────────────────────────────────────────

def test_run_all_rejects_a_request_with_no_api_key():
    # Registered in _API_KEY_ONLY_PATHS — still requires the X-API-Key gate
    # that applies to every non-public path; it only skips the SEPARATE
    # Supabase-JWT requirement.
    resp = client.post("/google-ads/optimizer/run-all")
    assert resp.status_code in (401, 403)

"""
Bulk WhatsApp outreach (wa.me click-to-chat, no browser automation).
Session tables hold ONLY position/status — prospect data and draft content
are read live from voice_prospects/revenue_outreach_drafts, never copied.

Real end-to-end coverage via TestClient (exercises real auth_middleware +
routing, matching test_auth_middleware.py's precedent) — these endpoints do
real work across multiple tables (session/items/voice_prospects/
lead_timeline_events) in one transaction, worth testing as the actual HTTP
surface, not just the underlying functions.

Key behaviors under test:
- _whatsapp_eligibility uses last_outcome_at (the real, actively-written
  contact signal), not the dead last_contacted_at column.
- Session creation re-validates eligibility server-side — never trusts the
  client's selection.
- 'sent' reuses _revenue_mark_draft_sent_core (starts the cooldown via
  last_outcome/last_outcome_at); 'skipped'/'no_number' never touch it.
- Every action writes a lead_timeline_events row with channel=whatsapp.
- Resume: GET .../sessions/active reflects the real in-progress session.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jwt as pyjwt
from fastapi.testclient import TestClient
from sqlalchemy import text

from main import app, engine, _whatsapp_eligibility, _get_or_create_voice_settings

TEST_SECRET = "test-jwt-secret-for-whatsapp-outreach-tests-only"
TEST_UID = "test-uid-whatsapp-outreach"


def _token(sub):
    return pyjwt.encode({"sub": sub}, TEST_SECRET, algorithm="HS256")


def setup_function():
    os.environ["SUPABASE_JWT_SECRET"] = TEST_SECRET
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM whatsapp_outreach_session_items WHERE session_id IN "
                           "(SELECT id FROM whatsapp_outreach_sessions WHERE user_id=:uid)"), {"uid": TEST_UID})
        conn.execute(text("DELETE FROM whatsapp_outreach_sessions WHERE user_id=:uid"), {"uid": TEST_UID})
        conn.execute(text("DELETE FROM voice_prospects WHERE user_id=:uid"), {"uid": TEST_UID})
        conn.execute(text("DELETE FROM voice_dnc_list WHERE user_id=:uid"), {"uid": TEST_UID})
        conn.execute(text("DELETE FROM revenue_outreach_drafts WHERE user_id=:uid"), {"uid": TEST_UID})
        conn.execute(text("DELETE FROM lead_timeline_events WHERE user_id=:uid"), {"uid": TEST_UID})
        conn.execute(text("DELETE FROM leads WHERE user_id=:uid"), {"uid": TEST_UID})
    _get_or_create_voice_settings(TEST_UID)  # ensures the row exists
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE voice_settings SET cooldown_days=7, business_name='Test Agency' WHERE user_id=:uid"
        ), {"uid": TEST_UID})


def teardown_function():
    setup_function()
    os.environ.pop("SUPABASE_JWT_SECRET", None)


client = TestClient(app)
_AUTH = {"Authorization": f"Bearer {_token(TEST_UID)}"}


def _insert_prospect(pid, phone_e164="+919828676825", last_outcome_at=None, matched_weakness="no_website",
                      opportunity_score=80, business_name="Test Biz"):
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO voice_prospects (id, batch_id, user_id, place_id, business_name, phone_e164, "
            "priority, opportunity_score, matched_weakness, gate_blocked, approval_status, service_fit, "
            "channel, last_outcome_at, created_at) "
            "VALUES (:id, 'test-batch', :uid, :place, :biz, :phone, 'high', :score, :mw, FALSE, 'pending', "
            "TRUE, 'revenue_engine', :loa, '2026-01-01')"
        ), {
            "id": pid, "uid": TEST_UID, "place": f"place-{pid}", "biz": business_name, "phone": phone_e164,
            "score": opportunity_score, "mw": matched_weakness, "loa": last_outcome_at,
        })


def _insert_draft(prospect_id, channel="whatsapp", content=None):
    import json
    with engine.begin() as conn:
        new_id = conn.execute(text(
            "INSERT INTO revenue_outreach_drafts (prospect_id, user_id, channel, draft_json, status, created_at) "
            "VALUES (:pid, :uid, :ch, :draft, 'draft', '2026-01-01') RETURNING id"
        ), {"pid": prospect_id, "uid": TEST_UID, "ch": channel, "draft": json.dumps(content or {"message_1": "Hi there"})}).scalar()
    return new_id


# ── _whatsapp_eligibility ────────────────────────────────────────────────────

def test_eligible_with_phone_no_dnc_no_cooldown():
    settings = _get_or_create_voice_settings(TEST_UID)
    ok, reason = _whatsapp_eligibility(TEST_UID, "+919828676825", None, settings)
    assert ok is True
    assert reason is None


def test_ineligible_missing_phone():
    settings = _get_or_create_voice_settings(TEST_UID)
    ok, reason = _whatsapp_eligibility(TEST_UID, None, None, settings)
    assert ok is False
    assert reason == "missing_phone"


def test_ineligible_dnc():
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO voice_dnc_list (user_id, phone_e164, reason, added_by, created_at) "
            "VALUES (:uid, '+919828676825', 'test', 'test', '2026-01-01')"
        ), {"uid": TEST_UID})
    settings = _get_or_create_voice_settings(TEST_UID)
    ok, reason = _whatsapp_eligibility(TEST_UID, "+919828676825", None, settings)
    assert ok is False
    assert reason == "dnc"


def test_ineligible_within_cooldown_using_last_outcome_at():
    from datetime import datetime, timedelta
    recent = (datetime.utcnow() - timedelta(days=1)).isoformat()
    settings = _get_or_create_voice_settings(TEST_UID)
    ok, reason = _whatsapp_eligibility(TEST_UID, "+919828676825", recent, settings)
    assert ok is False
    assert reason == "cooldown"


def test_eligible_after_cooldown_window_passes():
    from datetime import datetime, timedelta
    old = (datetime.utcnow() - timedelta(days=30)).isoformat()
    settings = _get_or_create_voice_settings(TEST_UID)
    ok, reason = _whatsapp_eligibility(TEST_UID, "+919828676825", old, settings)
    assert ok is True


# ── session creation ──────────────────────────────────────────────────────────

def test_create_session_includes_only_eligible_prospects():
    _insert_prospect(900001, phone_e164="+919828676825")
    _insert_prospect(900002, phone_e164=None)  # missing phone — ineligible
    resp = client.post("/revenue-engine/whatsapp-outreach/sessions",
                        json={"prospect_ids": [900001, 900002]}, headers=_AUTH)
    assert resp.status_code == 200
    data = resp.json()
    assert data["eligible_count"] == 1
    assert any(d["prospect_id"] == 900002 and d["reason"] == "missing_phone" for d in data["dropped"])


def test_create_session_rejects_when_none_eligible():
    _insert_prospect(900003, phone_e164=None)
    resp = client.post("/revenue-engine/whatsapp-outreach/sessions",
                        json={"prospect_ids": [900003]}, headers=_AUTH)
    assert resp.status_code == 400


def test_create_session_never_trusts_client_selection_dnc_added_after_selection():
    _insert_prospect(900004, phone_e164="+919828676825")
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO voice_dnc_list (user_id, phone_e164, reason, added_by, created_at) "
            "VALUES (:uid, '+919828676825', 'test', 'test', '2026-01-01')"
        ), {"uid": TEST_UID})
    resp = client.post("/revenue-engine/whatsapp-outreach/sessions",
                        json={"prospect_ids": [900004]}, headers=_AUTH)
    assert resp.status_code == 400  # would have been eligible at selection time, not anymore


def test_create_session_requires_auth():
    resp = client.post("/revenue-engine/whatsapp-outreach/sessions", json={"prospect_ids": [1]})
    assert resp.status_code == 401


# ── advance: sent ─────────────────────────────────────────────────────────────

def test_advance_sent_starts_cooldown_and_logs_timeline():
    _insert_prospect(900010, phone_e164="+919828676825")
    draft_id = _insert_draft(900010)
    session_id = client.post("/revenue-engine/whatsapp-outreach/sessions",
                              json={"prospect_ids": [900010]}, headers=_AUTH).json()["session_id"]

    resp = client.post(f"/revenue-engine/whatsapp-outreach/sessions/{session_id}/advance",
                        json={"outcome": "sent", "draft_id": draft_id}, headers=_AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["current_index"] == 1
    assert body["session_status"] == "completed"

    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT last_outcome, last_outcome_at FROM voice_prospects WHERE id=900010"
        )).fetchone()
    assert row[0] == "whatsapp_sent"
    assert row[1] is not None

    with engine.connect() as conn:
        events = conn.execute(text(
            "SELECT event_type FROM lead_timeline_events WHERE user_id=:uid ORDER BY id DESC LIMIT 1"
        ), {"uid": TEST_UID}).fetchall()
    assert events[0][0] == "outreach_sent"


def test_advance_sent_requires_draft_id():
    _insert_prospect(900011, phone_e164="+919828676825")
    session_id = client.post("/revenue-engine/whatsapp-outreach/sessions",
                              json={"prospect_ids": [900011]}, headers=_AUTH).json()["session_id"]
    resp = client.post(f"/revenue-engine/whatsapp-outreach/sessions/{session_id}/advance",
                        json={"outcome": "sent"}, headers=_AUTH)
    assert resp.status_code == 400


def test_advance_sent_rejects_draft_belonging_to_a_different_prospect():
    _insert_prospect(900012, phone_e164="+919828676825")
    _insert_prospect(900013, phone_e164="+919828676826")
    wrong_draft_id = _insert_draft(900013)
    session_id = client.post("/revenue-engine/whatsapp-outreach/sessions",
                              json={"prospect_ids": [900012]}, headers=_AUTH).json()["session_id"]
    resp = client.post(f"/revenue-engine/whatsapp-outreach/sessions/{session_id}/advance",
                        json={"outcome": "sent", "draft_id": wrong_draft_id}, headers=_AUTH)
    assert resp.status_code == 400


# ── advance: skipped / no_number never touch cooldown ────────────────────────

def test_advance_skipped_logs_timeline_but_does_not_start_cooldown():
    _insert_prospect(900020, phone_e164="+919828676825")
    session_id = client.post("/revenue-engine/whatsapp-outreach/sessions",
                              json={"prospect_ids": [900020]}, headers=_AUTH).json()["session_id"]
    resp = client.post(f"/revenue-engine/whatsapp-outreach/sessions/{session_id}/advance",
                        json={"outcome": "skipped"}, headers=_AUTH)
    assert resp.status_code == 200

    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT last_outcome, last_outcome_at FROM voice_prospects WHERE id=900020"
        )).fetchone()
    assert row[0] is None
    assert row[1] is None

    with engine.connect() as conn:
        events = conn.execute(text(
            "SELECT event_type FROM lead_timeline_events WHERE user_id=:uid ORDER BY id DESC LIMIT 1"
        ), {"uid": TEST_UID}).fetchall()
    assert events[0][0] == "outreach_skipped"


def test_advance_no_number_logs_timeline_but_does_not_start_cooldown():
    _insert_prospect(900021, phone_e164="+919828676825")
    session_id = client.post("/revenue-engine/whatsapp-outreach/sessions",
                              json={"prospect_ids": [900021]}, headers=_AUTH).json()["session_id"]
    resp = client.post(f"/revenue-engine/whatsapp-outreach/sessions/{session_id}/advance",
                        json={"outcome": "no_number"}, headers=_AUTH)
    assert resp.status_code == 200

    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT last_outcome FROM voice_prospects WHERE id=900021"
        )).fetchone()
    assert row[0] is None

    with engine.connect() as conn:
        events = conn.execute(text(
            "SELECT event_type FROM lead_timeline_events WHERE user_id=:uid ORDER BY id DESC LIMIT 1"
        ), {"uid": TEST_UID}).fetchall()
    assert events[0][0] == "outreach_no_number"


# ── advance: position/idempotency guards ─────────────────────────────────────

def test_advance_rejects_reprocessing_an_already_processed_item():
    _insert_prospect(900030, phone_e164="+919828676825")
    session_id = client.post("/revenue-engine/whatsapp-outreach/sessions",
                              json={"prospect_ids": [900030]}, headers=_AUTH).json()["session_id"]
    r1 = client.post(f"/revenue-engine/whatsapp-outreach/sessions/{session_id}/advance",
                      json={"outcome": "skipped"}, headers=_AUTH)
    assert r1.status_code == 200
    r2 = client.post(f"/revenue-engine/whatsapp-outreach/sessions/{session_id}/advance",
                      json={"outcome": "skipped"}, headers=_AUTH)
    assert r2.status_code == 400  # session already completed after the one item


def test_advance_moves_to_next_position_across_multiple_items():
    _insert_prospect(900040, phone_e164="+919828676825")
    _insert_prospect(900041, phone_e164="+919828676826")
    session_id = client.post("/revenue-engine/whatsapp-outreach/sessions",
                              json={"prospect_ids": [900040, 900041]}, headers=_AUTH).json()["session_id"]

    r1 = client.post(f"/revenue-engine/whatsapp-outreach/sessions/{session_id}/advance",
                      json={"outcome": "skipped"}, headers=_AUTH)
    assert r1.json()["current_index"] == 1
    assert r1.json()["session_status"] == "active"

    r2 = client.post(f"/revenue-engine/whatsapp-outreach/sessions/{session_id}/advance",
                      json={"outcome": "no_number"}, headers=_AUTH)
    assert r2.json()["current_index"] == 2
    assert r2.json()["session_status"] == "completed"


# ── resume ─────────────────────────────────────────────────────────────────────

def test_active_session_reflects_in_progress_queue():
    _insert_prospect(900050, phone_e164="+919828676825")
    session_id = client.post("/revenue-engine/whatsapp-outreach/sessions",
                              json={"prospect_ids": [900050]}, headers=_AUTH).json()["session_id"]
    resp = client.get("/revenue-engine/whatsapp-outreach/sessions/active", headers=_AUTH)
    assert resp.json()["session_id"] == session_id


def test_active_session_is_null_once_completed():
    _insert_prospect(900051, phone_e164="+919828676825")
    session_id = client.post("/revenue-engine/whatsapp-outreach/sessions",
                              json={"prospect_ids": [900051]}, headers=_AUTH).json()["session_id"]
    client.post(f"/revenue-engine/whatsapp-outreach/sessions/{session_id}/advance",
                json={"outcome": "skipped"}, headers=_AUTH)
    resp = client.get("/revenue-engine/whatsapp-outreach/sessions/active", headers=_AUTH)
    assert resp.json()["session_id"] is None


# ── session detail ────────────────────────────────────────────────────────────

def test_session_detail_returns_items_with_prospect_summary():
    _insert_prospect(900060, phone_e164="+919828676825", matched_weakness="no_website",
                      opportunity_score=91, business_name="Petite Patisserie")
    session_id = client.post("/revenue-engine/whatsapp-outreach/sessions",
                              json={"prospect_ids": [900060]}, headers=_AUTH).json()["session_id"]
    resp = client.get(f"/revenue-engine/whatsapp-outreach/sessions/{session_id}", headers=_AUTH)
    assert resp.status_code == 200
    body = resp.json()
    item = body["items"][0]
    assert item["business_name"] == "Petite Patisserie"
    assert item["opportunity_score"] == 91
    assert item["detected_gap"] == "No Website"
    assert item["status"] == "pending"

"""
Google Ads dashboard audit — three real reported bugs:

1. Daily trend chart skips dates (09-02 jumps to 09-16) — Google Ads'
   `FROM customer` segmented-by-date query isn't guaranteed to emit a row
   for every calendar day. _gads_zero_fill_daily is the fix, tested as a
   pure function (real unit tests, no mocking, no live Ads connection
   needed) rather than through the live-API endpoint itself.

2. Total Leads: 4, Lead Sources (WhatsApp/Website/Form) summed to 0 —
   leads created automatically by Voice Outreach (source='voice_outreach')
   and Revenue Engine (source='revenue_engine') were never NULL, just a
   source string /leads/stats' bucket set didn't recognize. Real DB-backed
   TestClient coverage (this bug was a missing bucket in real SQL-adjacent
   logic, the correct level to guard it at — same reasoning as
   test_quick_scan_cache.py's precedent for this codebase).

3. Conversions displays "0.0" instead of "0" — a frontend-only formatting
   bug (Dashboard.jsx's AnimatedDecimal always applies toFixed(1)
   regardless of whether the value is whole); no backend test needed,
   fixed and left to visual verification per this codebase's convention
   for frontend-only rendering bugs.
"""
import sys
import os
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jwt as pyjwt
from fastapi.testclient import TestClient
from sqlalchemy import text

from main import app, engine, LeadModel, SessionLocal, _gads_zero_fill_daily

TEST_SECRET = "test-jwt-secret-for-dashboard-diagnostics-tests-only"
TEST_UID = "test-uid-dashboard-diagnostics"


def _token(sub):
    return pyjwt.encode({"sub": sub}, TEST_SECRET, algorithm="HS256")


client = TestClient(app)
_AUTH = {"Authorization": f"Bearer {_token(TEST_UID)}"}


def setup_function():
    os.environ["SUPABASE_JWT_SECRET"] = TEST_SECRET
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM leads WHERE user_id=:uid"), {"uid": TEST_UID})


def teardown_function():
    setup_function()


def _insert_lead(source, status="New"):
    db = SessionLocal()
    try:
        db.add(LeadModel(name="Test Lead", phone="+919876543210", source=source, status=status, user_id=TEST_UID, created_at="2026-01-01"))
        db.commit()
    finally:
        db.close()


# ── /google-ads/daily zero-fill ──────────────────────────────────────────────

def test_zero_fill_inserts_a_row_for_every_day_in_range():
    start = date(2026, 9, 2)
    end = date(2026, 9, 5)
    result = _gads_zero_fill_daily({}, start, end)
    dates = [r["date"] for r in result]
    assert dates == ["2026-09-02", "2026-09-03", "2026-09-04", "2026-09-05"]


def test_zero_fill_never_skips_a_day_with_no_real_data():
    # The exact reported bug: 09-02 jumps to 09-16 — a 14-day gap with zero
    # activity must still produce 14 zero-filled rows, not be skipped.
    start = date(2026, 9, 2)
    end = date(2026, 9, 16)
    rows_by_date = {
        "2026-09-02": {"date": "2026-09-02", "impressions": 10, "clicks": 1, "cost_inr": 5.0},
        "2026-09-16": {"date": "2026-09-16", "impressions": 20, "clicks": 2, "cost_inr": 8.0},
    }
    result = _gads_zero_fill_daily(rows_by_date, start, end)
    assert len(result) == 15  # 09-02 through 09-16 inclusive
    middle = [r for r in result if r["date"] == "2026-09-09"][0]
    assert middle == {"date": "2026-09-09", "impressions": 0, "clicks": 0, "cost_inr": 0}


def test_zero_fill_preserves_real_data_for_days_that_have_it():
    start = date(2026, 9, 1)
    end = date(2026, 9, 2)
    rows_by_date = {"2026-09-01": {"date": "2026-09-01", "impressions": 78, "clicks": 5, "cost_inr": 4.39}}
    result = _gads_zero_fill_daily(rows_by_date, start, end)
    assert result[0] == {"date": "2026-09-01", "impressions": 78, "clicks": 5, "cost_inr": 4.39}
    assert result[1] == {"date": "2026-09-02", "impressions": 0, "clicks": 0, "cost_inr": 0}


def test_zero_fill_single_day_range():
    d = date(2026, 9, 1)
    result = _gads_zero_fill_daily({}, d, d)
    assert result == [{"date": "2026-09-01", "impressions": 0, "clicks": 0, "cost_inr": 0}]


# ── /leads/stats unknown bucket ──────────────────────────────────────────────

def test_stats_requires_auth():
    resp = client.get("/leads/stats")
    assert resp.status_code == 401


def test_known_sources_bucket_correctly():
    _insert_lead("whatsapp")
    _insert_lead("website")
    _insert_lead("form")
    resp = client.get("/leads/stats", headers=_AUTH)
    stats = resp.json()
    assert stats["whatsapp"] == 1
    assert stats["website"] == 1
    assert stats["form"] == 1
    assert stats["unknown"] == 0
    assert stats["total"] == 3


def test_the_exact_reported_bug_voice_and_revenue_engine_sources_land_in_unknown():
    # Real reported case: Total Leads: 4, Lead Sources (WhatsApp/Website/
    # Form) all 0 — because these two automatic sources aren't NULL, just
    # not one of the three recognized bucket strings.
    _insert_lead("voice_outreach")
    _insert_lead("revenue_engine")
    _insert_lead("voice_outreach")
    _insert_lead("revenue_engine")
    resp = client.get("/leads/stats", headers=_AUTH)
    stats = resp.json()
    assert stats["total"] == 4
    assert stats["whatsapp"] == 0
    assert stats["website"] == 0
    assert stats["form"] == 0
    assert stats["unknown"] == 4  # now visible instead of silently vanishing


def test_total_always_reconciles_with_the_four_buckets():
    _insert_lead("whatsapp")
    _insert_lead("voice_outreach")
    _insert_lead("website")
    _insert_lead("some_future_source_nobody_has_written_yet")
    resp = client.get("/leads/stats", headers=_AUTH)
    stats = resp.json()
    assert stats["total"] == stats["whatsapp"] + stats["website"] + stats["form"] + stats["unknown"]


def test_no_leads_returns_all_zeros_not_a_crash():
    resp = client.get("/leads/stats", headers=_AUTH)
    stats = resp.json()
    assert stats == {"total": 0, "whatsapp": 0, "website": 0, "form": 0, "unknown": 0, "new": 0, "converted": 0}


def test_status_buckets_are_unaffected_by_the_source_fix():
    _insert_lead("voice_outreach", status="Converted")
    _insert_lead("website", status="New")
    resp = client.get("/leads/stats", headers=_AUTH)
    stats = resp.json()
    assert stats["new"] == 1
    assert stats["converted"] == 1

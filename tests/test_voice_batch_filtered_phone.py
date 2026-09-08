"""
Post-audit fix. voice_prospects rows for enterprise/chain-filtered
businesses (approval_status='filtered' — still shown in the Pipeline list
by design, never silently hidden) never had phone_e164 computed at all —
the INSERT for filtered_out prospects in _run_voice_batch_job (main.py)
omitted the column entirely, while the INSERT for normally-scored
prospects always computed it via _normalize_phone_e164(phone_raw).

Found while investigating a live "44/44 show No phone number" report on a
"hotels in Jaipur" scan — a near-worst-case search for this bug, since
_VOICE_CHAIN_BRAND_KEYWORDS lists several hotel chains with real Jaipur
properties (Taj, ITC, Marriott, Hyatt...), on top of ordinary same-name
duplicate-location filtering. The WhatsApp bulk-send eligibility check
reads phone_e164, not phone_raw, so every filtered prospect displayed
"No phone number" regardless of what Google Places actually returned.

This test exercises the exact INSERT text used for filtered_out rows
(copied verbatim from main.py) against the real test DB — the bug was a
missing column in that literal SQL, so this is the correct level to guard
it at; the surrounding _run_voice_batch_job is a live-Places/Tavily/GPT
async function not practical to unit test in isolation.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from main import engine, _normalize_phone_e164

TEST_UID = "test-uid-filtered-phone"


def setup_function():
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM voice_prospects WHERE user_id=:uid"), {"uid": TEST_UID})


def teardown_function():
    setup_function()


def _insert_filtered_rows(rows):
    """The exact INSERT text used for filtered_out prospects in
    _run_voice_batch_job — kept identical here so a future edit to that
    statement that drops phone_e164 again fails this test."""
    ts = "2026-01-01"
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO voice_prospects (batch_id, user_id, place_id, business_name, address, "
            "phone_raw, phone_e164, website, google_rating, total_reviews, business_status, approval_status, "
            "filter_reason, created_at, updated_at) "
            "VALUES (:batch_id, :user_id, :place_id, :business_name, :address, :phone_raw, :phone_e164, :website, "
            ":google_rating, :total_reviews, :business_status, 'filtered', :filter_reason, :ts, :ts)"
        ), [{
            "batch_id": "test-batch", "user_id": TEST_UID, "place_id": r["place_id"],
            "business_name": r["business_name"], "address": "", "phone_raw": r["phone_raw"],
            "phone_e164": _normalize_phone_e164(r["phone_raw"]),
            "website": "", "google_rating": None, "total_reviews": 0,
            "business_status": "OPERATIONAL", "filter_reason": "enterprise_filtered: test", "ts": ts,
        } for r in rows])


def test_filtered_prospect_gets_phone_e164_computed_not_left_null():
    _insert_filtered_rows([{"place_id": "p1", "business_name": "Taj Jaipur", "phone_raw": "098286 76825"}])
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT phone_raw, phone_e164, approval_status FROM voice_prospects WHERE user_id=:uid AND place_id='p1'"
        ), {"uid": TEST_UID}).fetchone()
    assert row[0] == "098286 76825"
    assert row[1] == "+919828676825"  # this is exactly what was missing before the fix
    assert row[2] == "filtered"


def test_filtered_prospect_with_no_real_phone_stays_null_not_fabricated():
    _insert_filtered_rows([{"place_id": "p2", "business_name": "No Phone Hotel", "phone_raw": ""}])
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT phone_e164 FROM voice_prospects WHERE user_id=:uid AND place_id='p2'"
        ), {"uid": TEST_UID}).fetchone()
    assert row[0] is None


def test_filtered_prospect_phone_e164_makes_it_whatsapp_ineligible_correctly_not_falsely():
    from main import _whatsapp_eligibility, _get_or_create_voice_settings
    _insert_filtered_rows([
        {"place_id": "p3", "business_name": "Hotel With Real Phone", "phone_raw": "098286 76825"},
        {"place_id": "p4", "business_name": "Hotel With No Phone", "phone_raw": ""},
    ])
    settings = _get_or_create_voice_settings(TEST_UID)
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT place_id, phone_e164 FROM voice_prospects WHERE user_id=:uid ORDER BY place_id"
        ), {"uid": TEST_UID}).fetchall()
    by_place = {r[0]: r[1] for r in rows}

    ok3, reason3 = _whatsapp_eligibility(TEST_UID, by_place["p3"], None, settings)
    assert ok3 is True
    assert reason3 is None

    ok4, reason4 = _whatsapp_eligibility(TEST_UID, by_place["p4"], None, settings)
    assert ok4 is False
    assert reason4 == "missing_phone"

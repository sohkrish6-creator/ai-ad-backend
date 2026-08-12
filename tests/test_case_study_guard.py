"""
P0.2 tests. get_case_study_for_prompt() is DB-coupled by design (a real SQL
lookup — see report_validators.py's module docstring on why pure logic
lives there and DB-touching logic stays in main.py). It's tested here
against the local SQLite fallback DB (import main triggers the same
table-creation main.py always runs at module load, exercised safely
throughout this pass) — a real, if narrow, integration test, not a mock.

One real gotcha this surfaced: `get_case_study_for_prompt` returns None
immediately for an empty-string user_id (`if not user_id: return None`) —
correct and important (an empty uid must never match a same-empty-uid row,
which would be a cross-tenant leak risk if one ever existed), but it means
this function is untestable with the *actual* empty-string uid every
locally-run request carries (no SUPABASE_JWT_SECRET configured locally, so
auth_middleware's own documented fallback sets user_id=""). Every test
below therefore uses a realistic non-empty uid, matching what a real
authenticated production request always has.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from main import _format_case_study_for_prompt, get_case_study_for_prompt, engine

TEST_UID = "test-user-case-study-guard"
OTHER_UID = "test-user-other-tenant"


def _insert(uid, client_name, industry, verified=True):
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO case_studies (user_id, client_name, industry, metric, value, timeframe, verified, uploaded_by, created_at) "
            "VALUES (:uid, :name, :industry, 'repeat order volume', 'up 22 percent', '90 days', :verified, :uid, '2026-01-01T00:00:00')"
        ), {"uid": uid, "name": client_name, "industry": industry, "verified": verified})


def _cleanup():
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM case_studies WHERE user_id IN (:a, :b)"), {"a": TEST_UID, "b": OTHER_UID})


def setup_function():
    _cleanup()


def teardown_function():
    _cleanup()


def test_industry_match_returns_the_case_study():
    _insert(TEST_UID, "Apex Diagnostics", "Life Sciences Distribution")
    result = get_case_study_for_prompt(TEST_UID, "Life Sciences Distribution")
    assert result is not None
    assert result["client_name"] == "Apex Diagnostics"


def test_no_industry_hint_falls_back_to_most_recent_verified():
    _insert(TEST_UID, "Apex Diagnostics", "Life Sciences Distribution")
    result = get_case_study_for_prompt(TEST_UID, "")
    assert result is not None
    assert result["client_name"] == "Apex Diagnostics"


def test_non_matching_industry_falls_back_rather_than_returning_none():
    _insert(TEST_UID, "Apex Diagnostics", "Life Sciences Distribution")
    result = get_case_study_for_prompt(TEST_UID, "Retail")
    assert result is not None  # falls back to the tenant's most recent verified study


def test_unverified_case_study_is_never_referenced():
    _insert(TEST_UID, "Apex Diagnostics", "Life Sciences Distribution", verified=False)
    result = get_case_study_for_prompt(TEST_UID, "Life Sciences Distribution")
    assert result is None


def test_different_tenant_cannot_see_another_tenants_case_study():
    _insert(TEST_UID, "Apex Diagnostics", "Life Sciences Distribution")
    result = get_case_study_for_prompt(OTHER_UID, "Life Sciences Distribution")
    assert result is None


def test_empty_tenant_library_returns_none():
    result = get_case_study_for_prompt(TEST_UID, "Life Sciences Distribution")
    assert result is None


def test_format_includes_client_industry_metric_value_timeframe():
    cs = {"client_name": "Acme Labs", "industry": "life sciences", "metric": "order volume", "value": "+22%", "timeframe": "60 days"}
    formatted = _format_case_study_for_prompt(cs)
    assert "Acme Labs" in formatted
    assert "life sciences" in formatted
    assert "order volume" in formatted
    assert "+22%" in formatted
    assert "60 days" in formatted


def test_format_handles_missing_industry_and_timeframe():
    cs = {"client_name": "Acme Labs", "industry": None, "metric": "orders", "value": "+10", "timeframe": None}
    formatted = _format_case_study_for_prompt(cs)
    assert "Acme Labs" in formatted
    assert "a client" in formatted  # fallback for missing industry
    assert " in None" not in formatted  # missing timeframe must not leak a literal "None"

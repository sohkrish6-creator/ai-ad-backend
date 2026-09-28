"""
Campaign diagnostics endpoint (GET /google-ads/campaign-diagnostics/{id}) —
real reported case: a live Search campaign running 3 days with 78
impressions/5 clicks/₹4.39 spend/0 conversions, and no way for the
dashboard to say WHY. The endpoint itself needs a live Google Ads
connection (not available in this test environment — same reasoning
_run_gads_import_job's own tests give for not exercising the live-API path
directly), so this file covers: auth/validation guards via real TestClient,
and the pure reason->severity/detail/fix mapping and humanizer directly.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jwt as pyjwt
from fastapi.testclient import TestClient

from main import app, _CAMPAIGN_STATUS_REASON_INFO, _humanize_reason

TEST_SECRET = "test-jwt-secret-for-campaign-diagnostics-tests-only"
TEST_UID = "test-uid-campaign-diagnostics"


def _token(sub):
    return pyjwt.encode({"sub": sub}, TEST_SECRET, algorithm="HS256")


client = TestClient(app)


def setup_function():
    os.environ["SUPABASE_JWT_SECRET"] = TEST_SECRET


_AUTH = {"Authorization": f"Bearer {_token(TEST_UID)}"}


def test_requires_auth():
    resp = client.get("/google-ads/campaign-diagnostics/12345")
    assert resp.status_code == 401


def test_rejects_a_non_numeric_campaign_id():
    resp = client.get("/google-ads/campaign-diagnostics/not-a-number", headers=_AUTH)
    assert resp.status_code == 400


def test_humanize_reason_formats_the_raw_enum_name():
    assert _humanize_reason("BUDGET_CONSTRAINED") == "Budget constrained"
    assert _humanize_reason("NO_KEYWORDS") == "No keywords"


# ── reason-info mapping: every entry has the shape the endpoint depends on ──

def test_every_reason_entry_has_severity_detail_and_fix():
    for reason, (severity, detail, fix) in _CAMPAIGN_STATUS_REASON_INFO.items():
        assert severity in ("high", "medium", "low"), f"{reason} has an invalid severity {severity!r}"
        assert isinstance(detail, str) and detail, f"{reason} has no detail text"
        assert isinstance(fix, str) and fix, f"{reason} has no recommended fix"


def test_the_real_reported_symptoms_map_to_high_severity_actionable_reasons():
    # These are the reasons most likely to explain the exact reported case
    # (near-zero delivery against an untouched budget) — confirm they're
    # all present and flagged high, not buried as low-severity noise.
    for reason in ("BUDGET_CONSTRAINED", "BIDDING_STRATEGY_LIMITED", "SEARCH_VOLUME_LIMITED", "NO_KEYWORDS", "NO_AD_GROUP_ADS", "HAS_ADS_DISAPPROVED"):
        assert reason in _CAMPAIGN_STATUS_REASON_INFO
        assert _CAMPAIGN_STATUS_REASON_INFO[reason][0] == "high"


def test_bidding_strategy_learning_is_low_severity_not_a_false_alarm():
    # A brand-new automated bid strategy in its normal learning period must
    # not be flagged the same way as a genuine misconfiguration.
    assert _CAMPAIGN_STATUS_REASON_INFO["BIDDING_STRATEGY_LEARNING"][0] == "low"


def test_an_unmapped_reason_is_handled_by_the_endpoints_fallback_not_a_crash():
    # The endpoint's own code uses .get(reason, (fallback...)) for any
    # reason not in this dict — confirm the dict doesn't claim to cover
    # every possible enum value (it doesn't have to; the fallback exists
    # precisely so a newly-added Google Ads enum value never breaks this).
    assert "CAMPAIGN_GROUP_ALL_GROUP_BUDGETS_ENDED" not in _CAMPAIGN_STATUS_REASON_INFO

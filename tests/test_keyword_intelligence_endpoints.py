"""
Keyword Intelligence: HTTP-surface coverage for job listing/detail/
shortlist — real TestClient end-to-end (exercises real auth_middleware +
routing, matching test_reel_jobs_endpoints.py's precedent), inserting
keyword_research_jobs/keyword_research_memory rows directly rather than
running the real Google Ads pipeline (needs a live, rate-limited Ads API
call this test environment should never make in a unit-test run).

Key behaviors under test:
- Every query is user_id-scoped.
- The shortlist follow-up endpoint requires a completed job and a real
  positive budget, and never re-hits the Ads API (asserted implicitly —
  it only reads from the stored memory result).
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jwt as pyjwt
from fastapi.testclient import TestClient
from sqlalchemy import text

from main import app, engine, save_to_memory, _KW_LABEL_REAL_VOLUME

TEST_SECRET = "test-jwt-secret-for-keyword-intel-tests-only"
TEST_UID = "test-uid-keyword-intel"
OTHER_UID = "test-uid-keyword-intel-other-tenant"


def _token(sub):
    return pyjwt.encode({"sub": sub}, TEST_SECRET, algorithm="HS256")


client = TestClient(app)
_AUTH = {"Authorization": f"Bearer {_token(TEST_UID)}"}
_OTHER_AUTH = {"Authorization": f"Bearer {_token(OTHER_UID)}"}


def setup_function():
    os.environ["SUPABASE_JWT_SECRET"] = TEST_SECRET
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM keyword_research_jobs WHERE user_id IN (:u1, :u2)"),
                     {"u1": TEST_UID, "u2": OTHER_UID})
        conn.execute(text("DELETE FROM keyword_research_memory WHERE business_key LIKE :pfx"),
                     {"pfx": f"%{TEST_UID}%"})


def teardown_function():
    setup_function()


def _insert_job(job_id, user_id=TEST_UID, status="queued", business_key=None):
    business_key = business_key or f"{user_id}::skin clinic::jaipur"
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO keyword_research_jobs (id, user_id, business_key, category, city, seed_keywords_json, "
            "status, current_step, created_at) "
            "VALUES (:id, :uid, :bk, 'skin clinic', 'Jaipur', '[]', :status, 'Queued', '2026-01-01T00:00:00')"
        ), {"id": job_id, "uid": user_id, "bk": business_key, "status": status})
    return business_key


# ── auth ──────────────────────────────────────────────────────────────────

def test_start_job_requires_auth():
    resp = client.post("/keyword-intelligence/jobs", json={"category": "skin clinic", "city": "Jaipur"})
    assert resp.status_code == 401


def test_list_jobs_requires_auth():
    assert client.get("/keyword-intelligence/jobs").status_code == 401


def test_get_job_requires_auth():
    assert client.get("/keyword-intelligence/jobs/some-id").status_code == 401


def test_shortlist_requires_auth():
    resp = client.post("/keyword-intelligence/jobs/some-id/shortlist", json={"budget": 5000})
    assert resp.status_code == 401


def test_start_job_requires_a_category():
    resp = client.post("/keyword-intelligence/jobs", json={"category": "", "city": "Jaipur"}, headers=_AUTH)
    assert resp.status_code == 400


# ── user_id scoping ──────────────────────────────────────────────────────────

def test_list_jobs_only_returns_the_callers_own_jobs():
    _insert_job("job-mine")
    _insert_job("job-other", user_id=OTHER_UID)
    resp = client.get("/keyword-intelligence/jobs", headers=_AUTH)
    ids = [j["id"] for j in resp.json()["jobs"]]
    assert "job-mine" in ids
    assert "job-other" not in ids


def test_get_job_404s_for_another_tenants_job():
    _insert_job("job-not-mine")
    resp = client.get("/keyword-intelligence/jobs/job-not-mine", headers=_OTHER_AUTH)
    assert resp.status_code == 404


def test_get_job_succeeds_for_the_owning_tenant():
    _insert_job("job-owned", status="processing")
    resp = client.get("/keyword-intelligence/jobs/job-owned", headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["job"]["status"] == "processing"
    assert resp.json()["job"]["category"] == "skin clinic"


def test_get_job_404s_for_a_nonexistent_job():
    resp = client.get("/keyword-intelligence/jobs/does-not-exist", headers=_AUTH)
    assert resp.status_code == 404


def test_get_done_job_includes_the_stored_result():
    business_key = _insert_job("job-done", status="done")
    save_to_memory("keyword_research", business_key, {"data": {
        "category": "skin clinic", "top_searches": [{"keyword": "x", "avg_monthly_searches": 100, "label": _KW_LABEL_REAL_VOLUME}],
        "bid_shortlist": [], "needs_budget": True,
    }})
    resp = client.get("/keyword-intelligence/jobs/job-done", headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["result"]["category"] == "skin clinic"


def test_get_processing_job_has_no_result_yet():
    _insert_job("job-in-progress", status="processing")
    resp = client.get("/keyword-intelligence/jobs/job-in-progress", headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["result"] is None


# ── shortlist follow-up endpoint ──────────────────────────────────────────

def test_shortlist_rejects_non_positive_budget():
    _insert_job("job-for-shortlist", status="done")
    resp = client.post("/keyword-intelligence/jobs/job-for-shortlist/shortlist", json={"budget": 0}, headers=_AUTH)
    assert resp.status_code == 400


def test_shortlist_404s_for_a_job_not_owned_by_caller():
    _insert_job("job-shortlist-other")
    resp = client.post("/keyword-intelligence/jobs/job-shortlist-other/shortlist", json={"budget": 5000}, headers=_OTHER_AUTH)
    assert resp.status_code == 404


def test_shortlist_rejects_a_job_that_is_not_done_yet():
    _insert_job("job-shortlist-queued", status="queued")
    resp = client.post("/keyword-intelligence/jobs/job-shortlist-queued/shortlist", json={"budget": 5000}, headers=_AUTH)
    assert resp.status_code == 400


def test_shortlist_404s_when_no_stored_result_exists():
    _insert_job("job-shortlist-no-result", status="done", business_key="nonexistent-key-xyz")
    resp = client.post("/keyword-intelligence/jobs/job-shortlist-no-result/shortlist", json={"budget": 5000}, headers=_AUTH)
    assert resp.status_code == 404

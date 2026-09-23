"""
AI Reel Auto-Editor: HTTP-surface coverage for job listing/detail/download-
url — real TestClient end-to-end (exercises real auth_middleware + routing,
matching test_whatsapp_outreach_sessions.py's precedent), inserting
creative_reel_jobs rows directly rather than running the real upload+
background-job pipeline (that needs Supabase Storage + Anthropic
credentials neither available in this test environment nor desirable to
hit in a unit-test run — same reasoning _run_voice_batch_job's own tests
give for not exercising the live-API path directly).

Key behaviors under test:
- Every query is user_id-scoped — another tenant's job is never visible,
  matches the module-wide rule stated in its own docstring.
- download-url refuses a job that isn't status='done' with a readable
  message, rather than returning a broken/empty signed URL.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jwt as pyjwt
from fastapi.testclient import TestClient
from sqlalchemy import text

from main import app, engine

TEST_SECRET = "test-jwt-secret-for-reel-jobs-tests-only"
TEST_UID = "test-uid-reel-jobs"
OTHER_UID = "test-uid-reel-jobs-other-tenant"


def _token(sub):
    return pyjwt.encode({"sub": sub}, TEST_SECRET, algorithm="HS256")


client = TestClient(app)
_AUTH = {"Authorization": f"Bearer {_token(TEST_UID)}"}
_OTHER_AUTH = {"Authorization": f"Bearer {_token(OTHER_UID)}"}


def setup_function():
    os.environ["SUPABASE_JWT_SECRET"] = TEST_SECRET
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM creative_reel_jobs WHERE user_id IN (:u1, :u2)"),
                     {"u1": TEST_UID, "u2": OTHER_UID})


def teardown_function():
    setup_function()


def _insert_job(job_id, user_id=TEST_UID, status="queued", output_storage_path=None):
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO creative_reel_jobs (id, user_id, status, current_step, source_filename, "
            "source_storage_path, output_storage_path, created_at) "
            "VALUES (:id, :uid, :status, 'Queued', 'clip.mp4', :sp, :op, '2026-01-01T00:00:00')"
        ), {"id": job_id, "uid": user_id, "status": status,
            "sp": f"{user_id}/{job_id}/source.mp4", "op": output_storage_path})


# ── auth ──────────────────────────────────────────────────────────────────

def test_list_jobs_requires_auth():
    resp = client.get("/creative-studio/reel-editor/jobs")
    assert resp.status_code == 401


def test_get_job_requires_auth():
    resp = client.get("/creative-studio/reel-editor/jobs/some-id")
    assert resp.status_code == 401


def test_download_url_requires_auth():
    resp = client.get("/creative-studio/reel-editor/jobs/some-id/download-url")
    assert resp.status_code == 401


# ── user_id scoping — the module's own stated rule ───────────────────────────

def test_list_jobs_only_returns_the_callers_own_jobs():
    _insert_job("job-mine-1")
    _insert_job("job-other-1", user_id=OTHER_UID)
    resp = client.get("/creative-studio/reel-editor/jobs", headers=_AUTH)
    ids = [j["id"] for j in resp.json()["jobs"]]
    assert "job-mine-1" in ids
    assert "job-other-1" not in ids


def test_get_job_404s_for_another_tenants_job():
    _insert_job("job-not-mine")
    resp = client.get("/creative-studio/reel-editor/jobs/job-not-mine", headers=_OTHER_AUTH)
    assert resp.status_code == 404


def test_get_job_succeeds_for_the_owning_tenant():
    _insert_job("job-owned", status="processing")
    resp = client.get("/creative-studio/reel-editor/jobs/job-owned", headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["job"]["status"] == "processing"


def test_get_job_404s_for_a_nonexistent_job():
    resp = client.get("/creative-studio/reel-editor/jobs/does-not-exist", headers=_AUTH)
    assert resp.status_code == 404


# ── download-url status gating ───────────────────────────────────────────────

def test_download_url_rejects_a_job_that_is_still_queued():
    _insert_job("job-queued", status="queued")
    resp = client.get("/creative-studio/reel-editor/jobs/job-queued/download-url", headers=_AUTH)
    assert resp.status_code == 400
    assert "queued" in resp.json()["detail"].lower()


def test_download_url_rejects_a_failed_job():
    _insert_job("job-failed", status="failed")
    resp = client.get("/creative-studio/reel-editor/jobs/job-failed/download-url", headers=_AUTH)
    assert resp.status_code == 400
    assert "failed" in resp.json()["detail"].lower()


def test_download_url_404s_for_another_tenants_done_job():
    _insert_job("job-done-other", user_id=OTHER_UID, status="done", output_storage_path=f"{OTHER_UID}/job-done-other/output.mp4")
    resp = client.get("/creative-studio/reel-editor/jobs/job-done-other/download-url", headers=_AUTH)
    assert resp.status_code == 404

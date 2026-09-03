"""
Post-audit fix (Revenue Brain Phase 0 tenancy audit, follow-up #1). A JWT
that verifies cleanly (real signature, not expired) but carries an empty,
missing, or whitespace-only `sub` claim used to pass through auth_middleware
as user_id="" — indistinguishable from the legitimate local-dev no-JWT-
secret case, which every "if uid: filter(...)" endpoint in this codebase
(~20 of them, reported separately) trusts to mean "no tenant scoping
needed". This is the root cause: fixing it here closes the whole class at
its source rather than requiring every call site to be patched
individually.

Real end-to-end test via TestClient — the middleware only exists as an ASGI
middleware, not a plain function, so this exercises the actual registered
auth_middleware exactly as a real request would, not a mocked substitute.
Uses GET /leads as the protected target (confirmed not in _PUBLIC_PATHS).
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jwt as pyjwt
from fastapi.testclient import TestClient

from main import app

TEST_SECRET = "test-jwt-secret-for-auth-middleware-tests-only"


def _token(sub=None, **extra_claims):
    payload = dict(extra_claims)
    if sub is not None:
        payload["sub"] = sub
    return pyjwt.encode(payload, TEST_SECRET, algorithm="HS256")


def setup_function():
    os.environ["SUPABASE_JWT_SECRET"] = TEST_SECRET


def teardown_function():
    os.environ.pop("SUPABASE_JWT_SECRET", None)


client = TestClient(app)


def test_valid_sub_is_accepted():
    token = _token("real-user-id-123")
    resp = client.get("/leads", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200


def test_empty_sub_is_rejected_with_401():
    token = _token("")
    resp = client.get("/leads", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_missing_sub_claim_entirely_is_rejected_with_401():
    token = _token(other_claim="something")  # no "sub" key at all
    resp = client.get("/leads", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_whitespace_only_sub_is_rejected_with_401():
    token = _token("   ")
    resp = client.get("/leads", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_no_authorization_header_at_all_is_rejected():
    resp = client.get("/leads")
    assert resp.status_code == 401


def test_rejected_request_never_reaches_the_handler():
    # Not just "some 401" — confirm it's specifically the missing-subject
    # rejection, not a coincidental 401 from inside the handler itself.
    token = _token("")
    resp = client.get("/leads", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert "subject" in resp.json().get("error", "").lower()

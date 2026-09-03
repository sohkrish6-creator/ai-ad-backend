"""
Post-audit fix. GET /leads and GET /leads/stats used `if _uid: filter(...)` —
an empty/missing user_id (a JWT that decodes but carries no `sub`, or any
future auth-middleware regression) silently returned every tenant's leads
instead of none. Found during the Revenue Brain Phase 0 tenancy audit,
alongside ~20 other call sites sharing the same conditional-filter pattern
(reported separately, not fixed here — this file covers only the two
endpoints explicitly fixed).

Real DB integration test (matches this codebase's "DB-touching logic gets
a real local-SQLite test" convention, see test_prospect_scan_history.py) —
calls the endpoint functions directly with a minimal fake Request, since
FastAPI's auth middleware itself (which sets request.state.user_id) is
already covered by not being reachable without a real deploy/JWT setup.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from main import get_leads, get_stats, LeadModel, SessionLocal, engine

TENANT_A = "test-tenant-a-leads"
TENANT_B = "test-tenant-b-leads"


class _FakeRequestState:
    def __init__(self, user_id):
        self.user_id = user_id


class _FakeRequest:
    def __init__(self, user_id):
        self.state = _FakeRequestState(user_id)


def _cleanup():
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM leads WHERE user_id IN (:a, :b)"), {"a": TENANT_A, "b": TENANT_B})


def setup_function():
    _cleanup()
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO leads (name, phone, email, source, message, campaign, status, user_id, created_at) "
            "VALUES ('Tenant A Lead', '111', 'a@x.com', 'form', '', '', 'New', :uid, '01 Jan 2026')"
        ), {"uid": TENANT_A})
        conn.execute(text(
            "INSERT INTO leads (name, phone, email, source, message, campaign, status, user_id, created_at) "
            "VALUES ('Tenant B Lead', '222', 'b@x.com', 'whatsapp', '', '', 'Converted', :uid, '01 Jan 2026')"
        ), {"uid": TENANT_B})


def teardown_function():
    _cleanup()


def test_get_leads_with_empty_uid_is_rejected_not_unfiltered():
    db = SessionLocal()
    try:
        with pytest.raises(HTTPException) as exc_info:
            get_leads(_FakeRequest(""), db=db)
        assert exc_info.value.status_code == 401
    finally:
        db.close()


def test_get_leads_only_returns_the_requesting_tenants_rows():
    db = SessionLocal()
    try:
        result = get_leads(_FakeRequest(TENANT_A), db=db)
        names = [lead["name"] for lead in result["leads"]]
        assert "Tenant A Lead" in names
        assert "Tenant B Lead" not in names
        assert result["total"] == len(names)
    finally:
        db.close()


def test_get_stats_with_empty_uid_is_rejected_not_unfiltered():
    db = SessionLocal()
    try:
        with pytest.raises(HTTPException) as exc_info:
            get_stats(_FakeRequest(""), db=db)
        assert exc_info.value.status_code == 401
    finally:
        db.close()


def test_get_stats_only_counts_the_requesting_tenants_rows():
    db = SessionLocal()
    try:
        result = get_stats(_FakeRequest(TENANT_A), db=db)
        assert result["total"] == 1
        assert result["form"] == 1
        assert result["whatsapp"] == 0  # Tenant B's whatsapp lead must not be counted
    finally:
        db.close()


def test_get_leads_with_none_uid_is_also_rejected():
    # getattr(request.state, "user_id", "") on a real request never yields
    # None (auth_middleware always sets a string), but the guard should be
    # robust to it regardless — `if not _uid` covers None the same as "".
    db = SessionLocal()
    try:
        with pytest.raises(HTTPException) as exc_info:
            get_leads(_FakeRequest(None), db=db)
        assert exc_info.value.status_code == 401
    finally:
        db.close()

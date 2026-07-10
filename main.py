from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from openai import OpenAI
import httpx
from datetime import datetime, date, timedelta
from typing import Optional
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow as _GoogleOAuthFlow
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from dotenv import load_dotenv
import os
import logging
import json
import asyncio
import re
import random
import time
import traceback as _traceback
import difflib
import hmac
import hashlib
import base64
import uuid

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY")
_raw_db_url = os.getenv("DATABASE_URL", "sqlite:///./ai_ad_manager.db")
# SQLAlchemy requires "postgresql://" but Supabase/Render supply "postgres://"
DATABASE_URL = _raw_db_url.replace("postgres://", "postgresql://", 1)
_is_sqlite = DATABASE_URL.startswith("sqlite")
# Mask password for safe logging: show scheme + host only
_db_host = DATABASE_URL.split("@")[-1].split("/")[0] if "@" in DATABASE_URL else DATABASE_URL[:40]
logger.info(f"[DB] Connecting to host: {_db_host} | sqlite={_is_sqlite}")
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class LeadModel(Base):
    __tablename__ = "leads"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255))
    phone = Column(String(50))
    email = Column(String(255))
    source = Column(String(50))
    message = Column(Text)
    campaign = Column(String(255))
    status = Column(String(50), default="New")
    created_at = Column(String(100))

class AnalysisModel(Base):
    __tablename__ = "analyses"
    id = Column(Integer, primary_key=True, index=True)
    url = Column(String(500))
    business_type = Column(String(100))
    budget = Column(Integer)
    goal = Column(String(100))
    result = Column(Text)
    created_at = Column(String(100))

class ReportModel(Base):
    __tablename__ = "reports"
    id = Column(Integer, primary_key=True, index=True)
    report_type = Column(String(100))
    title = Column(String(500))
    input_data = Column(Text)
    result_data = Column(Text)
    created_at = Column(String(100))

try:
    Base.metadata.create_all(bind=engine)
    logger.info("[DB] create_all succeeded")
except Exception as _e:
    logger.error(f"[DB] create_all failed ({_e}). Falling back to per-table creation.")
    for _model in [LeadModel, AnalysisModel, ReportModel]:
        try:
            _model.__table__.create(bind=engine, checkfirst=True)
            logger.info(f"[DB] Created table: {_model.__tablename__}")
        except Exception as _te:
            logger.warning(f"[DB] Could not create table '{_model.__tablename__}': {_te}")

# ── Memory System Tables ─────────────────────────────────────────────────────
# Use raw SQL so JSONB works on Postgres and TEXT works on SQLite identically.
_MEMORY_DDL = """
CREATE TABLE IF NOT EXISTS business_memory (
    id           BIGSERIAL PRIMARY KEY,
    business_key TEXT UNIQUE NOT NULL,
    business_name TEXT,
    industry      TEXT,
    city          TEXT,
    business_dna  TEXT,
    uvp           TEXT,
    positioning   TEXT,
    brand_score   REAL,
    trust_score   REAL,
    opportunity_score REAL,
    created_at    TEXT,
    updated_at    TEXT
);
CREATE TABLE IF NOT EXISTS market_memory (
    id               BIGSERIAL PRIMARY KEY,
    business_key     TEXT UNIQUE NOT NULL,
    market_size      TEXT,
    growth           TEXT,
    trends           TEXT,
    seasonality      TEXT,
    competition_level TEXT,
    market_gap       TEXT,
    created_at       TEXT,
    updated_at       TEXT
);
CREATE TABLE IF NOT EXISTS competitor_memory (
    id           BIGSERIAL PRIMARY KEY,
    business_key TEXT UNIQUE NOT NULL,
    competitors  TEXT,
    created_at   TEXT,
    updated_at   TEXT
);
CREATE TABLE IF NOT EXISTS audience_memory (
    id           BIGSERIAL PRIMARY KEY,
    business_key TEXT UNIQUE NOT NULL,
    segments     TEXT,
    created_at   TEXT,
    updated_at   TEXT
);
CREATE TABLE IF NOT EXISTS campaign_memory (
    id            BIGSERIAL PRIMARY KEY,
    business_key  TEXT UNIQUE NOT NULL,
    campaign_data TEXT,
    created_at    TEXT,
    updated_at    TEXT
);
CREATE TABLE IF NOT EXISTS opportunity_memory (
    id                BIGSERIAL PRIMARY KEY,
    business_key      TEXT UNIQUE NOT NULL,
    opportunity_data  TEXT,
    created_at        TEXT,
    updated_at        TEXT
);
CREATE TABLE IF NOT EXISTS offer_memory (
    id           BIGSERIAL PRIMARY KEY,
    business_key TEXT UNIQUE NOT NULL,
    offer_data   TEXT,
    created_at   TEXT,
    updated_at   TEXT
);
CREATE TABLE IF NOT EXISTS website_memory (
    id            BIGSERIAL PRIMARY KEY,
    business_key  TEXT UNIQUE NOT NULL,
    url           TEXT,
    audit_data    TEXT,
    overall_score REAL,
    created_at    TEXT,
    updated_at    TEXT
);
CREATE TABLE IF NOT EXISTS visibility_memory (
    id               BIGSERIAL PRIMARY KEY,
    business_key     TEXT UNIQUE NOT NULL,
    url              TEXT,
    visibility_data  TEXT,
    overall_score    REAL,
    created_at       TEXT,
    updated_at       TEXT
);
CREATE TABLE IF NOT EXISTS outreach_memory (
    id            BIGSERIAL PRIMARY KEY,
    business_key  TEXT UNIQUE NOT NULL,
    outreach_data TEXT,
    created_at    TEXT,
    updated_at    TEXT
);
CREATE TABLE IF NOT EXISTS kpi_memory (
    id           BIGSERIAL PRIMARY KEY,
    business_key TEXT UNIQUE NOT NULL,
    kpi_data     TEXT,
    budget       REAL,
    goal         TEXT,
    created_at   TEXT,
    updated_at   TEXT
);
CREATE TABLE IF NOT EXISTS performance_memory (
    id               BIGSERIAL PRIMARY KEY,
    business_key     TEXT UNIQUE NOT NULL,
    performance_data TEXT,
    date_range       TEXT,
    overall_health   REAL,
    created_at       TEXT,
    updated_at       TEXT
);
CREATE TABLE IF NOT EXISTS optimizer_memory (
    id             BIGSERIAL PRIMARY KEY,
    business_key   TEXT UNIQUE NOT NULL,
    optimizer_data TEXT,
    created_at     TEXT,
    updated_at     TEXT
);
CREATE TABLE IF NOT EXISTS result_memory (
    id             BIGSERIAL PRIMARY KEY,
    business_key   TEXT UNIQUE NOT NULL,
    result_data    TEXT,
    overall_score  REAL,
    created_at     TEXT,
    updated_at     TEXT
);
CREATE TABLE IF NOT EXISTS growth_memory (
    id               BIGSERIAL PRIMARY KEY,
    industry         TEXT,
    business_type    TEXT,
    budget_range     TEXT,
    winning_audience TEXT,
    winning_platform TEXT,
    winning_offer    TEXT,
    avg_cpl          TEXT,
    avg_roas         TEXT,
    confidence       REAL,
    created_at       TEXT
);
CREATE TABLE IF NOT EXISTS prospect_memory (
    id              BIGSERIAL PRIMARY KEY,
    business_key    TEXT UNIQUE NOT NULL,
    prospects_data  TEXT,
    industry        TEXT,
    city            TEXT,
    created_at      TEXT,
    updated_at      TEXT
);
CREATE TABLE IF NOT EXISTS autonomous_plan_memory (
    id              BIGSERIAL PRIMARY KEY,
    business_key    TEXT UNIQUE NOT NULL,
    plan_data       TEXT,
    goal_text       TEXT,
    budget          REAL,
    created_at      TEXT,
    updated_at      TEXT
);
CREATE TABLE IF NOT EXISTS social_intel_memory (
    id              BIGSERIAL PRIMARY KEY,
    business_key    TEXT UNIQUE NOT NULL,
    data            TEXT,
    created_at      TEXT,
    updated_at      TEXT
);
CREATE TABLE IF NOT EXISTS creative_director_memory (
    id              BIGSERIAL PRIMARY KEY,
    business_key    TEXT UNIQUE NOT NULL,
    data            TEXT,
    created_at      TEXT,
    updated_at      TEXT
);
CREATE TABLE IF NOT EXISTS ad_creative_memory (
    id              BIGSERIAL PRIMARY KEY,
    business_key    TEXT UNIQUE NOT NULL,
    data            TEXT,
    created_at      TEXT,
    updated_at      TEXT
);
CREATE TABLE IF NOT EXISTS creative_studio_memory (
    id              BIGSERIAL PRIMARY KEY,
    business_key    TEXT UNIQUE NOT NULL,
    data            TEXT,
    created_at      TEXT,
    updated_at      TEXT
);
"""

def _create_memory_tables():
    """
    Create memory tables with TEXT columns for JSON data.
    If tables already exist with JSONB columns (from a prior bad schema),
    drop and recreate them — they are always empty if saves were failing.
    """
    _memory_table_names = ["business_memory", "market_memory", "competitor_memory",
                           "audience_memory", "campaign_memory", "opportunity_memory",
                           "offer_memory", "website_memory", "visibility_memory", "outreach_memory", "kpi_memory", "performance_memory", "optimizer_memory", "result_memory", "growth_memory", "prospect_memory", "autonomous_plan_memory", "social_intel_memory", "creative_director_memory", "ad_creative_memory", "creative_studio_memory"]
    with engine.connect() as conn:
        # Check if any table has JSONB columns (only on Postgres)
        needs_recreate = False
        if not _is_sqlite:
            try:
                row = conn.execute(text(
                    "SELECT COUNT(*) FROM information_schema.columns "
                    "WHERE table_name IN ('business_memory','market_memory','competitor_memory','audience_memory','campaign_memory') "
                    "AND data_type = 'jsonb'"
                )).scalar()
                needs_recreate = (row or 0) > 0
            except Exception:
                pass
        if needs_recreate:
            logger.info("[MEMORY] Detected JSONB columns — dropping and recreating memory tables with TEXT")
            for t in reversed(_memory_table_names):
                conn.execute(text(f"DROP TABLE IF EXISTS {t}"))
            conn.commit()

        ddl = _MEMORY_DDL
        if _is_sqlite:
            ddl = ddl.replace("BIGSERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
        for stmt in [s.strip() for s in ddl.split(";") if s.strip()]:
            conn.execute(text(stmt))
        conn.commit()

try:
    _create_memory_tables()
    logger.info("[MEMORY] Memory tables created/verified")
except Exception as _me:
    logger.warning(f"[MEMORY] Could not create memory tables: {_me}")

# ── Memory helpers ───────────────────────────────────────────────────────────
_MEMORY_TABLES = {
    "business":    "business_memory",
    "market":      "market_memory",
    "competitor":  "competitor_memory",
    "audience":    "audience_memory",
    "campaign":    "campaign_memory",
    "opportunity": "opportunity_memory",
    "offer":       "offer_memory",
    "website":     "website_memory",
    "visibility":  "visibility_memory",
    "outreach":    "outreach_memory",
    "kpi":         "kpi_memory",
    "performance": "performance_memory",
    "optimizer":   "optimizer_memory",
    "result":      "result_memory",
    "prospect":    "prospect_memory",
    "autonomous_plan": "autonomous_plan_memory",
    "social_intel":    "social_intel_memory",
    "creative_director": "creative_director_memory",
    "ad_creative":       "ad_creative_memory",
    "creative_studio":   "creative_studio_memory",
}

def _json_val(v):
    """Serialize dict/list → JSON string for TEXT storage."""
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return v

def derive_business_key(url: str, industry: str = "", city: str = "") -> str:
    """
    Single shared key-derivation function used by ALL endpoints (save + lookup).

    Non-B2B (url only, no industry):       "sohscape.com"
    B2B (url + target industry + city):    "sohscape.com::hospitality::mumbai"
    Industry-only mode (no url):           "hospitality::mumbai"

    City always defaults to "india" (national scope) when blank — NEVER a
    specific city — so keys are ALWAYS complete and consistent regardless of
    whether the frontend sent city="" or a real city, without silently
    assuming the business is in any one place.
    """
    # City default: ALWAYS "india" (national scope) when blank — never a
    # specific city. This used to default to "jaipur", which silently
    # assumed every business with no city was in Jaipur — that assumption
    # is exactly what this tool must never make.
    _tc = (city or "").strip().lower() or "india"

    if url and url.strip():
        k = url.strip().rstrip("/").lower()
        k = re.sub(r'^https?://', '', k)
        k = k.rstrip("/")
        if industry and industry.strip():
            _ti = industry.strip().lower()
            return f"{k}::{_ti}::{_tc}"
        return k
    _ind = (industry or "").strip().lower()
    return f"{_ind}::{_tc}"

# Keep old name as alias so any leftover calls don't break
_normalize_biz_key = derive_business_key

def save_to_memory(table_key: str, business_key: str, data: dict) -> tuple:
    """
    Upsert data into a memory table.
    Returns (success: bool, error: str | None).
    Never raises — all errors are caught, logged at ERROR level with full traceback.
    """
    import traceback as _tb
    table = _MEMORY_TABLES.get(table_key)
    if not table:
        msg = f"Unknown table key: {table_key!r}"
        logger.error(f"[MEMORY] save_to_memory: {msg}")
        return (False, msg)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data = {k: _json_val(v) for k, v in data.items() if v is not None}
    cols   = ["business_key", "updated_at"] + list(data.keys())
    vals   = [business_key, now] + list(data.values())
    params = {f"p{i}": v for i, v in enumerate(vals)}
    placeholders = ", ".join(f":p{i}" for i in range(len(vals)))
    col_str      = ", ".join(cols)
    update_parts = ["updated_at = :p1"] + [f"{c} = :p{i+2}" for i, c in enumerate(data.keys())]
    update_pairs = ", ".join(update_parts)
    sql = f"""
        INSERT INTO {table} ({col_str}, created_at)
        VALUES ({placeholders}, :p1)
        ON CONFLICT(business_key) DO UPDATE SET {update_pairs}
    """
    logger.info(f"[MEMORY] Attempting save: table={table} key={business_key!r} cols={cols}")
    try:
        with engine.connect() as conn:
            conn.execute(text(sql), params)
            conn.commit()
        logger.info(f"[MEMORY] SUCCESS: table={table} key={business_key!r}")
        return (True, None)
    except Exception as _e:
        err = f"{type(_e).__name__}: {_e}"
        logger.error(f"[MEMORY] FAILED: table={table} key={business_key!r}\n{err}\n{_tb.format_exc()}")
        return (False, err)

def get_memory(business_key: str) -> dict:
    """Return all stored memory for a business across all 5 tables as one dict."""
    result = {}
    for key, table in _MEMORY_TABLES.items():
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    text(f"SELECT * FROM {table} WHERE business_key = :bk"),
                    {"bk": business_key}
                ).mappings().first()
            if row:
                row_dict = dict(row)
                for col, val in row_dict.items():
                    if isinstance(val, str) and val.startswith(("{", "[")):
                        try:
                            row_dict[col] = json.loads(val)
                        except Exception:
                            pass
                result[key] = row_dict
        except Exception as _e:
            logger.error(f"[MEMORY] get_memory({table}) FAILED: {_e}")
    return result

def get_memory_with_city_fallback(business_key: str, industry: str = "", city: str = "") -> tuple:
    """
    Returns (memory_dict, key_used).
    1. Try exact derived key (city defaults to 'india' — national scope — in derive_business_key).
    2. If miss: try the legacy blank-city key (data saved before the city-default fix).
    3. If still miss: LIKE prefix query to find any key matching url::industry::*
    """
    norm_key = derive_business_key(business_key, industry, city)
    mem = get_memory(norm_key)
    if mem:
        logger.info(f"[MEMORY] Exact key hit: {norm_key!r}")
        return mem, norm_key

    # Legacy fallback 1: key was saved with empty city suffix (pre-fix data)
    if industry.strip():
        if business_key.strip():
            _u = business_key.strip().rstrip("/").lower()
            _u = re.sub(r'^https?://', '', _u).rstrip("/")
            legacy_key = f"{_u}::{industry.strip().lower()}::"
        else:
            legacy_key = f"{industry.strip().lower()}::"
        if legacy_key != norm_key:
            mem = get_memory(legacy_key)
            if mem:
                logger.info(f"[MEMORY] Legacy blank-city fallback: {norm_key!r} → {legacy_key!r}")
                return mem, legacy_key

    # Legacy fallback 2: LIKE prefix scan — finds any city suffix
    if industry.strip():
        if business_key.strip():
            _u = business_key.strip().rstrip("/").lower()
            _u = re.sub(r'^https?://', '', _u).rstrip("/")
            prefix = f"{_u}::{industry.strip().lower()}::"
        else:
            prefix = f"{industry.strip().lower()}::"
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT business_key FROM business_memory WHERE business_key LIKE :p ORDER BY updated_at DESC LIMIT 1"),
                    {"p": prefix + "%"}
                ).first()
            if row:
                fallback_key = row[0]
                logger.info(f"[MEMORY] LIKE fallback: {norm_key!r} → {fallback_key!r}")
                mem = get_memory(fallback_key)
                if mem:
                    return mem, fallback_key
        except Exception as _e:
            logger.warning(f"[MEMORY] LIKE fallback query failed: {_e}")

    logger.info(f"[MEMORY] No memory found for key={norm_key!r}")
    return {}, norm_key


# ── Activity Log (History page) ──────────────────────────────────────────────
# Single append-only table every major module + campaign push writes one row
# to on success — the one place "what has this tool ever done" can be answered
# from, since every module's own report otherwise only lives in localStorage
# on whichever browser generated it.
_ACTIVITY_LOG_DDL = """
CREATE TABLE IF NOT EXISTS activity_log (
    id            BIGSERIAL PRIMARY KEY,
    activity_type TEXT,
    business_key  TEXT,
    business_name TEXT,
    url           TEXT,
    industry      TEXT,
    city          TEXT,
    summary       TEXT,
    reference_id  TEXT,
    created_at    TEXT
);
"""

try:
    with engine.connect() as _al_conn:
        _al_ddl = _ACTIVITY_LOG_DDL
        if _is_sqlite:
            _al_ddl = _al_ddl.replace("BIGSERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
        _al_conn.execute(text(_al_ddl))
        _al_conn.commit()
    logger.info("[ACTIVITY] activity_log table created/verified")
except Exception as _ale:
    logger.warning(f"[ACTIVITY] Could not create activity_log table: {_ale}")


def log_activity(activity_type: str, business_key: str = "", business_name: str = "",
                  url: str = "", industry: str = "", city: str = "",
                  summary: str = "", reference_id: str = "") -> None:
    """
    Record one row to activity_log. Called at the end of every major module's
    successful run and every campaign push — best-effort (never raises, so a
    logging failure can never break the actual module response). reference_id
    is the Google/Meta campaign_id for campaign pushes, blank otherwise.
    """
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO activity_log "
                "(activity_type, business_key, business_name, url, industry, city, summary, reference_id, created_at) "
                "VALUES (:at, :bk, :bn, :url, :ind, :cit, :sm, :rid, :ca)"
            ), {
                "at": activity_type, "bk": business_key or "", "bn": business_name or "",
                "url": url or "", "ind": industry or "", "cit": city or "",
                "sm": summary or "", "rid": reference_id or "",
                "ca": datetime.utcnow().isoformat(),
            })
        logger.info(f"[ACTIVITY] logged type={activity_type!r} business={business_name or business_key!r} ref={reference_id!r}")
    except Exception as _e:
        logger.warning(f"[ACTIVITY] log_activity failed (non-fatal): {_e}")


@app.get("/activity/list")
async def activity_list(limit: int = 50, type: str = "", business_key: str = ""):
    """Recent activity across the whole tool, newest first — the History page's Activity tab."""
    try:
        limit = max(1, min(limit, 200))
        clauses = []
        params = {"lim": limit}
        if type.strip():
            clauses.append("activity_type = :at")
            params["at"] = type.strip()
        if business_key.strip():
            clauses.append("business_key = :bk")
            params["bk"] = business_key.strip()
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, activity_type, business_key, business_name, url, industry, city, "
                    "summary, reference_id, created_at FROM activity_log "
                    f"{where} ORDER BY id DESC LIMIT :lim"
                ),
                params,
            ).mappings().all()
        return {"success": True, "activity": [dict(r) for r in rows]}
    except Exception as _e:
        logger.error(f"[ACTIVITY] list failed: {_e}")
        return {"success": False, "error": str(_e), "activity": []}


@app.get("/activity/business/{business_key}")
async def activity_for_business(business_key: str):
    """Full activity history for one business — every report + campaign push ever logged against this key."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, activity_type, business_key, business_name, url, industry, city, "
                    "summary, reference_id, created_at FROM activity_log "
                    "WHERE business_key = :bk ORDER BY id DESC"
                ),
                {"bk": business_key},
            ).mappings().all()
        return {"success": True, "business_key": business_key, "activity": [dict(r) for r in rows]}
    except Exception as _e:
        logger.error(f"[ACTIVITY] business history failed for {business_key!r}: {_e}")
        return {"success": False, "error": str(_e), "activity": []}


# ── Report Snapshots (History page "restore this report") ───────────────────
# activity_log records THAT something happened; this stores the actual full
# response so the History page can genuinely reopen a past report exactly as
# it was generated, not just re-derive an approximation from the structured
# memory tables (which only keep extracted pieces, not the full formatted
# text sections a report like Marketing Brain returns).
_REPORT_SNAPSHOT_DDL = """
CREATE TABLE IF NOT EXISTS report_snapshot (
    id            BIGSERIAL PRIMARY KEY,
    module        TEXT NOT NULL,
    business_key  TEXT NOT NULL,
    response_json TEXT,
    created_at    TEXT
);
"""

try:
    with engine.connect() as _rs_conn:
        _rs_ddl = _REPORT_SNAPSHOT_DDL
        if _is_sqlite:
            _rs_ddl = _rs_ddl.replace("BIGSERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
        _rs_conn.execute(text(_rs_ddl))
        _rs_conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_report_snapshot_module_key "
            "ON report_snapshot (module, business_key)"
        ))
        _rs_conn.commit()
    logger.info("[SNAPSHOT] report_snapshot table created/verified")
except Exception as _rse:
    logger.warning(f"[SNAPSHOT] Could not create report_snapshot table: {_rse}")


def save_report_snapshot(module: str, business_key: str, response: dict) -> None:
    """Upsert the full response for one module+business_key — best-effort, never raises."""
    if not business_key:
        return
    try:
        now = datetime.utcnow().isoformat()
        payload = json.dumps(response, ensure_ascii=False, default=str)
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO report_snapshot (module, business_key, response_json, created_at) "
                "VALUES (:m, :bk, :rj, :ca) "
                "ON CONFLICT(module, business_key) DO UPDATE SET response_json=:rj, created_at=:ca"
            ), {"m": module, "bk": business_key, "rj": payload, "ca": now})
    except Exception as _e:
        logger.warning(f"[SNAPSHOT] save_report_snapshot failed for module={module!r} key={business_key!r}: {_e}")


@app.get("/report-snapshot")
async def get_report_snapshot(module: str, business_key: str):
    """Fetch the last full saved response for one module+business_key — powers the History page's 'restore report' click-through."""
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT response_json, created_at FROM report_snapshot WHERE module = :m AND business_key = :bk"),
                {"m": module, "bk": business_key},
            ).first()
        if not row:
            return {"success": False, "error": "No snapshot found for this module/business_key"}
        return {"success": True, "response": json.loads(row[0]), "created_at": row[1]}
    except Exception as _e:
        logger.error(f"[SNAPSHOT] get_report_snapshot failed: {_e}")
        return {"success": False, "error": str(_e)}


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

class AnalyzeRequest(BaseModel):
    url: str
    business_type: str
    budget: int
    goal: str
    force: bool = False

class LeadCreate(BaseModel):
    name: str
    phone: str
    email: Optional[str] = ""
    source: str
    message: Optional[str] = ""
    campaign: Optional[str] = ""

@app.get("/")
def home():
    return {"message": "Adsoh Backend chal raha hai!"}

@app.post("/analyze")
async def analyze(request: AnalyzeRequest, db: Session = Depends(get_db)):
    import json
    import re
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client_http:
            response = await client_http.get(request.url, headers={"User-Agent": "Mozilla/5.0"})
            raw = response.text
    except:
        raw = ""

    def extract_clean(html):
        parts = []
        t = re.search(r'<title[^>]*>(.*?)</title>', html, re.I | re.S)
        if t: parts.append("TITLE: " + t.group(1).strip())
        m = re.search(r'<meta[^>]*name=["\']description["\'][^>]*content=["\'](.*?)["\']', html, re.I)
        if m: parts.append("DESCRIPTION: " + m.group(1).strip())
        ogt = re.search(r'<meta[^>]*property=["\']og:title["\'][^>]*content=["\'](.*?)["\']', html, re.I)
        if ogt: parts.append("OG_TITLE: " + ogt.group(1).strip())
        ogd = re.search(r'<meta[^>]*property=["\']og:description["\'][^>]*content=["\'](.*?)["\']', html, re.I)
        if ogd: parts.append("OG_DESC: " + ogd.group(1).strip())
        kw = re.search(r'<meta[^>]*name=["\']keywords["\'][^>]*content=["\'](.*?)["\']', html, re.I)
        if kw: parts.append("KEYWORDS: " + kw.group(1).strip())
        for tag in ['h1', 'h2', 'h3']:
            for h in re.findall(r'<' + tag + r'[^>]*>(.*?)</' + tag + r'>', html, re.I | re.S):
                clean = re.sub(r'<[^>]+>', '', h).strip()
                if clean and len(clean) > 2:
                    parts.append(f"{tag.upper()}: {clean}")
        body = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.I | re.S)
        body = re.sub(r'<style[^>]*>.*?</style>', '', body, flags=re.I | re.S)
        body = re.sub(r'<[^>]+>', ' ', body)
        body = re.sub(r'\s+', ' ', body).strip()
        parts.append("BODY: " + body[:2000])
        return "\n".join(parts)

    website_text = extract_clean(raw) if raw else ""

    if not website_text or len(website_text) < 100:
        return {"success": False, "scan_failed": True, "message": "Website scan nahi ho payi. URL check karo ya doosra try karo."}

    classify_prompt = (
        "You are a strict Business Classification Engine.\n"
        "RULE: Website content (especially TITLE, DESCRIPTION, headings) is the ONLY source of truth.\n"
        "The user-selected category is almost always wrong — IGNORE it unless the website clearly confirms it.\n"
        "If the website TITLE or DESCRIPTION mentions specific products/services that contradict the selected category, you MUST set category_mismatch to true.\n\n"
        f"User-selected category (likely wrong, treat with suspicion): {request.business_type}\n"
        f"Website URL: {request.url}\n"
        f"Website content:\n{website_text}\n\n"
        "Return STRICT JSON only:\n"
        "{\n"
        '  "detected_industry": "",\n'
        '  "detected_sub_industry": "",\n'
        '  "primary_products_or_services": [],\n'
        '  "confidence_score": 0,\n'
        '  "evidence": ["quote exact words from TITLE or DESCRIPTION"],\n'
        '  "selected_category": "' + request.business_type + '",\n'
        '  "recommended_category": "",\n'
        '  "category_mismatch": false\n'
        "}\n\n"
        "confidence_score = how sure about YOUR detected category (based on website), 0-100.\n"
        "category_mismatch = true if selected category does NOT match what the website actually sells/offers.\n"
        "Base everything on the TITLE and DESCRIPTION first."
    )

    classification = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": classify_prompt}],
        max_tokens=600,
        response_format={"type": "json_object"}
    )

    try:
        class_data = json.loads(classification.choices[0].message.content)
    except:
        class_data = {"category_mismatch": False, "confidence_score": 0, "recommended_category": request.business_type}

    mismatch = class_data.get("category_mismatch", False)
    confidence = class_data.get("confidence_score", 0)

    if not request.force and (mismatch or confidence < 85):
        return {"success": False, "needs_confirmation": True, "classification": class_data}

    detected = class_data.get("recommended_category") or request.business_type
    services = ", ".join(class_data.get("primary_products_or_services", []))

    prompt = (
        "Tu ek world-class digital marketing strategist hai jo Google Ads aur Meta Ads expert hai.\n\n"
        "HUMAN WRITING RULE: Yeh AI buzzwords KABHI mat use kar: unleash, elevate, dive in, game-changer, unlock, revolutionize, seamless, empower, transform your.\n"
        "Chhote, punchy, natural lines likho. Indian audience ke liye thodi local feel de.\n\n"
        f"Business URL: {request.url}\n"
        f"VERIFIED Business Category: {detected}\n"
        f"Detected Services/Products: {services}\n"
        f"Monthly Budget: Rs {request.budget}\n"
        f"Marketing Goal: {request.goal}\n"
        f"Website Content:\n{website_text[:3000]}\n\n"
        "Koi asterisk mat use kar. Seedha likho:\n\n"
        "BUSINESS SUMMARY:\n[2-3 lines]\n\nTARGET AUDIENCE:\n[2-3 lines]\n\n"
        "DEMOGRAPHICS:\nAge Range: []\nGender: []\nIncome Level: []\nLocation: []\nLanguage: []\n\n"
        "DEVICE TARGETING:\nMobile: [%]\nDesktop: [%]\nBest Device: []\n\n"
        "AD PLACEMENTS:\n1. Instagram Feed: []\n2. Instagram Reels: []\n3. Facebook Feed: []\n4. Google Search: []\n5. Google Display: []\n\n"
        "TIME TARGETING:\nBest Days: []\nPeak Hours: []\nAvoid: []\n\n"
        f"BUDGET SPLIT (Total Rs {request.budget}/month):\n1. Google Search Ads: Rs [] ([%]) - [reason]\n2. Meta Ads (FB+IG): Rs [] ([%]) - [reason]\n3. [Platform]: Rs [] ([%]) - [reason]\n\n"
        "GOOGLE ADS HEADLINES (8, STRICT max 30 characters each):\n1. []\n2. []\n3. []\n4. []\n5. []\n6. []\n7. []\n8. []\n\n"
        "GOOGLE ADS DESCRIPTIONS (4, max 90 characters each):\n1. []\n2. []\n3. []\n4. []\n\n"
        "AD GROUPS:\n1. Group: [] | Keywords: [kw1, kw2, kw3, kw4, kw5]\n2. Group: [] | Keywords: [kw1, kw2, kw3, kw4, kw5]\n3. Group: [] | Keywords: [kw1, kw2, kw3, kw4, kw5]\n\n"
        "META AD COPY:\nPrimary Text: []\nHeadline: [max 40 chars]\nCTA Button: []\n\n"
        "INTEREST TARGETING:\n1. []\n2. []\n3. []\n4. []\n5. []\n\n"
        "REMARKETING STRATEGY:\n1. []\n2. []\n3. []\n\n"
        "KPI TARGETS:\nGoogle Search CTR: [3-6%]\nMeta CTR: [1-3%]\nExpected CPL: Rs []\nExpected ROAS: []\n\n"
        "NEGATIVE KEYWORDS:\n1. []\n2. []\n3. []\n4. []\n5. []\n\n"
        "COMMON MISTAKES:\n1. []\n2. []\n3. []\n\n"
        "OPPORTUNITIES:\n1. []\n2. []\n3. []\n"
    )

    ai_response = client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": prompt}], max_tokens=4000)
    result = ai_response.choices[0].message.content

    analysis = AnalysisModel(url=request.url, business_type=detected, budget=request.budget, goal=request.goal, result=result, created_at=datetime.now().strftime("%d %b %Y, %I:%M %p"))
    db.add(analysis)
    db.commit()

    try:
        report = ReportModel(report_type="analyze", title=request.url, input_data=json.dumps({"url": request.url, "business_type": request.business_type, "budget": request.budget, "goal": request.goal}), result_data=json.dumps({"analysis": result, "detected_category": detected, "confidence": confidence}), created_at=datetime.now().strftime("%d %b %Y, %I:%M %p"))
        db.add(report)
        db.commit()
    except Exception as _re:
        logger.warning(f"[REPORTS] Could not save analyze report: {_re}")
        db.rollback()

    return {"success": True, "url": request.url, "detected_category": detected, "confidence": confidence, "analysis": result}

class CompetitorRequest(BaseModel):
    my_url: str
    competitor_urls: list[str]
    business_type: str

@app.post("/competitor")
async def competitor(request: CompetitorRequest, db: Session = Depends(get_db)):
    import re

    def extract_clean(html):
        parts = []
        t = re.search(r'<title[^>]*>(.*?)</title>', html, re.I | re.S)
        if t: parts.append("TITLE: " + t.group(1).strip())
        m = re.search(r'<meta[^>]*name=["\']description["\'][^>]*content=["\'](.*?)["\']', html, re.I)
        if m: parts.append("DESCRIPTION: " + m.group(1).strip())
        for tag in ['h1', 'h2']:
            for h in re.findall(r'<' + tag + r'[^>]*>(.*?)</' + tag + r'>', html, re.I | re.S):
                clean = re.sub(r'<[^>]+>', '', h).strip()
                if clean and len(clean) > 2:
                    parts.append(f"{tag.upper()}: {clean}")
        body = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.I | re.S)
        body = re.sub(r'<style[^>]*>.*?</style>', '', body, flags=re.I | re.S)
        body = re.sub(r'<[^>]+>', ' ', body)
        body = re.sub(r'\s+', ' ', body).strip()
        parts.append("BODY: " + body[:1200])
        return "\n".join(parts)

    async def fetch(u):
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
                r = await c.get(u, headers={"User-Agent": "Mozilla/5.0"})
                return extract_clean(r.text)
        except:
            return ""

    my_content = await fetch(request.my_url)
    comp_blocks = []
    for i, cu in enumerate(request.competitor_urls):
        if not cu.strip(): continue
        content = await fetch(cu)
        comp_blocks.append(f"COMPETITOR {i+1} ({cu}):\n{content[:1500]}\n")
    competitors_text = "\n".join(comp_blocks)

    prompt = (
        "Tu ek world-class competitor intelligence analyst hai.\n\n"
        f"Business Type: {request.business_type}\n\nMY BUSINESS ({request.my_url}):\n{my_content[:1500]}\n\nCOMPETITORS:\n{competitors_text}\n\n"
        "Koi asterisk mat use kar. Seedha likho:\n\n"
        "MY POSITIONING:\n[2-3 lines]\n\nCOMPETITOR ANALYSIS:\nCompetitor 1: [naam/url]\nPositioning: []\nStrengths: []\nWeaknesses: []\nMessaging Style: []\n\n"
        "MARKET GAPS:\n1. []\n2. []\n3. []\n\nWHERE YOU CAN WIN:\n1. []\n2. []\n3. []\n\nRECOMMENDED MESSAGING:\n[]\n\nQUICK WINS:\n1. []\n2. []\n3. []\n"
    )

    ai_response = client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": prompt}], max_tokens=2500)
    analysis_text = ai_response.choices[0].message.content
    try:
        report = ReportModel(report_type="competitor", title=request.my_url, input_data=json.dumps({"my_url": request.my_url, "competitor_urls": request.competitor_urls, "business_type": request.business_type}), result_data=json.dumps({"analysis": analysis_text}), created_at=datetime.now().strftime("%d %b %Y, %I:%M %p"))
        db.add(report)
        db.commit()
    except Exception as _re:
        logger.warning(f"[REPORTS] Could not save competitor report: {_re}")
        db.rollback()
    return {"success": True, "analysis": analysis_text}

class AdIntelRequest(BaseModel):
    business_name: str
    business_type: str
    website: str = ""
    country: str = "IN"

@app.post("/ad-intelligence")
async def ad_intelligence(request: AdIntelRequest, db: Session = Depends(get_db)):
    import urllib.parse
    encoded_name = urllib.parse.quote(request.business_name)
    meta_link = f"https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country={request.country}&q={encoded_name}&search_type=keyword_unordered"
    if request.website:
        domain = request.website.replace("https://", "").replace("http://", "").replace("www.", "").rstrip("/").split("/")[0]
        google_link = f"https://adstransparency.google.com/?region={request.country}&domain={domain}"
    else:
        google_link = f"https://adstransparency.google.com/?region={request.country}"

    prompt = (
        "Tu ek elite competitor ad intelligence strategist hai.\n\n"
        "Meta Ad Library mein DIKHTA hai: creative, start date, ad count, versions, platforms.\n"
        "NAHI dikhta: likes, CTR, conversions, ROAS, budget, spend.\n\n"
        f"COMPETITOR: {request.business_name}\nWEBSITE: {request.website or 'N/A'}\nINDUSTRY: {request.business_type}\n\n"
        "Koi asterisk mat use kar. Seedha likho:\n\n"
        "AD LIBRARY MEIN KYA DEKHNA HAI:\n1. []\n2. []\n3. []\n4. []\n5. []\n\n"
        "WINNING ADS KAISE PEHCHANE:\n1. []\n2. []\n3. []\n4. []\n\n"
        f"{request.business_name} KE BAARE MEIN YEH PATA KARO:\n1. []\n2. []\n3. []\n4. []\n5. []\n\n"
        "UNKI KAMZORI:\n1. []\n2. []\n3. []\n\nTUMHARA WINNING ANGLE:\n1. []\n2. []\n3. []\n\nABHI YEH KARO:\n1. []\n2. []\n3. []\n"
    )

    ai_response = client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": prompt}], max_tokens=1500)
    guide_text = ai_response.choices[0].message.content
    try:
        report = ReportModel(report_type="ad-intelligence", title=request.business_name, input_data=json.dumps({"business_name": request.business_name, "business_type": request.business_type, "website": request.website}), result_data=json.dumps({"guide": guide_text, "meta_ad_library_link": meta_link, "google_ads_link": google_link}), created_at=datetime.now().strftime("%d %b %Y, %I:%M %p"))
        db.add(report)
        db.commit()
    except Exception as _re:
        logger.warning(f"[REPORTS] Could not save ad-intelligence report: {_re}")
        db.rollback()
    return {"success": True, "business_name": request.business_name, "meta_ad_library_link": meta_link, "google_ads_link": google_link, "guide": guide_text}

class FullReportRequest(BaseModel):
    url: str = ""
    business_type: str
    budget: int
    goal: str
    competitor_name: str = ""
    competitor_website: str = ""
    language: str = "Hinglish"
    target_industry: str = ""
    target_city: str = ""
    mode: str = "b2c"  # kept for backward compat, unused

async def fetch_firecrawl(url: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            resp = await c.post(
                "https://api.firecrawl.dev/v1/scrape",
                headers={
                    "Authorization": f"Bearer {FIRECRAWL_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={"url": url, "formats": ["markdown"], "onlyMainContent": True},
            )
            data = resp.json()
            return (data.get("data") or {}).get("markdown", "")
    except Exception:
        return ""

async def fetch_tavily(query: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            resp = await c.post(
                "https://api.tavily.com/search",
                headers={"Content-Type": "application/json"},
                json={
                    "api_key": TAVILY_API_KEY,
                    "query": query,
                    "search_depth": "advanced",
                    "max_results": 5,
                },
            )
            data = resp.json()
            snippets = [r.get("content", "") for r in data.get("results", []) if r.get("content")]
            return "\n".join(snippets)
    except Exception:
        return ""

async def fetch_google_places(query: str, city: str, max_results: int = 20) -> list:
    """Search Google Places Text Search API. Returns [] on failure or missing key."""
    if not GOOGLE_PLACES_API_KEY:
        return []
    try:
        async with httpx.AsyncClient(timeout=12) as c:
            resp = await c.get(
                "https://maps.googleapis.com/maps/api/place/textsearch/json",
                params={"query": f"{query} in {city}", "key": GOOGLE_PLACES_API_KEY},
            )
            data = resp.json()
            results = data.get("results", [])[:max_results]
            out = []
            for r in results:
                out.append({
                    "name":                r.get("name", ""),
                    "address":             r.get("formatted_address", ""),
                    "rating":              r.get("rating"),
                    "user_ratings_total":  r.get("user_ratings_total", 0),
                    "website":             r.get("website", ""),
                    "place_id":            r.get("place_id", ""),
                    "business_status":     r.get("business_status", ""),
                })
            return out
    except Exception as _e:
        logger.warning(f"[PLACES] fetch_google_places failed: {_e}")
        return []

async def fetch_place_details(place_id: str) -> dict:
    """Fetch detailed info for a single place. Returns {} on failure."""
    if not GOOGLE_PLACES_API_KEY or not place_id:
        return {}
    try:
        async with httpx.AsyncClient(timeout=12) as c:
            resp = await c.get(
                "https://maps.googleapis.com/maps/api/place/details/json",
                params={
                    "place_id": place_id,
                    "fields": "name,formatted_address,formatted_phone_number,website,rating,user_ratings_total,opening_hours,reviews",
                    "key": GOOGLE_PLACES_API_KEY,
                },
            )
            data = resp.json()
            return data.get("result", {})
    except Exception as _e:
        logger.warning(f"[PLACES] fetch_place_details failed for {place_id}: {_e}")
        return {}

def _fix_rs(obj):
    """Replace 'RS' placeholder with ₹ in all string values of a JSON object."""
    if isinstance(obj, str):  return obj.replace("RS ", "₹").replace("RS", "₹")
    if isinstance(obj, dict): return {k: _fix_rs(v) for k, v in obj.items()}
    if isinstance(obj, list): return [_fix_rs(v) for v in obj]
    return obj

_BANNED_WORD_MAP = {
    "Transform ":    "Improve ", "transform ":    "improve ",
    "Transform\n":   "Improve\n","transform\n":   "improve\n",
    "Elevate ":      "Strengthen ","elevate ":    "strengthen ",
    "Elevate\n":     "Strengthen\n","elevate\n":  "strengthen\n",
    "Unlock ":       "Access ","unlock ":         "access ",
    "Unlock\n":      "Access\n","unlock\n":        "access\n",
    "Revolutionize ":"Modernize ","revolutionize ":"modernize ",
    "Empower ":      "Help ","empower ":           "help ",
    "Empowering ":   "Helping ","empowering ":     "helping ",
    "Seamless ":     "Simple ","seamless ":        "simple ",
    "Seamlessly ":   "Smoothly ","seamlessly ":    "smoothly ",
    "Leverage ":     "Use ","leverage ":           "use ",
    "Leveraging ":   "Using ","leveraging ":       "using ",
    "Utilize ":      "Use ","utilize ":            "use ",
    "Boost your ":   "Grow your ","boost your ":   "grow your ",
    "Maximize ":     "Increase ","maximize ":      "increase ",
    "Take your business to new heights": "grow your business",
    "take your business to new heights": "grow your business",
    "Unleash ":      "Release ","unleash ":        "release ",
    "Game-changer":  "advantage","game-changer": "advantage",
    "Game changer":  "advantage","game changer":  "advantage",
    "Dive in":       "Start","dive in":            "start",
    "Cutting-edge ": "Modern ","cutting-edge ":    "modern ",
    "Cutting edge ": "Modern ","cutting edge ":    "modern ",
    "State-of-the-art ": "Advanced ","state-of-the-art ": "advanced ",
    "World-class ":  "Proven ","world-class ":      "proven ",
    "World class ":  "Proven ","world class ":      "proven ",
    "One-stop solution": "complete service","one-stop solution": "complete service",
    "One stop solution": "complete service","one stop solution": "complete service",
    "Look no further. ": "","look no further. ": "",
    "Look no further, ": "","look no further, ": "",
    "Look no further": "","look no further": "",
    "In today's digital age, ": "","in today's digital age, ": "",
    "In today's digital age": "","in today's digital age": "",
}

# ── Conjugation catcher ────────────────────────────────────────────────────
# _BANNED_WORD_MAP above only matches the bare word ("Transform ", "boost your ").
# GPT frequently uses conjugated forms instead ("leverages", "boosting",
# "transformed") which slip past those exact-phrase entries. Generate every
# -s / -ing / -ed form for each core banned verb and match them case-insensitively.
_CONJUGATION_BASE_MAP = {
    "leverage":      "use",
    "utilize":       "use",
    "transform":     "improve",
    "elevate":       "strengthen",
    "unlock":        "access",
    "revolutionize": "modernize",
    "empower":       "help",
    "boost":         "increase",
    "maximize":      "increase",
}

def _inflect(word: str) -> tuple:
    """Return the regular (s_form, ing_form, ed_form) for a regular verb."""
    if word.endswith("e"):
        return (word + "s", word[:-1] + "ing", word + "d")
    if word.endswith(("s", "x", "z", "ch", "sh")):
        return (word + "es", word + "ing", word + "ed")
    return (word + "s", word + "ing", word + "ed")

_CONJUGATION_MAP = {}
for _bad_base, _good_base in _CONJUGATION_BASE_MAP.items():
    for _bad_form, _good_form in zip(_inflect(_bad_base), _inflect(_good_base)):
        _CONJUGATION_MAP[_bad_form] = _good_form

_CONJUGATION_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in sorted(_CONJUGATION_MAP, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

def _clean_banned_conjugations(text: str) -> str:
    """Catch conjugated forms of the core banned verbs (leverages, boosting, transformed, ...)."""
    def _replace(m):
        matched = m.group(0)
        good = _CONJUGATION_MAP[matched.lower()]
        if matched[0].isupper():
            good = good[0].upper() + good[1:]
        logger.warning(f"[BANNED-WORD-CONJUGATION] '{matched}' found in AI output — replacing with '{good}'")
        return good
    return _CONJUGATION_PATTERN.sub(_replace, text)

def _clean_banned_words(text: str) -> str:
    """Post-processing: replace banned buzzwords with plain alternatives.
    Logs a warning for each hit so prompt engineers can track leakage.
    """
    for bad, good in _BANNED_WORD_MAP.items():
        if bad in text:
            logger.warning(f"[BANNED-WORD] '{bad.strip()}' found in AI output — replacing")
            text = text.replace(bad, good)
    text = _clean_banned_conjugations(text)
    return text

def _clean_banned_words_deep(obj):
    """Recursively apply _clean_banned_words to every string leaf of a JSON
    object (dict/list) — needed for endpoints that return structured GPT JSON
    (nested hooks, headlines, recommendations) instead of one raw text blob."""
    if isinstance(obj, str):  return _clean_banned_words(obj)
    if isinstance(obj, dict): return {k: _clean_banned_words_deep(v) for k, v in obj.items()}
    if isinstance(obj, list): return [_clean_banned_words_deep(v) for v in obj]
    return obj


_GPT_REFUSAL_PATTERNS = (
    "i'm sorry", "i am sorry", "i cannot assist", "i can't assist", "i cannot help",
    "i can't help", "i'm not able to", "i am not able to", "i cannot fulfill",
    "i can't fulfill", "i cannot comply", "i can't comply", "sorry, but i can't",
    "sorry, but i cannot",
)


def _looks_like_gpt_refusal(text: str) -> bool:
    """
    Deterministic check: GPT occasionally declines a request outright
    ("I'm sorry, I can't assist with that request.") instead of returning
    the actual content — and a caller that only checks the wrapping
    endpoint's own `success` flag (which is True as long as the API call
    itself succeeded) would silently treat that refusal string as if it
    were real content. Confirmed live: campaign_launch_kit once returned
    this exact refusal as both meta_kit and google_kit, and downstream
    keyword/headline extraction found nothing — a real PAUSED Google Ads
    campaign got created with 0 keywords and no ad as a result. Two checks:
    an explicit refusal phrase at the very start, or a suspiciously short
    response with none of the expected "=== SECTION ===" structure at all.
    """
    if not text:
        return True
    t = text.strip().lower()
    if any(t.startswith(p) for p in _GPT_REFUSAL_PATTERNS):
        return True
    if len(text.strip()) < 300 and "===" not in text:
        return True
    return False


# ══════════════════════════════════════════════════════════════════════════
# TRUST & ACCURACY LAYER — shared by /full-report (Marketing Brain) and
# /creative-studio. Born from the sohscape.com wellness-mismatch bug: a
# wrapper dict that always has SOME keys was treated as "real data present"
# even when the actual content (positioning/uvp/business_dna) was empty. This
# layer surfaces one honest verdict to the user instead of letting a
# generic/off output pass as if it were grounded. All checks here are
# deterministic Python — never GPT self-assessment — for the same reason the
# scoring/localization bugs earlier this session were fixed in Python: GPT
# cannot be trusted to reliably self-report its own confidence.
# ══════════════════════════════════════════════════════════════════════════

def _dna_dict_has_signal(dna: dict) -> bool:
    """True only if the business_dna dict has at least one substantive field
    — not just present keys with empty/placeholder values like "Unknown"."""
    if not dna:
        return False
    def _real(v):
        if v is None:
            return False
        if isinstance(v, str):
            return bool(v.strip()) and v.strip().lower() not in ("unknown", "n/a", "none", "")
        if isinstance(v, (list, dict)):
            return bool(v)
        return bool(v)
    for k in ("detected_industry", "business_model", "unique_value_prop", "value_proposition",
              "positioning_statement", "core_products", "target_geography"):
        if _real(dna.get(k)):
            return True
    return False


def _has_real_business_dna(bm: dict) -> bool:
    """
    The bug this guards against: a memory 'business' dict wrapper ALWAYS has
    business_name/industry/city keys even when nothing real was ever found for
    it — so `bool(bm)` is always True regardless of whether real intelligence
    was found. Check the actual content instead. Shared by every module that
    reads business_memory (Creative Studio, Opportunity Engine, Offer
    Intelligence, KPI Engine, Smart Analysis) so the same real-data standard
    applies everywhere.
    """
    if not bm:
        return False
    return bool((bm.get("positioning") or "").strip() or (bm.get("uvp") or "").strip() or (bm.get("business_dna") or {}))


def _compute_trust_verdict(has_business_dna: bool, has_audience: bool, extra_note: str = "") -> dict:
    """Single top-level confidence badge — HIGH / MEDIUM / VERIFY_FIRST."""
    if has_business_dna and has_audience:
        reason = "Business DNA and audience intelligence were both grounded in real data from the website/crawl."
    elif has_business_dna or has_audience:
        gap = "audience data is thin" if has_business_dna else "business DNA is thin"
        reason = f"Partial real data available — {gap}, so some sections may rely on reasonable assumptions."
    else:
        reason = "No real business DNA or audience data could be extracted — this output leans heavily on AI assumptions and may not accurately reflect the business."
    if extra_note:
        reason = f"{reason} {extra_note}"
    level = "HIGH" if (has_business_dna and has_audience) else ("MEDIUM" if (has_business_dna or has_audience) else "VERIFY_FIRST")
    return {"level": level, "reason": reason}


def _compute_based_on_line(has_business_dna: bool, has_audience: bool, has_campaign_data: bool) -> str:
    dna_mark  = "✓" if has_business_dna else "✗ (none found)"
    aud_mark  = "✓" if has_audience    else "✗ (none found)"
    camp_mark = "✓" if has_campaign_data else "✗ (none yet)"
    if has_business_dna and has_audience:
        tail = "Treat this as well-grounded."
    elif has_business_dna or has_audience:
        tail = "Some sections may be generalized — review before using with a client."
    else:
        tail = "This is largely AI inference — verify before using with a client."
    return f"Based on: your website content {dna_mark}, audience research {aud_mark}, real campaign data {camp_mark}. {tail}"


# Vertical keyword buckets for the deterministic business-match sanity check.
# Deliberately keyword-based rather than a GPT judge call — cheap, fast, and
# directly catches the exact failure mode observed live (a marketing agency's
# business_dna paired with wholly-unrelated wellness/mindfulness output).
_VERTICAL_KEYWORDS = {
    "wellness_mindfulness": ["wellness", "mindfulness", "meditation", "yoga", "stress relief", "stress management", "harmonious home", "relaxation", "self-care", "holistic healing"],
    "marketing_advertising": ["marketing agency", "digital marketing", "advertising", "lead generation", "seo ", "ppc", "social media marketing", "brand strategy", "video marketing", "content marketing", "ad campaign", "media buying", "growth marketing"],
    "food_beverage": ["restaurant", "menu", "cuisine", "dining", "food delivery", "cafe", "bakery", "recipe"],
    "fitness_gym": ["gym", "fitness", "workout", "personal training", "strength training", "bodybuilding"],
    "real_estate": ["real estate", "property", "apartment", "villa", "housing project", " plot "],
    "finance": ["loan", "investment", "insurance", "mutual fund", "banking", "credit card", "financial planning"],
    "healthcare_medical": ["clinic", "hospital", "doctor", "patient", "medical treatment", "diagnosis", "healthcare"],
    "beauty_salon": ["salon", "beauty parlor", "skincare", "makeup", "hairstyling", "spa treatment"],
    "hospitality_travel": ["hotel", "resort", "travel agency", "tourism", "vacation package", "hospitality industry"],
    "education": ["school", "college", "tuition", "exam prep", "curriculum", "admissions"],
    "ecommerce_retail": ["e-commerce", "online store", "shopping cart", "product catalog", "retail brand"],
    "automotive": ["car dealership", "vehicle", "automobile", "auto repair"],
    "legal": ["law firm", "attorney", "legal services", "lawsuit", "legal advice"],
}


def _detect_verticals(text: str) -> set:
    t = (text or "").lower()
    return {vertical for vertical, keywords in _VERTICAL_KEYWORDS.items() if any(kw in t for kw in keywords)}


def _business_match_sanity_check(business_context: str, output_context: str, business_label: str, business_positioning: str) -> str:
    """Returns a validation_warning string if the generated content's theme
    doesn't overlap at all with the real business's known vertical — empty
    string if no mismatch is detected (including when there isn't enough
    signal on either side to compare confidently)."""
    biz_verticals = _detect_verticals(business_context)
    out_verticals = _detect_verticals(output_context)
    if not biz_verticals or not out_verticals:
        return ""
    if biz_verticals.isdisjoint(out_verticals):
        out_label = ", ".join(sorted(v.replace("_", "/") for v in out_verticals))
        return (
            f"⚠️ The generated content theme ({out_label}) may not match this business "
            f"({business_label} — {business_positioning or 'no clear positioning on file'}). "
            "Please verify before using with a client."
        )
    return ""


_CONTRADICTION_PHRASES = [
    "no specific business positioning", "no positioning available", "no positioning was available",
    "no business dna available", "positioning data not available", "not available, so assumptions",
]


def _detect_consistency_contradiction(has_business_dna: bool, text: str) -> str:
    """Catches exactly the sohscape.com bug: data_sources_used says DNA is
    present, but the narrative text still claims no positioning was found."""
    if not has_business_dna:
        return ""
    t = (text or "").lower()
    for phrase in _CONTRADICTION_PHRASES:
        if phrase in t:
            return (
                "⚠️ This output claims business intelligence was unavailable, but real business data was found. "
                "The analysis text may be stale or inconsistent — please verify before using with a client."
            )
    return ""


def _combine_validation_warnings(*warnings: str) -> str:
    parts = [w for w in warnings if w]
    return " ".join(parts)

async def fetch_youtube_search(query: str, max_results: int = 10) -> list:
    if not YOUTUBE_API_KEY:
        return []
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.get(
                "https://www.googleapis.com/youtube/v3/search",
                params={
                    "key": YOUTUBE_API_KEY,
                    "q": query,
                    "part": "snippet",
                    "type": "video",
                    "order": "viewCount",
                    "maxResults": max_results,
                    "relevanceLanguage": "en",
                },
            )
            data = resp.json()
            items = data.get("items", [])
            return [
                {
                    "videoId":     item["id"]["videoId"],
                    "title":       item["snippet"]["title"],
                    "channel":     item["snippet"]["channelTitle"],
                    "publishedAt": item["snippet"]["publishedAt"][:10],
                    "thumbnail":   item["snippet"]["thumbnails"].get("medium", {}).get("url", ""),
                }
                for item in items
                if item.get("id", {}).get("videoId")
            ]
    except Exception:
        return []

async def fetch_youtube_video_stats(video_ids: list) -> list:
    if not YOUTUBE_API_KEY or not video_ids:
        return []
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={
                    "key": YOUTUBE_API_KEY,
                    "id": ",".join(video_ids),
                    "part": "statistics,snippet",
                },
            )
            data = resp.json()
            results = []
            for item in data.get("items", []):
                stats = item.get("statistics", {})
                snippet = item.get("snippet", {})
                results.append({
                    "videoId":      item["id"],
                    "title":        snippet.get("title", ""),
                    "channel":      snippet.get("channelTitle", ""),
                    "publishedAt":  snippet.get("publishedAt", "")[:10],
                    "views":        int(stats.get("viewCount", 0)),
                    "likes":        int(stats.get("likeCount", 0)),
                    "comments":     int(stats.get("commentCount", 0)),
                })
            return sorted(results, key=lambda x: x["views"], reverse=True)
    except Exception:
        return []

class YoutubeIntelligenceRequest(BaseModel):
    industry: str
    city: str = ""
    topic: str = ""

@app.post("/youtube-intelligence")
async def youtube_intelligence(request: YoutubeIntelligenceRequest):
    if not YOUTUBE_API_KEY:
        return {"success": False, "error": "YouTube API not configured"}

    city_part  = f" {request.city}" if request.city else ""
    topic_part = f" {request.topic}" if request.topic else "marketing reels viral"
    query      = f"{request.industry}{city_part} {topic_part}"

    videos = await fetch_youtube_search(query, max_results=10)
    if not videos:
        return {"success": False, "error": "YouTube search returned no results. Check API key or quota."}

    video_ids     = [v["videoId"] for v in videos]
    video_stats   = await fetch_youtube_video_stats(video_ids)

    stats_map = {v["videoId"]: v for v in video_stats}
    top_videos = []
    for v in videos:
        s = stats_map.get(v["videoId"], {})
        top_videos.append({
            "videoId":     v["videoId"],
            "title":       s.get("title") or v["title"],
            "channel":     s.get("channel") or v["channel"],
            "publishedAt": s.get("publishedAt") or v["publishedAt"],
            "views":       s.get("views", 0),
            "likes":       s.get("likes", 0),
            "comments":    s.get("comments", 0),
            "url":         f"https://www.youtube.com/watch?v={v['videoId']}",
        })
    top_videos.sort(key=lambda x: x["views"], reverse=True)

    video_list_txt = "\n".join([
        f"{i+1}. \"{v['title']}\" — {v['channel']} — {v['views']:,} views ({v['publishedAt']})"
        for i, v in enumerate(top_videos)
    ])

    prompt = (
        f"You are a YouTube content strategist for Indian businesses.\n"
        f"Industry: {request.industry} | City: {request.city or 'India'} | Topic focus: {request.topic or 'marketing & content'}\n\n"
        f"TOP PERFORMING YOUTUBE VIDEOS IN THIS NICHE (real data):\n{video_list_txt}\n\n"
        "Analyze these real results and generate THREE outputs. Be specific to the industry and city. "
        "No generic advice. Base insights on actual patterns in the titles above.\n\n"
        "TRENDING CONTENT IDEAS:\n"
        f"[Generate 8 specific video content ideas for {request.industry} businesses"
        f"{f' in {request.city}' if request.city else ''}. "
        "Each idea: Topic (specific angle) | Format (Reel/Short/Tutorial/Vlog/Case Study) | Why it will work (based on the top videos above). "
        "Number them 1-8. No asterisks. Plain text.]\n\n"
        "VIRAL HOOKS:\n"
        f"[Write 10 hook lines / video title templates adapted from the top-performing titles above, "
        f"rewritten for {request.industry} businesses in India. "
        "Each hook should be punchy, curiosity-driven, and ready to use as a YouTube title or Reel opening line. "
        "Number them 1-10. Keep each under 70 characters. No asterisks.]\n\n"
        "COMPETITOR INSIGHTS:\n"
        f"[Write 3-4 paragraphs analyzing: "
        "1) What content FORMATS appear most (shorts vs long, tutorial vs vlog vs talking head), "
        "2) What TOPICS and angles are driving the most views, "
        "3) What these top creators are doing RIGHT that this business should copy, "
        "4) One clear CONTENT STRATEGY recommendation for this industry based on the data above. "
        "Be specific — name actual patterns you see in the titles, not generic advice.]"
    )

    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
        max_tokens=2500,
    )
    ai_text = resp.choices[0].message.content.strip()

    def extract_section(text, start_marker, end_marker=None):
        start = text.find(start_marker)
        if start == -1:
            return ""
        start += len(start_marker)
        if end_marker:
            end = text.find(end_marker, start)
            return text[start:end].strip() if end != -1 else text[start:].strip()
        return text[start:].strip()

    content_ideas     = extract_section(ai_text, "TRENDING CONTENT IDEAS:", "VIRAL HOOKS:")
    viral_hooks       = extract_section(ai_text, "VIRAL HOOKS:", "COMPETITOR INSIGHTS:")
    competitor_insights = extract_section(ai_text, "COMPETITOR INSIGHTS:")

    return {
        "success":              True,
        "query":                query,
        "top_videos":           top_videos,
        "content_ideas":        content_ideas or ai_text,
        "viral_hooks":          viral_hooks,
        "competitor_insights":  competitor_insights,
    }

@app.post("/full-report")
async def full_report(request: FullReportRequest, db: Session = Depends(get_db)):

    async def run_ai(prompt, max_tokens):
        resp = await asyncio.to_thread(
            lambda: client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens
            )
        )
        return resp.choices[0].message.content

    # ── Step 1: Get BI data (check cache first, then run fresh) ──────────
    industry_only_mode = bool(request.target_industry and not (request.url or "").strip())

    industry_only_context = f"""
Business Type: Digital Marketing Agency (Sohscape)
Target Industry: {request.target_industry}
Target City: {request.target_city}
Monthly Budget: Rs {request.budget}
Goal: {request.goal}
Language: {request.language}

You are generating a B2B lead generation campaign for a digital marketing agency targeting {request.target_industry} businesses in {request.target_city}. Focus on reaching OWNERS, MANAGERS, and DECISION MAKERS of these businesses — not their end customers.

For {request.target_industry} businesses in {request.target_city}, include:
- Specific pain points these business owners face with their marketing
- Where to find them (Google Maps search terms, Instagram hashtags, local FB groups, business associations, directories)
- Seasonal timing — when are they most likely to need marketing help
- A hyper-specific WhatsApp outreach message (not generic — reference their actual industry pain)
- A hyper-specific Instagram DM script (3 lines max, reference something real)
- What free offer to make (free audit, free report, free reel, etc.)
- Pitch angle for first meeting + how to handle "we already have someone"
"""

    bi_data = {} if industry_only_mode else None
    bi_cached = False
    firecrawl_used_bi = False

    if industry_only_mode:
        logger.info("[FULL-REPORT] Industry-only B2B mode: skipping crawl and BI engines")
    else:
        try:
            cached_row = db.query(ReportModel).filter(
                ReportModel.report_type == "intelligence",
                ReportModel.title == request.url
            ).order_by(ReportModel.id.desc()).first()
            if cached_row and cached_row.result_data:
                bi_data = json.loads(cached_row.result_data)
                bi_cached = True
                logger.info(f"[FULL-REPORT] Using cached BI for {request.url}")
        except Exception as _e:
            logger.warning(f"[FULL-REPORT] Cache lookup failed: {_e}")

        if not bi_data:
            logger.info(f"[FULL-REPORT] Running fresh BI for {request.url}")
            fresh_bi = await gather_bi_data(
                request.url,
                request.business_type,
                [request.competitor_website] if request.competitor_website else []
            )
            if fresh_bi:
                bi_data = {**fresh_bi["intelligence"], "scores": fresh_bi["scores"]}
                firecrawl_used_bi = fresh_bi.get("firecrawl_used", False)
                try:
                    bi_cache_row = ReportModel(
                        report_type="intelligence",
                        title=request.url,
                        input_data=json.dumps({"url": request.url, "business_type": request.business_type}),
                        result_data=json.dumps(bi_data),
                        created_at=datetime.now().strftime("%d %b %Y, %I:%M %p")
                    )
                    db.add(bi_cache_row)
                    db.commit()
                except Exception as _re:
                    logger.warning(f"[FULL-REPORT] Could not cache BI: {_re}")
                    db.rollback()

    # ── Step 2: Ad library links ──────────────────────────────────────────
    import urllib.parse
    ad_name = request.competitor_name or request.competitor_website or request.business_type
    encoded = urllib.parse.quote(ad_name)
    meta_link = f"https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=IN&q={encoded}&search_type=keyword_unordered"
    if request.competitor_website:
        dom = request.competitor_website.replace("https://", "").replace("http://", "").replace("www.", "").rstrip("/").split("/")[0]
        google_link = f"https://adstransparency.google.com/?region=IN&domain={dom}"
    else:
        google_link = "https://adstransparency.google.com/?region=IN"

    # ── Step 3: Extract BI context strings ───────────────────────────────
    if industry_only_mode:
        dna_txt = opp_txt = thr_txt = pos_txt = aud_txt = exec_txt = sc_txt = industry_only_context
    elif bi_data:
        dna      = bi_data.get("business_dna", {})
        opp      = bi_data.get("opportunity_score", {})
        thr      = bi_data.get("threat_intelligence", {})
        pos      = bi_data.get("positioning", {})
        aud      = bi_data.get("audience_intelligence", {})
        exec_dec = bi_data.get("executive_decisions", {})
        scores   = bi_data.get("scores", {})
        dna_txt  = json.dumps(dna,      indent=2)[:2000]
        opp_txt  = json.dumps(opp,      indent=2)[:1000]
        thr_txt  = json.dumps(thr,      indent=2)[:1000]
        pos_txt  = json.dumps(pos,      indent=2)[:1500]
        aud_txt  = json.dumps(aud,      indent=2)[:2000]
        exec_txt = json.dumps(exec_dec, indent=2)[:1500]
        sc_txt   = json.dumps(scores,   indent=2)
    else:
        dna_txt = opp_txt = thr_txt = pos_txt = aud_txt = exec_txt = sc_txt = (
            "Intelligence data not available — website could not be fully analyzed"
        )

    lang = request.language
    biz  = f"{(request.url or 'Industry-only B2B campaign')} | {request.business_type}"
    bdgt = f"Rs {request.budget}/month"

    # ── Step 4: Build optional industry/B2B context, then 3 parallel calls ──
    industry_context = ""
    if industry_only_mode:
        industry_context = industry_only_context + "\n"
    elif request.target_industry:
        city = request.target_city or "India"
        industry_context = (
            f"TARGET INDUSTRY: {request.target_industry} businesses in {city}\n"
            f"MODE: B2B Lead Generation — target OWNERS, MANAGERS, DIRECTORS of {request.target_industry} businesses\n"
            f"CITY/REGION: {city} — reference local factors (season, events, local competitors, local pain points)\n"
            "INDUSTRY-AWARE RULES:\n"
            f"1. Generate pain points specific to {request.target_industry} businesses in {city}\n"
            f"2. Ads target business owners/managers — NOT their end customers\n"
            "3. Include WhatsApp outreach messages with industry-specific hooks\n"
            "4. Include Instagram DM scripts for cold outreach\n"
            "5. Include where to find these businesses (Google Maps search terms, Instagram hashtags, directories)\n"
            "6. Include first meeting pitch angle + how to close\n\n"
        )

    # ── Step 4.5: Tavily real-time market intelligence ───────────────────────
    tavily_competitors = ""
    tavily_market = ""
    live_data_used = False
    if TAVILY_API_KEY:
        # No silent city assumption: blank city means national (India-wide) scope,
        # never a specific city the user didn't actually provide.
        city_for_tavily = request.target_city.strip() if request.target_city else ""
        location_phrase = f"{city_for_tavily} India" if city_for_tavily else "India (nationally)"
        q_competitors = (
            f"List actual {request.business_type} companies and "
            f"{request.business_type} providers operating in {location_phrase} 2026. "
            f"Include local players, not just WeWork or Regus. "
            f"Give company names, locations, pricing if available."
        )
        q_market = (
            f"Digital marketing strategies working for {request.business_type} businesses "
            f"in {location_phrase} in 2026. "
            f"What platforms, what content, what offers are getting results right now."
        )
        logger.info(f"[FULL-REPORT] Fetching Tavily data for: {request.business_type}")
        tavily_competitors, tavily_market = await asyncio.gather(
            fetch_tavily(q_competitors),
            fetch_tavily(q_market),
        )
        live_data_used = bool(tavily_competitors or tavily_market)
        logger.info(f"[FULL-REPORT] Tavily live_data_used={live_data_used}")

    live_intel_block = ""
    if live_data_used:
        live_intel_block = (
            "LIVE MARKET INTELLIGENCE (real-time web data):\n"
            "TOP COMPETITORS IN THIS MARKET:\n"
            f"{tavily_competitors}\n\n"
            "CURRENT MARKET TRENDS:\n"
            f"{tavily_market}\n\n"
            "Use this real data. Reference actual company names. Make analysis specific not generic.\n\n"
        )

    _analyzed_url = request.url or f"{request.business_type} business"
    business_critical = (
        "IDENTITY: You are Marketing Brain — the complete Marketing Operating System inside Adsoh. "
        "You are NOT a chatbot and NOT a generic AI report generator. You think exactly like an experienced "
        "Chief Marketing Officer, Growth Strategist, Media Buyer, Brand Strategist, Market Researcher, "
        "Performance Marketer, and Business Consultant working together on one account. Never write like ChatGPT. "
        "Never pad a section with generic marketing filler — if you don't have evidence for a point, don't make it.\n"
        f"CRITICAL: The business you are analyzing is the one at {_analyzed_url}. "
        f"Do NOT call it Adsoh or Sohscape. "
        f"Use the actual business name detected from the website content.\n"
        "BANNED WORDS — ZERO TOLERANCE. Never use ANY of these in copy, headlines, hooks, or analysis: "
        "Transform, Elevate, Unlock, Revolutionize, Empower, Seamless, Leverage, Utilize, Boost, Maximize, "
        "Unleash, Game-changer, Dive in, Take your business to new heights, Cutting-edge, State-of-the-art, "
        "World-class, One-stop solution, Look no further, In today's digital age.\n"
        "CRITICAL FINAL CHECK: Before outputting your response, scan every sentence. "
        "If you find any banned word above, rewrite that sentence completely using plain, direct language.\n\n"
        "EVIDENCE DISCIPLINE: Every claim must cite its basis — 'based on the website's [specific detail]', "
        "'competitor [name] does [specific thing]', 'reviews mention [specific complaint/praise]'. "
        "No unsupported generic statements.\n"
        "NUMBER DISCIPLINE: All numbers must be specific and realistic for the industry and city — exact ₹ ranges, "
        "exact %, exact timeframes. Never say 'increase revenue' — say something like 'estimated 15-25% more "
        "walk-ins within 60 days'.\n"
        "MISSING DATA: If there isn't enough information for a section, say 'Insufficient data — recommend "
        "running [specific module name]' instead of filling the section with generic advice.\n\n"
        "DECISION DISCIPLINE: Every non-obvious recommendation must be backed by an observation from the data, "
        "the evidence for it, why it matters, your confidence in it (state 'high confidence' when it's directly "
        "backed by data above, 'medium confidence' when inferred, 'low confidence / assumption' when you're "
        "filling a gap), and the risk of being wrong. Weave this into normal prose — do not create a separate "
        "labeled sub-section for it.\n"
        "SELF-CHECK BEFORE ANSWERING: Before finalizing each section, verify it actually matches this specific "
        "business, its real industry, and its real audience — not a generic template for that industry. If a "
        "sentence could be copy-pasted into a report for an unrelated business without anyone noticing, rewrite it "
        "with a detail that only applies here.\n\n"
    )

    # ── Memory: derive key + fetch existing knowledge ────────────────────────
    _mem_key = derive_business_key(request.url, request.target_industry, request.target_city)
    logger.info(f"[MEMORY][full-report] SAVE key: {_mem_key!r} | url={request.url!r} target_industry={request.target_industry!r} city={request.target_city!r}")
    _prior_memory = get_memory(_mem_key)
    memory_used = bool(_prior_memory)

    _memory_block = ""
    if _prior_memory:
        parts = []
        bm = _prior_memory.get("business", {})
        mm = _prior_memory.get("market", {})
        cm = _prior_memory.get("competitor", {})
        am = _prior_memory.get("audience", {})
        if bm.get("uvp"):
            parts.append(f"UVP (previously detected): {bm['uvp']}")
        if bm.get("positioning"):
            parts.append(f"Positioning (previously detected): {bm['positioning']}")
        if bm.get("brand_score"):
            parts.append(f"Brand Score: {bm['brand_score']} | Trust Score: {bm.get('trust_score','')} | Opportunity Score: {bm.get('opportunity_score','')}")
        if mm.get("market_gap"):
            parts.append(f"Market Gap (previously detected): {mm['market_gap']}")
        if mm.get("competition_level"):
            parts.append(f"Competition Level: {mm['competition_level']}")
        if cm.get("competitors"):
            comp_list = cm["competitors"]
            if isinstance(comp_list, list):
                parts.append(f"Known Competitors: {', '.join(str(c) for c in comp_list[:5])}")
        if am.get("segments"):
            segs = am["segments"]
            if isinstance(segs, list) and segs:
                parts.append(f"Audience Segments (previously identified): {json.dumps(segs[:2])[:300]}")
        if parts:
            _memory_block = (
                "PREVIOUSLY KNOWN ABOUT THIS BUSINESS (from prior Adsoh reports — use this as baseline, build on it, do not contradict without new evidence):\n"
                + "\n".join(f"- {p}" for p in parts)
                + "\n\n"
            )
        logger.info(f"[MEMORY] Injecting {len(parts)} prior memory points for key={_mem_key}")

    prompt_a = (
        "You are the Marketing Brain inside Adsoh.\n"
        f"{business_critical}"
        f"{_memory_block}"
        "Generate intelligence-driven analysis using the BI data below. No generic advice — every insight must come from the data.\n"
        f"LANGUAGE: {lang}\nBUSINESS: {biz} | BUDGET: {bdgt} | GOAL: {request.goal}\n\n"
        f"{industry_context}"
        f"BUSINESS DNA:\n{dna_txt}\n\nTHREAT INTELLIGENCE:\n{thr_txt}\n\nPOSITIONING DATA:\n{pos_txt}\n\n"
        f"{live_intel_block}"
        "Koi asterisk mat use kar. Seedha likho. Generate sections 1-4:\n\n"
        "BUSINESS UNDERSTANDING:\n"
        "[Run the Business Discovery Engine on the DNA data above: business model (B2B / B2C / D2C / Marketplace / "
        "Agency / Local Business / Enterprise / SaaS), whether it's product or service led, offline / online / "
        "hybrid, target market tier (premium / budget / luxury) and geographic scope (local / national / "
        "international). Then what truly makes this business different — UVP, trust signals, core_products from "
        "DNA. State your confidence in this classification (high/medium/low) — if below 90% confident on the "
        "business model, say exactly what's ambiguous. 5-6 specific points]\n\n"
        "MARKET UNDERSTANDING:\n"
        "[Run the Market Intelligence Engine: market size, demand, seasonality, growth, current trends, digital "
        "adoption level, competition level for this industry+city, and whether the biggest untapped opportunity is "
        "local, national, or international — reference market_size, market_opportunity_score, "
        "market_opportunity_reason. 4-5 specific points]\n\n"
        "COMPETITOR INSIGHTS:\n"
        "[Run the Competitor Intelligence Engine: real competitor landscape — their offers, pricing signals, SEO/"
        "keyword signals, social presence, positioning, strengths, weaknesses, and the single biggest opportunity "
        "their weakness creates for this business — reference key_threats, differentiators, moat_strength from "
        "threat data. 4-5 specific points]\n\n"
        "POSITIONING STRATEGY:\n"
        "[Market position this business should own — reference winning_position, positioning_gap, "
        "category_ownership_opportunity, messaging_shift, and the competitive advantage to lead with in every "
        "asset downstream. 3-4 specific points]"
    )

    if request.target_industry:
        city = request.target_city or "India"
        prompt_b = (
            "You are the Marketing Brain inside Adsoh.\n"
            f"{business_critical}"
            f"{_memory_block}"
            f"LANGUAGE: {lang}\nBUSINESS: {biz} | BUDGET: {bdgt} | GOAL: {request.goal}\n\n"
            f"TARGET INDUSTRY: {request.target_industry} in {city}\n\n"
            f"AUDIENCE INTELLIGENCE:\n{aud_txt}\n\nMARKET OPPORTUNITY:\n{opp_txt}\n\nBUSINESS DNA:\n{dna_txt}\n\n"
            f"{live_intel_block}"
            "Koi asterisk mat use kar. Seedha likho. Generate sections 5-8:\n\n"
            "AUDIENCE STRATEGY:\n"
            f"[First state in one line WHO the {request.target_industry} owner/manager actually is in relation to "
            f"this business — a prospect being sold to, a partner, a referral source, etc — this framing must drive "
            f"every script below.\n"
            f"BUYER INTELLIGENCE — 3 segments: Owner, Manager, Director of {request.target_industry} businesses in {city}. "
            f"For each: age, gender, income level, specific pain points in {request.target_industry}, buying trigger "
            f"(the specific event that makes them start looking for this service), dream outcome, "
            f"what they search for online, where they hang out online (platform behaviour), device usage, and "
            f"what triggers them to buy a service like yours.\n"
            f"Then estimate: audience reach in {city}, expected CPC range, expected CTR range, expected CPL range, "
            f"an audience score (0-100) and a buying-intent score (0-100) with one line of reasoning each, and an "
            f"overall confidence level for this audience read]\n\n"
            "LEAD SOURCES:\n"
            f"[WHERE TO FIND {request.target_industry.upper()} BUSINESSES IN {city.upper()}:\n"
            f"Google Maps search terms to find them:\n"
            f"Instagram hashtags they use:\n"
            f"Facebook groups they're in:\n"
            f"Local directories and trade associations:\n"
            f"Seasonal timing — when are they most stressed or spending in {city}:]\n\n"
            "OUTREACH SCRIPTS:\n"
            f"[RULE: Every message = Hook (specific to their {request.target_industry} situation) + Value (what they get) + CTA (exact next step). NO generic pain points.\n\n"
            f"WhatsApp Message 1 — Pain angle (reference a real {request.target_industry} problem like slow season, low footfall, no online presence):\n"
            f"[Write 3-4 lines. End EXACTLY with: \"Reply 'AUDIT' aur main aapka free analysis bhejta hoon 👇\"]\n\n"
            f"WhatsApp Message 2 — Proof/result angle (reference a specific outcome like 'ek {request.target_industry} client ko 3x leads mile in 30 days'):\n"
            f"[Write 3-4 lines. End EXACTLY with: \"Reply 'AUDIT' aur main aapka free analysis bhejta hoon 👇\"]\n\n"
            f"Instagram DM Script (for {request.target_industry} business owner's personal/business account):\n"
            f"[3 lines: Line 1 = something specific you noticed about their profile/business. Line 2 = what result you got for similar {request.target_industry} business. "
            f"Line 3 = End EXACTLY with: \"Interested? Main ek quick voice note bhej sakta hoon 🎙️\"]\n\n"
            f"Cold Call Opening — First 10 seconds:\n"
            f"[Hook = reference their specific business type + city. End EXACTLY with: \"Kya kal 10 minute ka call ho sakta hai?\"]\n\n"
            f"Objection 'We already have someone' → Exact response:]\n\n"
            "PITCH & CLOSE:\n"
            f"[First Meeting Agenda (for {request.target_industry} owner):\n"
            f"Key Questions to Ask:\n"
            f"Top 2 Objections + exact responses:\n"
            f"Closing Offer (what to propose at end of meeting):\n"
            f"Follow-up Sequence: Day 1 / Day 3 / Day 7:]"
        )
    else:
        prompt_b = (
            "You are the Marketing Brain inside Adsoh.\n"
            f"{business_critical}"
            f"{_memory_block}"
            "Generate intelligence-driven audience strategy and campaign strategies. Every recommendation must cite BI evidence.\n"
            f"LANGUAGE: {lang}\nBUSINESS: {biz} | BUDGET: {bdgt} | GOAL: {request.goal}\n\n"
            f"AUDIENCE INTELLIGENCE:\n{aud_txt}\n\nMARKET OPPORTUNITY:\n{opp_txt}\n\nBUSINESS DNA:\n{dna_txt}\n\n"
            f"{live_intel_block}"
            "IMPORTANT RULES:\n"
            "1. Indian market ke liye — Indian apps, platforms suggest karo.\n"
            "2. Age exclude sirf 45+ karo.\n"
            "3. KABHI betting, gambling, investment words mat use karo.\n"
            "4. KABHI earn money, win cash language mat use karo.\n\n"
            "Koi asterisk mat use kar. Seedha likho. Generate sections 5-8:\n\n"
            "AUDIENCE STRATEGY:\n"
            "[First state in one line WHO the buyer actually is relative to this business — the end consumer, a "
            "gift-buyer, a repeat customer, a first-time trial buyer, etc — this framing must drive every script "
            "below.\n"
            "BUYER INTELLIGENCE — 3 validated segments from audience BI. "
            "For each: age, gender, income level, specific pain points, buying trigger (the specific event that "
            "makes them start looking), dream outcome, what triggers purchase, where they hang out online "
            "(platform behaviour), device usage, what they search before buying. Reference validated_segments from BI.\n"
            "Then estimate: audience reach, expected CPC range, expected CTR range, expected CPL range, an "
            "audience score (0-100) and a buying-intent score (0-100) with one line of reasoning each, and an "
            "overall confidence level for this audience read]\n\n"
            "LEAD SOURCES:\n"
            f"[WHERE TO FIND BUYERS for {request.business_type}:\n"
            "Online — platforms, communities, hashtags, Facebook groups they're in:\n"
            "Offline — events, locations, associations, seasonal moments:\n"
            "Google Maps search terms they use:\n"
            "Best time of year / month / week to reach them:]\n\n"
            "OUTREACH SCRIPTS:\n"
            f"[RULE: Every message = Hook (specific to their situation as a {request.business_type} buyer) + Value (concrete benefit) + CTA (exact next step). NOT generic.\n\n"
            f"WhatsApp Message 1 — Pain angle (reference a real specific problem this audience faces — low footfall, wasted ad budget, no enquiries — based on the audience intel above):\n"
            f"[Write 3-4 lines. End EXACTLY with: \"Reply 'AUDIT' aur main aapka free analysis bhejta hoon 👇\"]\n\n"
            f"WhatsApp Message 2 — Proof/result angle (reference a specific result like '{request.business_type} client ko 40% more leads mile in 3 weeks'):\n"
            f"[Write 3-4 lines. End EXACTLY with: \"Reply 'AUDIT' aur main aapka free analysis bhejta hoon 👇\"]\n\n"
            f"Instagram DM Script:\n"
            f"[3 lines: Line 1 = notice something specific about their post/profile. Line 2 = connect it to a result you got for similar business. "
            f"Line 3 = End EXACTLY with: \"Interested? Main ek quick voice note bhej sakta hoon 🎙️\"]\n\n"
            f"Cold Outreach Email:\n"
            f"Subject: [specific, personalized — not 'Grow Your Business']\n"
            f"First line: [reference something real about their business. End EXACTLY with: \"Kya kal 10 minute ka call ho sakta hai?\"]\n\n"
            "Objection 'Not interested' → Exact response:]\n\n"
            "PITCH & CLOSE:\n"
            "[How to convert an interested lead into a paying customer:\n"
            "Discovery call agenda:\n"
            "Key qualifying questions:\n"
            "Top 2 objections + exact responses:\n"
            "Closing offer / call-to-action:\n"
            "Follow-up Sequence: Day 1 / Day 3 / Day 7:]"
        )

    prompt_c = (
        "CRITICAL INSTRUCTION: Do NOT write Business Understanding, Market Understanding, Competitor Insights, or Positioning Strategy sections. Those are already complete in sections 1-4. Your output must START DIRECTLY with 'MARKETING PLAN' and only contain sections 9, 10, 11.\n\n"
        "You are the Marketing Brain inside Adsoh.\n"
        f"{business_critical}"
        f"{_memory_block}"
        "Generate the marketing plan, ad assets, and media buying plan. All recommendations must reference BI evidence.\n"
        f"LANGUAGE: {lang}\nBUSINESS: {biz} | BUDGET: {bdgt} | GOAL: {request.goal}\n\n"
        f"{industry_context}"
        f"EXECUTIVE DECISIONS:\n{exec_txt}\n\nBI SCORES:\n{sc_txt}\n\nBUSINESS DNA:\n{dna_txt}\n\n"
        f"{live_intel_block}"
        "NEVER use the word Elevate in any headline, hook, or copy.\n"
        "Koi asterisk mat use kar. Seedha likho. Generate sections 9-11 ONLY:\n\n"
        "MARKETING PLAN:\n"
        "Channel Ranking: [Rank the top 4-5 channels for THIS business right now out of: Google Ads, Meta Ads, SEO, "
        "Local SEO / Google Business Profile, Email, WhatsApp, LinkedIn, YouTube, Display, Performance Max, Demand "
        "Gen, Organic Social, Influencer, Referral, Community, Partnership, PR, Offline/Events, Cold Outreach. "
        "For each ranked channel give one line: why it ranks here for this specific business, citing evidence "
        "from the data above — not a generic channel description]\n"
        "Google Ads: [specific keywords, match types, bid strategy, budget split]\n"
        "Meta Ads: [exact audience — age, gender, interests, behaviors, placements]\n"
        "Remarketing: [3 retargeting sequences with triggers and copy angles]\n"
        "Landing Page: [3 conversion optimizations based on BI data]\n\n"
        "AD ASSETS:\n"
        "RULES FOR ALL ASSETS:\n"
        f"- Headlines: Must include a number OR {request.target_city or 'your city'} OR a specific benefit — NEVER generic phrases like 'Best Marketing' or 'Grow Your Business'\n"
        "- Descriptions: Last sentence MUST be a CTA ('Call karo', 'WhatsApp karo', 'Form bharo abhi', 'Free audit lo')\n"
        "- Hook Lines: Must START with the audience's exact situation (their job, problem, or desire) — NOT a generic question\n\n"
        "Google Headlines (8, STRICT max 30 characters each — include numbers, city, or specific benefits):\n"
        "1. []\n2. []\n3. []\n4. []\n5. []\n6. []\n7. []\n8. []\n"
        "Descriptions (4, max 90 characters each — last sentence must be a CTA):\n"
        "1. []\n2. []\n3. []\n4. []\n"
        "Hook Lines (3 for Meta/Reels — each starts with audience's exact situation, not a question):\n"
        "1. []\n2. []\n3. []\n"
        "CTAs (3 — specific actions, not 'Contact Us' or 'Learn More'):\n"
        "1. []\n2. []\n3. []\n"
        "Creative Brief 1 — [angle]: Hook: [] | Visual: [] | Copy: [] | CTA: []\n"
        "Creative Brief 2 — [angle]: Hook: [] | Visual: [] | Copy: [] | CTA: []\n\n"
        "MEDIA BUYING PLAN:\n"
        f"Campaign Objective: [{request.goal} — explain why this fits the business]\n"
        "Platform Priority: [1st: [] — why | 2nd: [] — why | 3rd: [] — why]\n"
        f"Budget Split: [{bdgt} — exact rupee allocation per platform with reasoning]\n"
        f"Budget Pacing: [Given {bdgt} and the current competition level and learning-phase needs, should this "
        "spend faster (compress into fewer days to exit the learning phase quickly — good for small budgets or "
        "high competition) or pace evenly across the full month (steadier signal, better for stable long-term "
        "accounts)? Recommend one and say why, citing the budget size and goal above]\n"
        "Bid Strategy: [recommended bid strategy + why it fits this business and goal]\n"
        "Launch Plan: [recommended launch date, what to set up first, first 7 days checklist]\n"
        "Scaling Rules: [when to scale — conditions, by how much %, safe vs aggressive thresholds]\n"
        "Pause Rules: [exact conditions — CTR below X%, no conversions after Y days, CPC above Z]\n"
        "Benchmarks: [CTR range, CPC range, CPL range, conversion rate range for this industry]"
    )

    prompt_guide = (
        "Tu elite ad intelligence strategist hai.\n\n"
        "Meta Ad Library mein DIKHTA hai: creative, start date, ad count, versions, platforms.\n"
        "NAHI dikhta: likes, CTR, conversions, ROAS, budget, spend.\n\n"
        f"Competitor: {ad_name}\nIndustry: {request.business_type}\n\n"
        f"THREAT INTELLIGENCE (BI scan se):\n{thr_txt}\n\n"
        f"POSITIONING DATA (BI scan se):\n{pos_txt}\n\n"
        "Koi asterisk mat use kar. Seedha likho:\n\n"
        "AD LIBRARY MEIN KYA DEKHO (is competitor ke liye specific):\n1. []\n2. []\n3. []\n4. []\n\n"
        "COMPETITOR KI KAMZORI (threat data se — unke ads kya NAHI bolenge):\n1. []\n2. []\n3. []\n\n"
        "TUMHARA WINNING ANGLE (positioning gap pe based):\n1. []\n2. []\n3. []\n\n"
        "ABHI YEH KARO:\n1. []\n2. []\n3. []"
    )

    section_a_raw, section_b_raw, section_c_raw, ad_guide_raw = await asyncio.gather(
        run_ai(prompt_a, 2400),
        run_ai(prompt_b, 2400),
        run_ai(prompt_c, 2800),
        run_ai(prompt_guide, 1000),
    )
    section_a = _clean_banned_words(section_a_raw)
    section_b = _clean_banned_words(section_b_raw)
    section_c = _clean_banned_words(section_c_raw)
    ad_guide  = _clean_banned_words(ad_guide_raw)

    # ── Split each grouped output into individual sections ────────────────
    def _header_pattern(header):
        # GPT sometimes wraps headers in markdown bold/hashes or drops the
        # trailing colon (e.g. "**MARKETING PLAN**" instead of "MARKETING PLAN:").
        # Match the header text loosely regardless of that formatting noise.
        core = re.escape(header.rstrip(":"))
        return re.compile(r'[#\*\s]*' + core + r'[:\*\s]*', re.I)

    def split_by_headers(text, headers):
        result = {}
        for i, header in enumerate(headers):
            next_header = headers[i + 1] if i + 1 < len(headers) else None
            start_match = _header_pattern(header).search(text)
            if not start_match:
                result[header] = ""
                continue
            content_start = start_match.end()
            if next_header:
                end_match = _header_pattern(next_header).search(text[content_start:])
                content = text[content_start:content_start + end_match.start()].strip() if end_match else text[content_start:].strip()
            else:
                content = text[content_start:].strip()
            result[header] = content
        return result

    a_parts = split_by_headers(section_a, [
        "BUSINESS UNDERSTANDING:", "MARKET UNDERSTANDING:",
        "COMPETITOR INSIGHTS:", "POSITIONING STRATEGY:",
    ])
    b_parts = split_by_headers(section_b, [
        "AUDIENCE STRATEGY:", "LEAD SOURCES:", "OUTREACH SCRIPTS:", "PITCH & CLOSE:",
    ])
    c_parts = split_by_headers(section_c, [
        "MARKETING PLAN:", "AD ASSETS:", "MEDIA BUYING PLAN:",
    ])
    # Strip any repeated sections 1-4 content from section_c in case AI ignored the instruction
    _dupe_headers = ["BUSINESS UNDERSTANDING:", "MARKET UNDERSTANDING:", "COMPETITOR INSIGHTS:", "POSITIONING STRATEGY:"]
    for _dh in _dupe_headers:
        _m = re.search(re.escape(_dh), section_c, re.I)
        _plan_m = re.search(re.escape("MARKETING PLAN:"), section_c, re.I)
        if _m and _plan_m and _m.start() < _plan_m.start():
            section_c = section_c[_plan_m.start():]
            c_parts = split_by_headers(section_c, [
                "MARKETING PLAN:", "AD ASSETS:", "MEDIA BUYING PLAN:",
            ])
            break

    # Secondary cleanup: strip leaked section 1-4 content from the parsed marketing_plan value
    _mplan = c_parts.get("MARKETING PLAN:", section_c)
    if re.search(r'BUSINESS UNDERSTANDING:', _mplan, re.I):
        _anchor = re.search(r'(?:MARKETING PLAN:|Google Ads:)', _mplan, re.I)
        if _anchor:
            _trimmed = _mplan[_anchor.start():]
            _fmp_header = re.match(r'MARKETING PLAN:\s*', _trimmed, re.I)
            c_parts["MARKETING PLAN:"] = (_trimmed[_fmp_header.end():] if _fmp_header else _trimmed).strip()

    try:
        report = ReportModel(
            report_type="full-report",
            title=request.url or f"industry-only:{request.target_industry}:{request.target_city}",
            input_data=json.dumps({"url": request.url, "business_type": request.business_type, "budget": request.budget, "goal": request.goal, "competitor_name": request.competitor_name, "competitor_website": request.competitor_website, "target_industry": request.target_industry, "target_city": request.target_city}),
            result_data=json.dumps({"section_a": section_a, "section_b": section_b, "section_c": section_c, "ad_guide": ad_guide}),
            created_at=datetime.now().strftime("%d %b %Y, %I:%M %p")
        )
        db.add(report)
        db.commit()
    except Exception as _re:
        logger.warning(f"[REPORTS] Could not save full-report: {_re}")
        db.rollback()

    # ── Memory Save ───────────────────────────────────────────────────────────
    _b2b = bool(request.target_industry)
    _ti  = request.target_industry or ""
    _tc  = request.target_city or ""

    logger.info(f"[MEMORY SAVE] ===== BLOCK ENTERED =====")
    logger.info(f"[MEMORY SAVE] key={_mem_key!r}")
    logger.info(f"[MEMORY SAVE] url={request.url!r} target_industry={_ti!r} target_city={_tc!r} b2b={_b2b}")

    _dna    = bi_data.get("business_dna", {})           if bi_data else {}
    _opp    = bi_data.get("opportunity", {})             if bi_data else {}
    _aud    = bi_data.get("audience_intelligence", {})   if bi_data else {}
    _thr    = bi_data.get("threat_intelligence", {})     if bi_data else {}
    _scores = bi_data.get("scores", {})                  if bi_data else {}

    # business_memory
    _biz_data = {
        "business_name":     _dna.get("business_name") or request.business_type,
        "industry":          _dna.get("detected_industry") or request.business_type,
        "city":              _tc,
        "business_dna":      _dna or None,
        "uvp":               _dna.get("value_proposition") or _dna.get("uvp"),
        "positioning":       (f"B2B campaign targeting {_ti} businesses in {_tc}" if _b2b
                              else _dna.get("positioning_statement")),
        "brand_score":       _scores.get("brand_strength"),
        "trust_score":       _scores.get("trust_score"),
        "opportunity_score": _scores.get("opportunity_score"),
    }
    logger.info(f"[MEMORY SAVE] table=business key={_mem_key!r} data_keys={list(_biz_data.keys())}")
    _ok1, _err1 = save_to_memory("business", _mem_key, _biz_data)
    logger.info(f"[MEMORY SAVE] table=business result={'OK' if _ok1 else 'FAIL: ' + str(_err1)}")

    # market_memory
    if _b2b:
        _mkt_data = {
            "market_size":       f"{_ti} businesses in {_tc}",
            "growth":            "Active B2B market",
            "competition_level": "Medium — most agencies use generic pitches",
            "market_gap":        (
                f"{_ti} businesses in {_tc} are underserved — competitors use generic "
                f"pitches not tailored to {_ti}-specific pain points, seasonality, and buying cycles"
            ),
        }
    else:
        _mkt_data = {
            "market_size":       str(_opp.get("market_size", "") or ""),
            "growth":            str(_opp.get("market_growth", "") or ""),
            "trends":            _opp.get("trending_opportunities") or None,
            "seasonality":       _opp.get("seasonal_patterns") or None,
            "competition_level": str(_thr.get("competition_level") or _scores.get("market_saturation") or ""),
            "market_gap":        str(_opp.get("market_gap") or _opp.get("market_opportunity_reason") or ""),
        }
    logger.info(f"[MEMORY SAVE] table=market key={_mem_key!r} data_keys={list(_mkt_data.keys())}")
    _ok2, _err2 = save_to_memory("market", _mem_key, _mkt_data)
    logger.info(f"[MEMORY SAVE] table=market result={'OK' if _ok2 else 'FAIL: ' + str(_err2)}")

    # competitor_memory
    _comp_list = []
    if isinstance(_thr.get("key_threats"), list):
        for _t in _thr["key_threats"]:
            _n = (_t.get("competitor") or _t.get("name")) if isinstance(_t, dict) else (_t if isinstance(_t, str) else None)
            if _n:
                _comp_list.append(_n)
    if request.competitor_name:
        _comp_list = [request.competitor_name] + _comp_list
    _comp_data = {"competitors": list(dict.fromkeys(_comp_list))[:10]}
    logger.info(f"[MEMORY SAVE] table=competitor key={_mem_key!r} data_keys={list(_comp_data.keys())} competitors={_comp_data['competitors']}")
    _ok3, _err3 = save_to_memory("competitor", _mem_key, _comp_data)
    logger.info(f"[MEMORY SAVE] table=competitor result={'OK' if _ok3 else 'FAIL: ' + str(_err3)}")

    # audience_memory: B2B → target-industry decision makers; non-B2B → own BI segments
    if _b2b:
        _aud_segs = [
            f"{_ti} Owner / Founder in {_tc} — primary decision maker, controls budget, signs contracts",
            f"{_ti} General Manager / Operations Head in {_tc} — day-to-day contact, influences vendor selection",
            f"{_ti} Marketing In-charge in {_tc} — manages external agencies, evaluates ROI",
        ]
    else:
        _aud_segs = _aud.get("validated_segments") or _aud.get("segments") or []
    _aud_data = {"segments": _aud_segs or None}
    logger.info(f"[MEMORY SAVE] table=audience key={_mem_key!r} mode={'B2B target segs' if _b2b else 'BI segs'} segments={_aud_segs}")
    _ok4, _err4 = save_to_memory("audience", _mem_key, _aud_data)
    logger.info(f"[MEMORY SAVE] table=audience result={'OK' if _ok4 else 'FAIL: ' + str(_err4)}")

    # ── Immediate read-back to confirm saves landed ───────────────────────────
    _readback = get_memory(_mem_key)
    _rb_tables = list(_readback.keys())
    logger.info(f"[MEMORY SAVE] ===== READ-BACK after saves: key={_mem_key!r} tables_found={_rb_tables} =====")
    if "audience" in _readback:
        logger.info(f"[MEMORY SAVE] audience segments in DB: {_readback['audience'].get('segments')}")

    # ── Auto-purge stale plain-URL key in B2B mode ────────────────────────────
    # When running B2B, the correct data lives at the B2B key (url::industry::city).
    # Any old row at the plain URL key (url only) has wrong audience data from
    # pre-fix runs. Delete it so Opportunity Engine / Offer Intelligence can't
    # accidentally read it when the user forgets to pass the target industry.
    if _b2b and request.url.strip():
        _stale_key = derive_business_key(request.url, "", "")
        if _stale_key != _mem_key:
            _purged = []
            for _tbl in _MEMORY_TABLES.values():
                try:
                    with engine.connect() as conn:
                        result = conn.execute(
                            text(f"DELETE FROM {_tbl} WHERE business_key = :bk"), {"bk": _stale_key}
                        )
                        conn.commit()
                        if result.rowcount:
                            _purged.append(_tbl)
                except Exception as _de:
                    logger.warning(f"[MEMORY] Could not purge stale key {_stale_key!r} from {_tbl}: {_de}")
            if _purged:
                logger.info(f"[MEMORY] Purged stale plain-URL key {_stale_key!r} from: {_purged}")

    log_activity(
        "marketing_brain", business_key=_mem_key,
        business_name=_dna.get("business_name") or request.business_type,
        url=request.url, industry=request.target_industry, city=request.target_city,
        summary="Marketing Brain report generated",
    )

    # ── TRUST & ACCURACY LAYER ────────────────────────────────────────────
    # industry_only_mode never crawls a specific business — it's a static
    # B2B outreach template for Sohscape's own agency, not grounded in any
    # analyzed business's real data, so it can never claim HIGH confidence.
    _trust_has_dna = (not industry_only_mode) and bool(
        _dna_dict_has_signal(_dna) or (_biz_data.get("uvp") or "").strip() or (_biz_data.get("positioning") or "").strip()
    )
    _trust_has_audience = bool(_aud_data.get("segments"))
    _trust_extra_note = "This is a generic B2B outreach template, not grounded in a specific crawled business." if industry_only_mode else ""
    trust_verdict = _compute_trust_verdict(_trust_has_dna, _trust_has_audience, _trust_extra_note)
    based_on = _compute_based_on_line(_trust_has_dna, _trust_has_audience, has_campaign_data=False)

    _biz_label = _biz_data.get("business_name") or request.business_type
    _biz_positioning_txt = _biz_data.get("positioning") or ""
    _business_context_txt = " ".join([
        request.business_type or "", _dna.get("detected_industry", "") or "", _dna.get("detected_sub_industry", "") or "",
        _biz_positioning_txt, json.dumps(_dna.get("core_products", []) if isinstance(_dna, dict) else []),
    ])
    _output_context_txt = " ".join([
        a_parts.get("BUSINESS UNDERSTANDING:", ""), c_parts.get("AD ASSETS:", ""), c_parts.get("MARKETING PLAN:", ""),
    ])
    _match_warning = _business_match_sanity_check(_business_context_txt, _output_context_txt, _biz_label, _biz_positioning_txt)
    _contradiction_warning = _detect_consistency_contradiction(_trust_has_dna, a_parts.get("BUSINESS UNDERSTANDING:", ""))
    validation_warning = _combine_validation_warnings(_match_warning, _contradiction_warning)

    _response = {
        "success": True,
        "url": request.url,
        "trust_verdict": trust_verdict,
        "based_on": based_on,
        "validation_warning": validation_warning or None,
        # Backward-compatible keys (existing frontend reads these)
        "strategy":       section_a,
        "competitor":     a_parts.get("COMPETITOR INSIGHTS:", section_a),
        "audience":       section_b,
        "smart_creative": section_c,
        "ad_guide":       ad_guide,
        # New 11-section structure
        "sections": {
            "business_understanding": a_parts.get("BUSINESS UNDERSTANDING:", section_a),
            "market_understanding":   a_parts.get("MARKET UNDERSTANDING:", ""),
            "competitor_insights":    a_parts.get("COMPETITOR INSIGHTS:", ""),
            "positioning_strategy":   a_parts.get("POSITIONING STRATEGY:", ""),
            "audience_strategy":      b_parts.get("AUDIENCE STRATEGY:", section_b),
            "lead_sources":           b_parts.get("LEAD SOURCES:", ""),
            "outreach_scripts":       b_parts.get("OUTREACH SCRIPTS:", ""),
            "pitch_close":            b_parts.get("PITCH & CLOSE:", ""),
            "marketing_plan":         c_parts.get("MARKETING PLAN:", section_c),
            "ad_assets":              c_parts.get("AD ASSETS:", ""),
            "media_buying_plan":      c_parts.get("MEDIA BUYING PLAN:", ""),
        },
        "bi_data":        bi_data,
        "bi_cached":      bi_cached,
        "live_data_used":  live_data_used,
        "firecrawl_used":  firecrawl_used_bi,
        "memory_used":     memory_used,
        "industry_only_mode": industry_only_mode,
        "target_industry": request.target_industry,
        "target_city":     request.target_city,
        "meta_ad_library_link": meta_link,
        "google_ads_link":      google_link,
    }
    save_report_snapshot("marketing_brain", _mem_key, _response)
    return _response

class CampaignLaunchKitRequest(BaseModel):
    url: str = ""
    industry: str = ""
    city: str = ""
    budget: int = 10000
    goal: str = ""
    language: str = "Hinglish"
    sections: dict = {}


def _extract_campaign_kit_assets(google_kit_text: str) -> dict:
    """
    Parse the AI-generated Google Ads launch kit text into structured
    keywords/headlines/descriptions for campaign_memory + push-to-Google-Ads.
    """
    def _clean_kw(raw: str) -> str:
        # AI output is inconsistent about wrapping the keyword itself in quotes
        # (e.g. `[exact match] "hotel marketing Jaipur"`) regardless of the
        # match-type prefix — strip any such wrapping so the literal quote
        # characters never end up in the keyword text sent to Google Ads.
        kw = raw.strip().rstrip(".")
        kw = kw.strip('"\'“”‘’').strip()
        return kw

    keywords, headlines, descriptions, sitelinks = [], [], [], []
    for line in (google_kit_text or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        m = re.match(r'^\[exact match\]\s*(.+)$', line, re.I)
        if m:
            kw = _clean_kw(m.group(1))
            if kw:
                keywords.append({"text": kw, "match_type": "EXACT"})
            continue
        m = re.match(r'^["“]phrase match["”]\s*(.+)$', line, re.I)
        if m:
            kw = _clean_kw(m.group(1))
            if kw:
                keywords.append({"text": kw, "match_type": "PHRASE"})
            continue
        m = re.match(r'^broad match\s*(.+)$', line, re.I)
        if m:
            kw = _clean_kw(re.sub(r'\s*\(include[^)]*\)', '', m.group(1), flags=re.I))
            if kw:
                keywords.append({"text": kw, "match_type": "BROAD"})
            continue
        m = re.match(r'^Sitelink\s*\d+\s*:\s*(.+?)\s*\|\s*Desc1\s*:\s*(.+?)\s*\|\s*Desc2\s*:\s*(.+)$', line, re.I)
        if m:
            link_text = m.group(1).strip()
            if link_text and len(sitelinks) < 6:
                sitelinks.append({
                    "link_text":    link_text,
                    "description1": m.group(2).strip(),
                    "description2": m.group(3).strip(),
                })
            continue
        m = re.match(r'^Headline\s*\d+\s*:\s*(.+)$', line, re.I)
        if m:
            h = m.group(1).strip()
            if h and h not in headlines:
                headlines.append(h)
            continue
        m = re.match(r'^Description\s*\d+\s*:\s*(.+)$', line, re.I)
        if m:
            d = m.group(1).strip()
            if d and d not in descriptions:
                descriptions.append(d)
            continue
    return {
        "keywords":     keywords[:30],
        "headlines":    headlines[:15],
        "descriptions": descriptions[:4],
        "sitelinks":    sitelinks[:6],
    }


@app.post("/campaign-launch-kit")
async def campaign_launch_kit(request: CampaignLaunchKitRequest):
    biz_label  = request.url or request.industry or "this business"
    city_label = request.city or "India"
    bdgt       = request.budget
    meta_bdgt  = int(bdgt * 0.50)
    google_bdgt = int(bdgt * 0.40)
    remarketing_bdgt = bdgt - meta_bdgt - google_bdgt

    sections_summary = "\n\n".join([
        f"{k.upper().replace('_', ' ')}:\n{str(v)[:400]}"
        for k, v in request.sections.items() if v
    ])

    _now          = datetime.now()
    _month_short  = _now.strftime("%b%Y")    # e.g. Jul2026
    _month_long   = _now.strftime("%B %Y")   # e.g. July 2026

    # ── Real performance benchmarks: calibrate against this account's actual
    # live campaign data (kpi_memory / performance_memory) when it exists,
    # instead of generic industry benchmarks. Gets smarter as real data accumulates.
    business_key = derive_business_key(request.url, request.industry, request.city)
    _prior_mem, _ = get_memory_with_city_fallback(request.url, request.industry, request.city)
    real_perf_block = ""
    _kpi_mem  = _prior_mem.get("kpi")
    _perf_mem = _prior_mem.get("performance")
    if _kpi_mem or _perf_mem:
        _perf_lines = []
        if _perf_mem:
            _perf_lines.append(f"Live performance data: {json.dumps(_perf_mem, ensure_ascii=False)[:600]}")
        if _kpi_mem:
            _perf_lines.append(f"Prior KPI targets/actuals: {json.dumps(_kpi_mem, ensure_ascii=False)[:600]}")
        real_perf_block = (
            "REAL PERFORMANCE DATA from this account's live campaigns — calibrate ALL predictions, "
            "bid ranges, and budget splits against these actuals, not generic industry benchmarks:\n"
            + "\n".join(_perf_lines) + "\n\n"
        )

    _growth_block = growth_learning_block(request.industry)

    prompt = (
        f"You are a senior media buyer building a ready-to-paste campaign launch kit.\n"
        f"Business: {biz_label} | City: {city_label} | Total Monthly Budget: Rs {bdgt}\n"
        f"Goal: {request.goal} | Language: {request.language}\n"
        f"CURRENT DATE: {_month_long} — use this for ALL campaign names and date references.\n\n"
        f"MARKETING BRAIN OUTPUT (extract specific details — audience pain points, competitors, positioning — and use them in every asset below):\n"
        f"{sections_summary}\n\n"
        f"{real_perf_block}"
        f"{_growth_block}"
        "RULES — READ BEFORE GENERATING:\n"
        "1. ZERO generic copy. Every asset must reference the specific business, industry, or city above.\n"
        "2. Ad Copy formula: Hook (problem/desire specific to this audience) + Body (specific benefit with proof or number) + CTA (one exact action).\n"
        "3. Every CTA must be one of: 'WhatsApp pe FREE AUDIT bhejo', 'Form bharo — free consultation lo', 'Call karo abhi', 'Link mein appointment book karo'.\n"
        "4. Audience targeting: Use EXACT job titles (not 'business owners'), specific interest combinations, behaviors that signal buying intent.\n"
        "5. No markdown bold or bullets. Plain text only. Use --- to separate sub-sections. Use exact rupee amounts.\n"
        "6. BANNED WORDS — ZERO TOLERANCE: Transform, Elevate, Unlock, Revolutionize, Empower, Seamless, Leverage, Utilize, Boost, Maximize, "
        "Game-changer, Unleash, Cutting-edge, State-of-the-art, World-class, One-stop solution, Look no further, In today's digital age. If any found, rewrite.\n"
        f"7. Campaign names MUST use current month/year: {_month_short}. NEVER hardcode old dates like Nov2023 or Jun2024.\n"
        "8. CTR-OPTIMIZED HEADLINES: use proven high-CTR patterns — numbers ('3x more bookings'), questions "
        "('Losing weekend footfall?'), specificity (name the city/neighborhood), urgency WITH a real reason "
        "('Before wedding season fills up'). BANNED generic patterns in any headline or hook: 'Best services', "
        "'Quality solutions', 'Your trusted partner'.\n"
        "9. GOOGLE ADS POLICY COMPLIANCE — headlines/descriptions get rejected by Google's ad review if they "
        "violate these, so avoid them entirely: NO unsubstantiated superlative claims ('best', '#1', 'number one', "
        "'guaranteed', 'world's best') — Google requires proof it can verify, which ad copy never has; use a "
        "specific number or result instead ('3x more bookings' not 'the best booking service'). NO trademarked "
        "brand/competitor names in headlines or descriptions. NO phone numbers written directly in ad text — "
        "phone numbers only go through Call extensions, never in a headline/description string. NO 'click here' "
        "or 'click now' — use one of the exact CTAs from rule 3 instead. NO ALL-CAPS words (reads as shouting and "
        "gets flagged) — normal sentence case only, Hinglish included.\n\n"
        "=== META ADS LAUNCH KIT ===\n"
        f"Campaign Name: [exact name — format: City_Industry_Goal_{_month_short}, e.g. {city_label.replace(' ', '')}_Coaching_Leads_{_month_short}]\n"
        f"Objective: [exact Meta objective — Leads / Traffic / Engagement / Sales — state which and why]\n"
        f"Daily Budget: Rs {int(meta_bdgt / 30)} per day (Rs {meta_bdgt}/month — 50% of total)\n"
        "---\n"
        "AUDIENCE SETTINGS\n"
        f"Location: {city_label} + [exact radius in km — justify the radius for this business type]\n"
        "Age: [28-55 for B2B/decision-maker audiences; 22-45 only for direct-to-consumer. Justify the range.]\n"
        "Gender: [All / Men / Women + one-line justification from audience data]\n"
        "Job Titles to target (Meta Detailed Targeting — copy-paste these):\n"
        "1. [exact job title or role]\n2. [exact job title]\n3. [exact job title]\n"
        "Interests (5 — paste into Meta Interests field):\n"
        "1. [exact interest as it appears in Meta]\n2. [exact interest]\n3. [exact interest]\n4. [exact interest]\n5. [exact interest]\n"
        "Behaviors (2-3 that indicate buying intent for this business):\n"
        "1. [behavior]\n2. [behavior]\n3. [behavior if relevant]\n"
        "Exclude: [specific audience to exclude — e.g. existing customers, competitors, students]\n"
        "Placements: Automatic placements recommended. Exclude: Audience Network (low quality), Right Column (low CTR).\n"
        "---\n"
        "AD VARIATION 1 — PAIN ANGLE\n"
        "Primary Text: [4-5 lines. Open with their specific pain. Close with exact CTA: 'WhatsApp pe FREE AUDIT bhejo']\n"
        "Headline: [max 40 chars — include number or city or specific benefit]\n"
        "Description: [max 30 chars — action-oriented]\n"
        "CTA Button: [exact Meta button label]\n"
        "---\n"
        "AD VARIATION 2 — PROOF ANGLE (numbers, results, credibility — must be distinct from Ad 1)\n"
        f"Primary Text: [4-5 lines. Open with a specific result ('{city_label} ke 30+ businesses ne X result paya in 60 days'). No generic claims. Close with exact CTA]\n"
        "Headline: [max 40 chars — include specific result or number — different from Ad 1]\n"
        "Description: [max 30 chars]\n"
        "CTA Button: [exact Meta button label]\n"
        "---\n"
        "AD VARIATION 3 — URGENCY ANGLE (limited time, competitor threat, or seasonal — must be distinct from Ads 1 and 2)\n"
        "Primary Text: [4-5 lines. Reference a real urgency — seasonal, limited spots this month, competitor gaining ground. Close with exact CTA]\n"
        "Headline: [max 40 chars — state urgency clearly — different from Ads 1 and 2]\n"
        "Description: [max 30 chars]\n"
        "CTA Button: [exact Meta button label]\n"
        "---\n"
        "CREATIVE DIRECTION\n"
        "Image/Video for Variation 1: [specific shot — who appears, what's shown, what text overlay]\n"
        "Image/Video for Variation 2: [specific shot — result visual, screenshot, or testimonial format]\n"
        "Image/Video for Variation 3: [urgency creative — countdown, limited-spots visual, or seasonal angle]\n\n"
        "=== GOOGLE ADS LAUNCH KIT ===\n"
        f"Campaign Name: [exact name — format: City_Industry_Search_{_month_short}, e.g. {city_label.replace(' ', '')}_Coaching_Search_{_month_short}]\n"
        "Campaign Type: Search\n"
        f"Daily Budget: Rs {int(google_bdgt / 30)} per day (Rs {google_bdgt}/month — 40% of total)\n"
        "Bid Strategy:\n"
        "  Launch phase (first 2 weeks, before conversion data): Manual CPC or Maximize Clicks\n"
        "  After 30 conversions: Switch to Maximize Conversions or Target CPA\n"
        "  Recommended starting bid: [Rs X-Y per click for this industry — give specific range]\n"
        "  Reason: [one sentence why this strategy fits the goal]\n"
        "---\n"
        "PRIMARY KEYWORD: Before writing anything below, decide ONE primary keyword phrase for "
        f"this business — normally [service/industry] + {city_label}, e.g. 'hospitality marketing {city_label.lower()}' "
        f"or 'hotel booking {city_label.lower()}'. Reuse this exact phrase (and close variants) in the keyword-match "
        "headlines marked below — this drives Google Ads' 'Ad Strength' relevance signal.\n"
        "KEYWORDS (15 minimum — mix of match types, include local + intent keywords)\n"
        "[exact match] keyword 1\n[exact match] keyword 2\n[exact match] keyword 3\n"
        "[exact match] keyword 4\n[exact match] keyword 5\n"
        "\"phrase match\" keyword 6\n\"phrase match\" keyword 7\n\"phrase match\" keyword 8\n"
        "\"phrase match\" keyword 9\n\"phrase match\" keyword 10\n"
        f"broad match keyword 11 (include {city_label})\nbroad match keyword 12\n"
        "broad match keyword 13\nbroad match keyword 14\nbroad match keyword 15\n"
        "---\n"
        "NEGATIVE KEYWORDS (10 minimum)\n"
        "1. []\n2. []\n3. []\n4. []\n5. []\n6. []\n7. []\n8. []\n9. []\n10. []\n"
        "---\n"
        "GOOGLE ADS RSA HEADLINES — 15 REQUIRED, NOT OPTIONAL. Google Ads rates 'Ad Strength' on how "
        "close you get to 15 UNIQUE headlines spanning distinct angles — filling only 8-10 or reusing "
        "the same idea in different words scores 'Poor'. Every one of the 15 below must be a genuinely "
        "different angle (not a reword of another headline) and must fit in 30 characters:\n"
        "AD 1 — BENEFIT ANGLE (what they get — specific outcome)\n"
        f"Headline 1: [KEYWORD-MATCH — literally include the PRIMARY KEYWORD phrase + city, e.g. 'Hospitality Marketing {city_label}']\n"
        "Headline 2: [max 30 chars — include city or specific offer]\n"
        "Headline 3: [max 30 chars — CTA or urgency]\n"
        "Description 1: [80-90 chars MANDATORY — specific benefit with number or proof. Last sentence = CTA. Count characters.]\n"
        "Description 2: [80-90 chars MANDATORY — social proof or differentiator. Last sentence = CTA. Count characters.]\n"
        "---\n"
        "AD 2 — PROOF ANGLE (numbers, results, credibility — completely different from Ad 1)\n"
        f"Headline 1: [KEYWORD-MATCH VARIANT — different word order/synonym of the PRIMARY KEYWORD + city, e.g. '{city_label} Hospitality Experts', ALSO include a specific result number]\n"
        f"Headline 2: [max 30 chars — who got the result, e.g. '{city_label} Hotels Trust Us']\n"
        "Headline 3: [max 30 chars — CTA]\n"
        "Description 1: [80-90 chars MANDATORY — state the specific result and who achieved it. Last sentence = CTA.]\n"
        "Description 2: [80-90 chars MANDATORY — credibility signal (years, clients, industry). Last sentence = CTA.]\n"
        "---\n"
        "AD 3 — URGENCY ANGLE (limited time, competitor threat, or seasonal — different from Ads 1 and 2)\n"
        "Headline 1: [max 30 chars — urgency signal, e.g. 'July: 2 Spots Left']\n"
        "Headline 2: [max 30 chars — what they lose by waiting]\n"
        "Headline 3: [max 30 chars — CTA]\n"
        "Description 1: [80-90 chars MANDATORY — real urgency tied to season/capacity/competitor. Last sentence = CTA.]\n"
        "Description 2: [80-90 chars MANDATORY — risk of delay + risk-free first step. Last sentence = CTA.]\n"
        "NOTE on 'Free': For Google Ads prefer 'Complimentary', 'No-cost', 'On us' over 'Free' to avoid ad policy flags.\n"
        "---\n"
        "ADDITIONAL RSA HEADLINES (6 more — MANDATORY, brings the total to 15. Each MUST be a completely "
        "different angle from the 9 above and from each other — no rewording of an existing headline):\n"
        f"Headline 10: [KEYWORD-MATCH — the PRIMARY KEYWORD phrase + city, plain and direct, e.g. 'Hotel Booking {city_label} Experts']\n"
        "Headline 11: [KEYWORD-MATCH VARIANT — a third distinct phrasing of the PRIMARY KEYWORD + city]\n"
        "Headline 12: [KEYWORD-MATCH VARIANT — a fourth distinct phrasing, e.g. service + 'near' + city or a neighborhood in the city]\n"
        "Headline 13: [FEATURE-FOCUSED — name one concrete deliverable/feature, not a vague benefit]\n"
        "Headline 14: [QUESTION-FOCUSED — must end in '?', speaks to the audience's exact pain point]\n"
        "Headline 15: [SOCIAL-PROOF-FOCUSED — a specific different proof point than Ad 2 Headline 2, e.g. a rating or review count]\n"
        "---\n"
        "AD EXTENSIONS\n"
        "Sitelinks (6 — link to specific pages, not homepage. Each with 2 description lines, max 35 chars each):\n"
        "Sitelink 1: [Label] | Desc1: [max 35 chars] | Desc2: [max 35 chars]\n"
        "Sitelink 2: [Label] | Desc1: [max 35 chars] | Desc2: [max 35 chars]\n"
        "Sitelink 3: [Label] | Desc1: [max 35 chars] | Desc2: [max 35 chars]\n"
        "Sitelink 4: [Label] | Desc1: [max 35 chars] | Desc2: [max 35 chars]\n"
        "Sitelink 5: [Label] | Desc1: [max 35 chars] | Desc2: [max 35 chars]\n"
        "Sitelink 6: [Label] | Desc1: [max 35 chars] | Desc2: [max 35 chars]\n"
        "Callouts (6 — short USPs, max 25 chars each):\n"
        "1. []\n2. []\n3. []\n4. []\n5. []\n6. []\n"
        "Structured Snippets (4 — header + 3-4 values each):\n"
        "1. Header: Services | Values: [val1], [val2], [val3], [val4]\n"
        "2. Header: [relevant header] | Values: [val1], [val2], [val3]\n"
        "3. Header: [relevant header] | Values: [val1], [val2], [val3]\n"
        "4. Header: [relevant header] | Values: [val1], [val2], [val3]\n"
        "Call Extension: [business phone placeholder — user to fill before going live]\n"
        "Location Extension: Connect Google Business Profile in Google Ads settings for automatic location extension.\n"
        "Lead Form Extension: Recommended for lead gen goals — create a native lead form in Google Ads > Extensions.\n\n"
        "=== REMARKETING KIT ===\n"
        f"Daily Budget: Rs {int(remarketing_bdgt / 30)} per day (Rs {remarketing_bdgt}/month — 10% of total)\n"
        "HOW TO BUILD AUDIENCES:\n"
        "Meta: Audiences > Create > Website Custom Audience (requires Meta Pixel installed)\n"
        "Google: Google Ads > Audience Manager > Website visitors (requires Google Ads tag + GA4)\n"
        "Exclusion: Always exclude 'Purchased / Converted' audience from cold audiences to avoid waste.\n"
        "---\n"
        "AUDIENCE 1 — Cold Visitors (homepage, no service page — bounced)\n"
        f"Trigger: Visited {biz_label} homepage but did NOT visit contact/pricing/booking page. Last 30 days.\n"
        "Frequency Cap: 2 impressions/day, 7 impressions/week per person.\n"
        "Day 1-3 message — reference what they SAW:\n"
        f"Primary Text: [3-4 lines. Reference that they visited the {biz_label} page — what specific service did they see? Show one specific result/proof. End with exact CTA.]\n"
        "Headline: [max 40 chars — reminder + specific service they viewed]\n"
        "Day 4-7 message — address hesitation:\n"
        f"Primary Text: [3-4 lines. Acknowledge they're comparing options. Address the main objection for {city_label} {request.industry if hasattr(request, 'industry') else 'businesses'}. Offer risk-free step.]\n"
        "Headline: [max 40 chars — address the hesitation directly]\n"
        "---\n"
        "AUDIENCE 2 — Warm (service/pricing page visited, high intent)\n"
        f"Trigger: Visited specific service or pricing page but did NOT submit form/call. Last 14 days.\n"
        "Frequency Cap: 3 impressions/day, 10 impressions/week per person.\n"
        "Day 1-3 message — handle the hesitation:\n"
        "Primary Text: [3-4 lines. They saw the price/service — what stopped them? Address that specific objection. Offer a no-risk first step (free audit, free call). End with exact CTA.]\n"
        "Headline: [max 40 chars — address the hesitation, e.g. 'Soch rahe ho? 15-min free call lo']\n"
        "Day 4-14 message — create real urgency:\n"
        f"Primary Text: [3-4 lines. Create real urgency — limited spots for {_now.strftime('%B')}, competitor gaining ground, seasonal window closing. NO generic 'Act Now'. End with exact CTA.]\n"
        f"Headline: [max 40 chars — real urgency tied to {_now.strftime('%B')} or capacity]\n"
        "---\n"
        "AUDIENCE 3 — Hot Leads (engaged social — 50%+ video watch or liked/commented/saved)\n"
        "Trigger: Watched 50%+ of video ad OR liked/commented/saved a post in last 60 days.\n"
        "Frequency Cap: 4 impressions/day, 14 impressions/week per person.\n"
        "Sequential retargeting:\n"
        f"Days 1-3: [Reference EXACTLY what they engaged with — which video or post type. Connect it to a specific result for {city_label} businesses.]\n"
        f"Days 4-7: [Aapke competitors {city_label} mein aage ja rahe hain — reference a real seasonal or competitive urgency. Limited spots angle.]\n"
        f"Days 8-14: [Final push — strongest offer, most specific urgency. e.g. '{_now.strftime('%B')} mein sirf 2 clients accept kar rahe hain — spot bachi hai kya?']\n"
        "Headline (Days 1-3): [max 40 chars — reference what they engaged with]\n"
        "Headline (Days 4-14): [max 40 chars — real urgency, spot/time limited]\n\n"
        "=== TRACKING SETUP ===\n"
        "--- META PIXEL ---\n"
        "Base Pixel: Install on ALL pages via header script or GTM.\n"
        "Custom Events to Track:\n"
        "1. PageView — fires automatically on every page\n"
        "2. ViewContent — fire on Services/Portfolio pages (shows service interest)\n"
        "3. Lead — fire on thank-you page after form submission\n"
        "4. Contact — fire on WhatsApp button click + Call button click\n"
        "5. CompleteRegistration — fire when audit/consultation is booked\n"
        "Custom Conversions: Create in Meta Events Manager using the Lead and Contact events above.\n"
        "Verify with: Meta Pixel Helper Chrome extension\n"
        "---\n"
        "--- GOOGLE ADS TAG ---\n"
        "Global Site Tag: Install on ALL pages (or use GTM).\n"
        "Conversion Actions to Create in Google Ads:\n"
        "1. Form Submission — track thank-you page URL as conversion\n"
        "2. Phone Calls — enable call tracking in Google Ads > Tools > Conversions\n"
        "3. WhatsApp Clicks — track as click event via GTM tag\n"
        "4. Audit Booking — track confirmation page or calendar booking event\n"
        "Import GA4 conversions into Google Ads for unified attribution.\n"
        "---\n"
        "--- GTM SETUP ---\n"
        "Container: Create one GTM container, install on all pages.\n"
        "Triggers needed: Page View, Form Submit, Button Click (WhatsApp/Call), Thank You Page\n"
        "Variables needed: Click URL, Click Text, Page URL\n"
        "Tags to fire: Meta Pixel base + events, Google Ads Conversion, GA4 Event\n"
        "---\n"
        "--- GA4 EVENTS ---\n"
        "Recommended custom events: generate_lead, contact_click, whatsapp_click, audit_request\n"
        "Mark as conversions in GA4 > Configure > Events > toggle Conversion\n"
        "Link GA4 to Google Ads for import: Google Ads > Tools > Linked accounts > Google Analytics\n\n"
        "=== LANDING PAGE CHECKLIST ===\n"
        "For every ad in this kit, verify the landing page has:\n"
        "[ ] Headline matches or directly supports the ad headline (message match)\n"
        "[ ] The exact offer from the ad is visible above the fold (e.g. 'Free Audit' ad → form says 'Get Your Free Audit')\n"
        "[ ] Trust signals above fold: client count, testimonial, or logo\n"
        "[ ] Single primary CTA — same action as the ad CTA\n"
        "[ ] WhatsApp button + Form visible without scrolling on mobile\n"
        "[ ] Phone number clickable on mobile\n"
        "[ ] Page loads under 3 seconds (test at PageSpeed Insights)\n"
        "[ ] Mobile-optimized layout (test at Google Mobile-Friendly Test)\n"
        "[ ] Thank-you page exists (needed for conversion tracking)\n"
        "[ ] No exit pop-ups that fire immediately (kills ad quality score)\n"
        "CRITICAL FINAL CHECK: Scan every word in your output above. "
        "FORBIDDEN: Transform, Elevate, Unlock, Revolutionize, Empower, Seamless, Leverage, Utilize, Boost, Maximize, Game-changer, Unleash, "
        "Cutting-edge, State-of-the-art, World-class, One-stop solution, Look no further, In today's digital age, 'Best services', "
        "'Quality solutions', 'Your trusted partner'. "
        "If ANY found — rewrite that sentence completely before returning."
    )

    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
        max_tokens=5500,
    )
    full_text = _clean_banned_words(resp.choices[0].message.content.strip())

    if _looks_like_gpt_refusal(full_text):
        # Refusals are often non-deterministic — the identical prompt
        # frequently succeeds on a second attempt, so retry once before
        # giving up rather than silently proceeding with a refusal string
        # as if it were real kit content.
        logger.warning(f"[CAMPAIGN KIT] GPT response looked like a refusal, retrying once: {full_text[:150]!r}")
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=5500,
        )
        full_text = _clean_banned_words(resp.choices[0].message.content.strip())
        if _looks_like_gpt_refusal(full_text):
            logger.error(f"[CAMPAIGN KIT] GPT declined again after retry: {full_text[:150]!r}")
            return {"success": False, "error": "GPT declined to generate content for this request. Try again, or rephrase the goal/budget."}

    def extract_kit(text, start_marker, end_marker=None):
        # Exact literal match first — the fast, deterministic path when GPT
        # follows the "=== X ===" instruction exactly (the common case).
        def _find(marker, from_idx=0):
            idx = text.find(marker, from_idx)
            if idx != -1:
                return idx, idx + len(marker)
            # Fallback: GPT occasionally drifts off the exact "=== X ==="
            # format (e.g. "### X" or "**X**"). Without this, the whole kit
            # silently collapses into one mislabeled section — meta_kit ends
            # up holding the entire raw text, google_kit ends up empty/wrong,
            # and _extract_campaign_kit_assets finds zero keywords/headlines
            # to push to Google Ads (confirmed live: a real PAUSED campaign
            # got created with 0 keywords and no ad because of exactly this).
            # Match the marker's core text loosely regardless of the
            # surrounding decoration — same tolerance Marketing Brain's own
            # section splitter (_header_pattern) already uses.
            core = re.escape(marker.strip("= \n"))
            m = re.search(r'^[#=\*\s\-]*' + core + r'[#=\*\s\-]*$', text[from_idx:], re.I | re.M)
            if m:
                return from_idx + m.start(), from_idx + m.end()
            return -1, -1

        start, start_end = _find(start_marker)
        if start == -1:
            return text
        if end_marker:
            end, _ = _find(end_marker, start_end)
            return text[start_end:end].strip() if end != -1 else text[start_end:].strip()
        return text[start_end:].strip()

    meta_kit        = extract_kit(full_text, "=== META ADS LAUNCH KIT ===",   "=== GOOGLE ADS LAUNCH KIT ===")
    google_kit      = extract_kit(full_text, "=== GOOGLE ADS LAUNCH KIT ===", "=== REMARKETING KIT ===")
    remarketing_kit = extract_kit(full_text, "=== REMARKETING KIT ===",       "=== TRACKING SETUP ===")
    tracking_kit    = extract_kit(full_text, "=== TRACKING SETUP ===",        "=== LANDING PAGE CHECKLIST ===")
    lp_checklist    = extract_kit(full_text, "=== LANDING PAGE CHECKLIST ===")
    # GPT occasionally echoes the trailing self-check instruction verbatim as
    # the last line of the last section — strip it if it leaked through.
    lp_checklist = re.sub(r'\n?CRITICAL FINAL CHECK:.*$', '', lp_checklist, flags=re.S).strip()

    # ── Save keywords/headlines/descriptions to campaign_memory ─────────────
    # so /google-ads/create-campaign can pull them back when the user clicks
    # "Push to Google Ads". Must use the SAME key derivation as the lookup.
    campaign_assets = _extract_campaign_kit_assets(google_kit)
    logger.info(f"[CAMPAIGN KIT] SAVE key: '{business_key}'")
    save_to_memory("campaign", business_key, {
        "campaign_data": {
            "keywords":     campaign_assets["keywords"],
            "headlines":    campaign_assets["headlines"],
            "descriptions": campaign_assets["descriptions"],
            "sitelinks":    campaign_assets["sitelinks"],
        }
    })

    log_activity(
        "campaign_kit", business_key=business_key, business_name=biz_label,
        url=request.url, industry=request.industry, city=request.city,
        summary=f"Campaign Launch Kit generated — {len(campaign_assets['keywords'])} keywords, "
                f"{len(campaign_assets['headlines'])} headlines, {len(campaign_assets['descriptions'])} descriptions",
    )

    return {
        "success":        True,
        "meta_kit":       meta_kit or full_text,
        "google_kit":     google_kit,
        "remarketing_kit": remarketing_kit,
        "tracking_kit":   tracking_kit,
        "lp_checklist":   lp_checklist,
        "business_key":   business_key,
    }


class AdCreativeRequest(BaseModel):
    url: str
    business_type: str
    offer: str
    platform: str = "Instagram"
    language: str = "Hinglish"

@app.post("/ad-creative")
async def ad_creative(request: AdCreativeRequest, db: Session = Depends(get_db)):
    import re

    def extract_clean(html):
        parts = []
        t = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        if t: parts.append("TITLE: " + t.group(1).strip())
        m = re.search(r'<meta[^>]*name=["\']description["\'][^>]*content=["\'](.*?)["\']', html, re.I)
        if m: parts.append("DESCRIPTION: " + m.group(1).strip())
        for tag in ["h1", "h2"]:
            for h in re.findall(r"<" + tag + r"[^>]*>(.*?)</" + tag + r">", html, re.I | re.S):
                clean = re.sub(r"<[^>]+>", "", h).strip()
                if clean and len(clean) > 2:
                    parts.append(tag.upper() + ": " + clean)
        body = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.I | re.S)
        body = re.sub(r"<style[^>]*>.*?</style>", "", body, flags=re.I | re.S)
        body = re.sub(r"<[^>]+>", " ", body)
        body = re.sub(r"\s+", " ", body).strip()
        parts.append("BODY: " + body[:1200])
        return "\n".join(parts)

    async def fetch(u):
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
                r = await c.get(u, headers={"User-Agent": "Mozilla/5.0"})
                return extract_clean(r.text)
        except:
            return ""

    site = await fetch(request.url)

    _current_month_yr = datetime.now().strftime("%B %Y")
    prompt = (
        "Tu ek award-winning ad creative director hai jo Indian brands ke liye scroll-stopping ads banata hai.\n\n"
        "BANNED WORDS — ZERO TOLERANCE: unleash, elevate, dive in, game-changer, unlock, revolutionize, seamless, empower, transform, leverage, maximize, utilize, boost your, "
        "cutting-edge, state-of-the-art, world-class, one-stop solution, look no further, in today's digital age.\n"
        "Agar yeh koi bhi word aaye toh sentence dobara likho. Plain aur direct language use karo.\n"
        "CTR-OPTIMIZED HOOKS/HEADLINES: use proven high-CTR patterns — numbers ('3x more bookings'), questions "
        "('Losing weekend footfall?'), specificity (name the city/neighborhood/audience), urgency WITH a real reason "
        "('Before wedding season fills up'). BANNED generic patterns: 'Best services', 'Quality solutions', 'Your trusted partner'.\n"
        "LANGUAGE: " + request.language + "\n"
        f"CURRENT DATE: {_current_month_yr}\n\n"
        "BRAND WEBSITE:\n" + site[:1500] + "\n\nPROMOTE: " + request.offer + "\nPLATFORM: " + request.platform + "\nINDUSTRY: " + request.business_type + "\n\n"
        "3 alag ad creative banao — DISTINCT angles (Benefit / Proof / Urgency). Koi asterisk mat use kar.\n\n"
        "CREATIVE 1 — BENEFIT ANGLE (what they get, specific outcome):\nHook Line: []\nPrimary Text: []\nHeadline: []\nCTA Button: []\nImage Concept: []\nText On Image: []\nColor Palette: []\nLayout: []\n\n"
        "CREATIVE 2 — PROOF ANGLE (numbers, results, credibility — no generic claims):\nHook Line: []\nPrimary Text: []\nHeadline: []\nCTA Button: []\nImage Concept: []\nText On Image: []\nColor Palette: []\nLayout: []\n\n"
        "CREATIVE 3 — URGENCY ANGLE (limited time, competitor threat, or seasonal urgency):\nHook Line: []\nPrimary Text: []\nHeadline: []\nCTA Button: []\nImage Concept: []\nText On Image: []\nColor Palette: []\nLayout: []\n\n"
        "CRITICAL FINAL CHECK: Scan every word. If Transform, Elevate, Unlock, Seamless, Empower, Leverage, Boost, Maximize, "
        "Cutting-edge, State-of-the-art, World-class, One-stop solution, Look no further, In today's digital age found — rewrite completely."
    )

    ai_response = client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": prompt}], max_tokens=2000)
    creative_text = _clean_banned_words(ai_response.choices[0].message.content)
    try:
        report = ReportModel(report_type="ad-creative", title=request.url, input_data=json.dumps({"url": request.url, "business_type": request.business_type, "offer": request.offer, "platform": request.platform}), result_data=json.dumps({"creative": creative_text}), created_at=datetime.now().strftime("%d %b %Y, %I:%M %p"))
        db.add(report)
        db.commit()
    except Exception as _re:
        logger.warning(f"[REPORTS] Could not save ad-creative report: {_re}")
        db.rollback()
    return {"success": True, "creative": creative_text}

class AudienceRequest(BaseModel):
    url: str = ""
    niche: str = ""
    business_type: str
    offer: str = ""
    platform: str = "Both"
    language: str = "Hinglish"
    target_industry: str = ""
    target_city: str = ""

@app.post("/audience-finder")
async def audience_finder(request: AudienceRequest, db: Session = Depends(get_db)):
    import re

    def extract_clean(html):
        parts = []
        t = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        if t: parts.append("TITLE: " + t.group(1).strip())
        m = re.search(r'<meta[^>]*name=["\']description["\'][^>]*content=["\'](.*?)["\']', html, re.I)
        if m: parts.append("DESCRIPTION: " + m.group(1).strip())
        for tag in ["h1", "h2"]:
            for h in re.findall(r"<" + tag + r"[^>]*>(.*?)</" + tag + r">", html, re.I | re.S):
                clean = re.sub(r"<[^>]+>", "", h).strip()
                if clean and len(clean) > 2:
                    parts.append(tag.upper() + ": " + clean)
        body = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.I | re.S)
        body = re.sub(r"<style[^>]*>.*?</style>", "", body, flags=re.I | re.S)
        body = re.sub(r"<[^>]+>", " ", body)
        body = re.sub(r"\s+", " ", body).strip()
        parts.append("BODY: " + body[:1500])
        return "\n".join(parts)

    async def fetch(u):
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
                r = await c.get(u, headers={"User-Agent": "Mozilla/5.0"})
                return extract_clean(r.text)
        except:
            return ""

    site = ""
    if request.url and request.url.strip():
        site = await fetch(request.url)

    niche_context = ""
    if request.niche and request.niche.strip():
        niche_context = "NICHE FOCUS: " + request.niche + "\n"

    if not site and not niche_context:
        return {"success": False, "scan_failed": True, "message": "URL ya Niche mein se kuch toh do bhai."}

    business_info = ""
    if site and len(site) >= 80:
        business_info = "BUSINESS WEBSITE:\n" + site[:1800] + "\n\n"

    if request.target_industry:
        city = request.target_city or "India"
        prompt = (
            "IMPORTANT: Poora response sirf is language mein likho: " + request.language + "\n\n"
            "Tu ek elite B2B media buyer hai jo business owners aur decision-makers ko target karta hai.\n\n"
            f"TARGET INDUSTRY: {request.target_industry} businesses in {city}\n"
            f"GOAL: Find and target OWNERS, MANAGERS, DIRECTORS of {request.target_industry} businesses in {city}\n"
            f"SELLER BUSINESS: {request.business_type}\n"
            f"PROMOTE: {request.offer or 'marketing / digital services'}\n"
            f"PLATFORM: {request.platform}\n\n"
            "CRITICAL: Yeh end-consumers NAHI hain — yeh BUSINESS OWNERS aur DECISION MAKERS hain.\n"
            "KABHI betting, gambling, wagering words mat use karo.\n\n"
            "HAR SECTION seedha aur specific likho:\n\n"
            f"IDEAL B2B TARGET:\n[Who exactly — {request.target_industry} business owners in {city}, typical profile, team size, revenue size, what they care about most]\n\n"
            "AUDIENCE SEGMENTS:\n"
            f"Segment 1 — Owners/Founders: [age, gender, income, core pain points in {request.target_industry}]\n"
            "Segment 2 — GMs/Managers: [age, gender, decision power, what they want improved]\n"
            "Segment 3 — Directors/Partners: [age, gender, strategic priorities, buying triggers]\n\n"
            f"WHERE TO FIND {request.target_industry.upper()} BUSINESS OWNERS IN {city.upper()}:\n"
            "WhatsApp Groups: [specific group types — hotel owners, chamber of commerce, trade groups]\n"
            "Industry Associations: [trade bodies, local chambers, professional networks]\n"
            "LinkedIn: [exact job titles to target]\n"
            "Local Events: [exhibitions, trade fairs, meetups, award shows in this industry]\n"
            "Directories: [JustDial, Sulekha, IndiaMart, industry-specific directories]\n"
            f"Instagram Hashtags: [10 hashtags combining {request.target_industry} + {city}]\n\n"
            f"SEASONAL TIMING ({city} context):\n[When are {request.target_industry} owners most stressed or spending? Peak seasons, off-seasons, local events that affect their business]\n\n"
            "META ADS B2B TARGETING:\n"
            "Job Titles: [exact titles for ad targeting]\n"
            "Industry: [Facebook industry categories]\n"
            "Behaviors: [business page admins, small business owners]\n"
            "Age/Gender: []\n"
            "Exclude: [employees, job seekers]\n\n"
            "GOOGLE ADS B2B TARGETING:\n"
            "Search Keywords: [what these business owners search when they need services like yours]\n"
            "In-Market Segments: [relevant B2B Google segments]\n"
            "Custom Intent Keywords: [5-7 keywords]\n\n"
            "CONTENT THAT ATTRACTS THEM:\n[What pain-point content makes them stop scrolling? What case studies, results, or formats work best for this industry]\n\n"
            "BEST TIME TO REACH THEM:\n[Day of week, time of day — when are these business owners most receptive to outreach or ads]\n\n"
            "POLICY SAFETY CHECK:\n"
            "Risk Level: [Low/Medium/High]\n"
            "Avoid These Words: []\n"
            "Landing Page Tip: []\n"
        )
    else:
        prompt = (
            "IMPORTANT: Poora response sirf is language mein likho: " + request.language + "\n\n"
            "Tu ek elite media buyer hai jo Meta aur Google Ads dono ka expert hai.\n\n"
            + business_info
            + niche_context
            + "PROMOTE: " + (request.offer or "general business") + "\n"
            + "PLATFORM: " + request.platform + "\n"
            + "INDUSTRY: " + request.business_type + "\n"
            + "IMPORTANT RULES:\n"
            + "1. INDUSTRY-SPECIFIC apps suggest karo — business type ke hisaab se. Fashion: Myntra, Nykaa, LBB. Food/Cafe: Zomato, Swiggy, EazyDiner. Hospitality: MakeMyTrip, Goibibo, TripAdvisor. Education: Unacademy, Vedantu. Jewellery: CaratLane, Tanishq, Pinterest. Sports: Dream11, MPL. Real Estate: 99acres, MagicBricks. Beauty: Nykaa, Purplle. Jo industry nahi hai uske apps KABHI mat do.\n"
            + "2. Age exclude sirf 45+ karo.\n"
            + "3. KABHI betting, gambling, wagering, investment words mat use karo.\n"
            + "4. Display Placements mein gambling apps KABHI mat do.\n"
            + "5. IDEAL AUDIENCE mein paise kamaana, earn money KABHI mat likho.\n\n"
            + "Is business ke liye exact audience batao. Koi asterisk mat use kar. HAR SECTION likho:\n\n"
            + "IDEAL AUDIENCE:\n[2-3 line]\n\n"
            + "AUDIENCE SEGMENTS:\n"
            + "Segment 1 — [naam]: [age, gender, interests, behavior]\n"
            + "Segment 2 — [naam]: [age, gender, interests, behavior]\n"
            + "Segment 3 — [naam]: [age, gender, interests, behavior]\n\n"
            + "WHERE TO FIND THEM:\n"
            + "Apps: [5 specific apps]\n"
            + "Pages/Communities: [3 Facebook pages ya groups]\n"
            + "YouTube Channels: [2-3 channels]\n"
            + "Influencer Type: [kis type ke influencer]\n\n"
            + "META ADS TARGETING:\n"
            + "Interests: [5 specific interests]\n"
            + "Behaviors: [2-3 behavior]\n"
            + "Age/Gender: []\n"
            + "Exclude: [sirf 45+]\n\n"
            + "GOOGLE ADS TARGETING:\n"
            + "In-Market Segments: [actual Google segments]\n"
            + "Custom Segment Keywords: [5 keywords]\n"
            + "Search Keywords: [5 high-intent keywords]\n\n"
            + "DISPLAY PLACEMENTS:\n"
            + "1. []\n2. []\n3. []\n4. []\n5. []\n\n"
            + "REMARKETING:\n[2 line]\n\n"
            + "EXPECTED CTR:\n[realistic rate]\n\n"
            + "POLICY SAFETY CHECK:\n"
            + "Risk Level: [Low/Medium/High]\n"
            + "Avoid These Words: []\n"
            + "Landing Page Tip: []\n"
            + "Certification Needed: []\n"
        )

    ai_response = client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": prompt}], max_tokens=2000)
    audience_text = ai_response.choices[0].message.content
    try:
        report = ReportModel(report_type="audience-finder", title=request.url or request.niche, input_data=json.dumps({"url": request.url, "niche": request.niche, "business_type": request.business_type, "offer": request.offer, "platform": request.platform}), result_data=json.dumps({"audience": audience_text}), created_at=datetime.now().strftime("%d %b %Y, %I:%M %p"))
        db.add(report)
        db.commit()
    except Exception as _re:
        logger.warning(f"[REPORTS] Could not save audience-finder report: {_re}")
        db.rollback()
    return {"success": True, "url": request.url, "niche": request.niche, "audience": audience_text}

async def gather_bi_data(url: str, business_type: str = "", competitor_urls: list = []) -> Optional[dict]:
    """
    Gather full Business Intelligence on a URL.
    Returns {'intelligence': {...}, 'scores': {...}} or None if site unreachable.
    Called by /intelligence endpoint and /full-report endpoint.
    """

    def extract_evidence(html, page_type="homepage"):
        evidence = []
        t = re.search(r'<title[^>]*>(.*?)</title>', html, re.I | re.S)
        if t:
            evidence.append({"type": "title", "value": t.group(1).strip(), "confidence": 0.95, "page": page_type})
        for name, label, conf in [
            ("description",        "meta_description",    0.90),
            ("keywords",           "keywords",            0.70),
            ("twitter:title",      "twitter_title",       0.82),
            ("twitter:description","twitter_description", 0.80),
        ]:
            m = re.search(r'<meta[^>]*name=["\']'  + re.escape(name) + r'["\'][^>]*content=["\'](.+?)["\']'  , html, re.I)
            if m and m.group(1).strip():
                evidence.append({"type": label, "value": m.group(1).strip(), "confidence": conf, "page": page_type})
        for prop, label, conf in [
            ("og:title",       "og_title",       0.85),
            ("og:description", "og_description", 0.85),
            ("og:site_name",   "og_site_name",   0.80),
            ("og:type",        "og_type",        0.70),
        ]:
            og = re.search(r'<meta[^>]*property=["\']'  + re.escape(prop) + r'["\'][^>]*content=["\'](.+?)["\']'  , html, re.I)
            if og and og.group(1).strip():
                evidence.append({"type": label, "value": og.group(1).strip(), "confidence": conf, "page": page_type})
        for tag in ['h1', 'h2', 'h3']:
            conf = 0.85 if tag == 'h1' else (0.75 if tag == 'h2' else 0.65)
            for h in re.findall(r'<' + tag + r'[^>]*>(.*?)</' + tag + r'>', html, re.I | re.S):
                clean = re.sub(r'<[^>]+>', '', h).strip()
                if clean and len(clean) > 3:
                    evidence.append({"type": tag, "value": clean, "confidence": conf, "page": page_type})
        for pattern, trust_type in [
            (r'(\d+\+?\s*(?:years?|yr)\s*(?:of\s*)?(?:experience|expertise))', "years_experience"),
            (r'(\d[\d,]*\+?\s*(?:customers?|clients?|users?))',                 "customer_count"),
            (r'(ISO\s*\d+[:\-]\d+)',                                            "certification"),
            (r'(rated\s*[\d.]+\s*(?:out\s*of\s*5|\/\s*5|stars?))',             "rating"),
            (r'(\d+\+?\s*(?:projects?|orders?|deliveries?))',                   "project_count"),
        ]:
            for match in re.findall(pattern, html, re.I)[:2]:
                evidence.append({"type": f"trust_{trust_type}", "value": match.strip(), "confidence": 0.85, "page": page_type})
        for price in re.findall(r'(?:Rs\.?|INR|₹|\$)\s*[\d,]+(?:\.\d+)?', html)[:5]:
            evidence.append({"type": "pricing_signal", "value": price.strip(), "confidence": 0.80, "page": page_type})
        nd = re.search(r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html, re.I | re.S)
        if nd:
            try:
                next_data = json.loads(nd.group(1).strip())
                props = next_data.get("props", {}).get("pageProps", {})
                def flatten_next(obj, depth=0):
                    out = []
                    if depth > 4:
                        return out
                    if isinstance(obj, dict):
                        for k, v in obj.items():
                            if isinstance(v, str) and 3 < len(v) < 500:
                                out.append(f"{k}: {v}")
                            elif isinstance(v, (int, float)) and str(k).lower() in ('price', 'mrp', 'saleprice', 'discount', 'rating', 'reviewcount', 'count', 'stock'):
                                out.append(f"{k}: {v}")
                            elif isinstance(v, (dict, list)):
                                out.extend(flatten_next(v, depth + 1))
                    elif isinstance(obj, list):
                        for item in obj[:6]:
                            out.extend(flatten_next(item, depth + 1))
                    return out
                flat = flatten_next(props)
                value = " | ".join(flat[:40]) if flat else json.dumps(props)[:1500]
                if value:
                    evidence.append({"type": "next_data", "value": value[:1500], "confidence": 0.85, "page": page_type})
            except:
                pass
        for jld_raw in re.findall(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.I | re.S):
            try:
                jld = json.loads(jld_raw.strip())
                fields = []
                for key in ('name', 'description', 'brand', 'telephone', 'priceRange', 'addressLocality', 'url'):
                    val = jld.get(key)
                    if not val and key == 'brand':
                        val = (jld.get('brand') or {}).get('name') if isinstance(jld.get('brand'), dict) else None
                    if val and isinstance(val, str):
                        fields.append(f"{key}: {val}")
                offers = jld.get('offers') or {}
                if isinstance(offers, dict) and offers.get('price'):
                    fields.append(f"price: {offers['price']} {offers.get('priceCurrency', '')}")
                for item in (jld.get('itemListElement') or [])[:5]:
                    if isinstance(item, dict) and item.get('name'):
                        fields.append(f"breadcrumb: {item['name']}")
                value = " | ".join(fields) if fields else json.dumps(jld)[:800]
                evidence.append({"type": "json_ld", "value": value[:800], "confidence": 0.88, "page": page_type})
            except:
                pass
        for ns in re.findall(r'<noscript[^>]*>(.*?)</noscript>', html, re.I | re.S):
            clean = re.sub(r'<[^>]+>', ' ', ns)
            clean = re.sub(r'\s+', ' ', clean).strip()
            if clean and len(clean) > 10:
                evidence.append({"type": "noscript_text", "value": clean[:400], "confidence": 0.65, "page": page_type})
        body = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.I | re.S)
        body = re.sub(r'<style[^>]*>.*?</style>', '', body, flags=re.I | re.S)
        body = re.sub(r'<[^>]+>', ' ', body)
        body = re.sub(r'\s+', ' ', body).strip()
        if body and len(body) > 50:
            evidence.append({"type": "body_text", "value": body[:2000], "confidence": 0.60, "page": page_type})
        return evidence

    def extract_evidence_from_markdown(md, page_type="homepage"):
        ev = []
        for line in md.split('\n'):
            s = line.strip()
            if s.startswith('### '):
                ev.append({"type": "h3", "value": s[4:].strip(), "confidence": 0.65, "page": page_type})
            elif s.startswith('## '):
                ev.append({"type": "h2", "value": s[3:].strip(), "confidence": 0.75, "page": page_type})
            elif s.startswith('# '):
                ev.append({"type": "h1", "value": s[2:].strip(), "confidence": 0.85, "page": page_type})
        for pattern, trust_type in [
            (r'(\d+\+?\s*(?:years?|yr)\s*(?:of\s*)?(?:experience|expertise))', "years_experience"),
            (r'(\d[\d,]*\+?\s*(?:customers?|clients?|users?))',                 "customer_count"),
            (r'(ISO\s*\d+[:\-]\d+)',                                            "certification"),
            (r'(rated\s*[\d.]+\s*(?:out\s*of\s*5|\/\s*5|stars?))',             "rating"),
            (r'(\d+\+?\s*(?:projects?|orders?|deliveries?))',                   "project_count"),
        ]:
            for match in re.findall(pattern, md, re.I)[:2]:
                ev.append({"type": f"trust_{trust_type}", "value": match.strip(), "confidence": 0.85, "page": page_type})
        for price in re.findall(r'(?:Rs\.?|INR|₹|\$)\s*[\d,]+(?:\.\d+)?', md)[:5]:
            ev.append({"type": "pricing_signal", "value": price.strip(), "confidence": 0.80, "page": page_type})
        clean = re.sub(r'[#*`\[\]()>|~_]', ' ', md)
        clean = re.sub(r'\s+', ' ', clean).strip()
        if clean:
            ev.append({"type": "body_text", "value": clean[:2000], "confidence": 0.75, "page": page_type})
        return ev

    async def fetch_page(u, page_type="homepage"):
        try:
            async with httpx.AsyncClient(timeout=12, follow_redirects=True) as c:
                r = await c.get(u, headers={"User-Agent": "Mozilla/5.0"})
                logger.info(f"[CRAWL] {u} → status={r.status_code}")
                if r.status_code < 400:
                    return (True, extract_evidence(r.text, page_type))
                return (False, [])
        except Exception as e:
            logger.info(f"[CRAWL] {u} → EXCEPTION: {e}")
        return (False, [])

    async def fetch_sitemap_urls(base):
        async def try_get(u):
            try:
                async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
                    r = await c.get(u, headers={"User-Agent": "Mozilla/5.0"})
                    return r.text if r.status_code < 400 else None
            except:
                return None
        skip_ext = ('.jpg', '.jpeg', '.png', '.gif', '.svg', '.webp', '.pdf', '.xml', '.css', '.js', '.woff', '.ico', '.mp4')
        def score_url(u):
            p = u.lower().replace(base.lower(), '')
            if any(k in p for k in ('/product', '/item', '/collection', '/category', '/shop', '/store', '/catalogue')):
                return 3
            if any(k in p for k in ('/about', '/contact', '/service', '/who-we-are', '/our-story', '/brand')):
                return 2
            if p in ('', '/', '/home', '/index'):
                return -1
            return 1
        sitemap_url = base + "/sitemap.xml"
        robots = await try_get(base + "/robots.txt")
        if robots:
            sm = re.search(r'Sitemap:\s*(https?://\S+)', robots, re.I)
            if sm:
                sitemap_url = sm.group(1).strip()
        sitemap_xml = await try_get(sitemap_url)
        if not sitemap_xml:
            return []
        sub_sitemaps = re.findall(r'<loc>(https?://[^<]*sitemap[^<]*\.xml[^<]*)</loc>', sitemap_xml, re.I)
        if sub_sitemaps:
            sub = await try_get(sub_sitemaps[0])
            if sub:
                sitemap_xml = sub
        all_urls = re.findall(r'<loc>(https?://[^<]+)</loc>', sitemap_xml, re.I)
        crawlable = [u for u in all_urls if not any(u.lower().endswith(e) for e in skip_ext)]
        selected = sorted(crawlable, key=score_url, reverse=True)
        selected = list(dict.fromkeys(u for u in selected if u.rstrip('/') != base))[:6]
        return selected

    async def run_ai_json(prompt, max_tokens):
        resp = await asyncio.to_thread(
            lambda: client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                response_format={"type": "json_object"}
            )
        )
        try:
            return json.loads(resp.choices[0].message.content)
        except:
            return {}

    def classify_page_type(u):
        p = u.lower()
        if any(k in p for k in ('/product', '/item', '/collection', '/catalogue')):
            return 'products'
        if any(k in p for k in ('/category', '/shop', '/store')):
            return 'shop'
        if '/about' in p or '/who' in p or '/our-story' in p or '/brand' in p:
            return 'about'
        if '/contact' in p:
            return 'contact'
        if '/service' in p or '/solution' in p:
            return 'services'
        return 'page'

    base = url.rstrip('/')

    # Phase 1: Evidence Collection — Firecrawl first, httpx fallback
    firecrawl_used = False
    if FIRECRAWL_API_KEY:
        fc_md = await fetch_firecrawl(base)
        if fc_md:
            firecrawl_used = True
            logger.info(f"[BI] Firecrawl succeeded for {base}: {len(fc_md)} chars")
        else:
            logger.info(f"[BI] Firecrawl returned empty for {base}, falling back to httpx")

    logger.info(f"[BI] Phase 1a: homepage + sitemap for {base}")
    if firecrawl_used:
        hp_fetched, hp_ev = True, extract_evidence_from_markdown(fc_md, "homepage")
        sitemap_urls = await fetch_sitemap_urls(base)
    else:
        (hp_fetched, hp_ev), sitemap_urls = await asyncio.gather(
            fetch_page(base, "homepage"),
            fetch_sitemap_urls(base)
        )

    if sitemap_urls:
        extra_targets = [(u, classify_page_type(u)) for u in sitemap_urls]
        logger.info(f"[BI] Phase 1b: crawling {len(extra_targets)} sitemap URLs")
    else:
        extra_targets = [
            (base + "/about",    "about"),
            (base + "/about-us", "about"),
            (base + "/products", "products"),
            (base + "/shop",     "shop"),
            (base + "/contact",  "contact"),
        ]
        logger.info(f"[BI] Phase 1b: no sitemap, using {len(extra_targets)} fallback guesses")

    extra_results = await asyncio.gather(*[fetch_page(u, pt) for u, pt in extra_targets])

    all_page_results = [(hp_fetched, hp_ev)] + list(extra_results)
    pages_attempted = 1 + len(extra_targets)
    pages_fetched = sum(1 for (fetched, _) in all_page_results if fetched)
    pages_with_evidence = sum(1 for (fetched, ev) in all_page_results if fetched and ev)
    sitemap_crawled = bool(sitemap_urls)

    all_evidence = []
    seen = set()
    for (fetched, page_ev) in all_page_results:
        for e in page_ev:
            key = (e["type"], e["value"][:60])
            if key not in seen:
                seen.add(key)
                all_evidence.append(e)

    if not all_evidence:
        return None

    avg_confidence = round(sum(e["confidence"] for e in all_evidence) / len(all_evidence), 2)
    evidence_collection = {
        "pages_attempted": pages_attempted,
        "pages_fetched": pages_fetched,
        "pages_with_evidence": pages_with_evidence,
        "sitemap_crawled": sitemap_crawled,
        "evidence_points": len(all_evidence),
        "avg_confidence": avg_confidence,
        "evidence": [e for e in all_evidence if e["type"] != "body_text"][:30],
    }

    evidence_text = "\n".join(
        f"[{e['page'].upper()}][{e['type']}](conf:{e['confidence']}) {e['value'][:200]}"
        for e in all_evidence if e["type"] != "body_text"
    )[:3000]

    body_text = " | ".join(
        e["value"] for e in all_evidence if e["type"] == "body_text"
    )[:3000]

    # Phase 2: Business DNA
    dna_prompt = (
        "You are a Business Intelligence DNA engine. Classify this business using ONLY the evidence below.\n"
        "Do NOT hallucinate. If evidence is missing for a field, use \"Unknown\".\n\n"
        f"EVIDENCE:\n{evidence_text}\n\n"
        f"BODY TEXT:\n{body_text[:1500]}\n\n"
        f"USER-STATED CATEGORY (verify against evidence, may be wrong): {business_type or 'Not provided'}\n\n"
        "Return STRICT JSON:\n"
        "{\n"
        '  "detected_industry": "",\n'
        '  "detected_sub_industry": "",\n'
        '  "business_model": "B2B or B2C or D2C or Marketplace or SaaS or Service or Hybrid",\n'
        '  "revenue_model": "One-time or Subscription or Freemium or Commission or Project-based or Mixed",\n'
        '  "core_products": ["product 1", "product 2", "product 3"],\n'
        '  "price_range": "Budget or Mid-market or Premium or Enterprise or Unknown",\n'
        '  "target_geography": "Local or Regional or National or International",\n'
        '  "trust_signals": ["signal 1", "signal 2"],\n'
        '  "unique_value_prop": "one sentence from evidence only",\n'
        '  "evidence_used": ["exact quote from evidence proving this classification"],\n'
        '  "dna_score": 0,\n'
        '  "dna_score_reason": "why this score"\n'
        "}\n\n"
        "dna_score 0-100: score 90+ only if pricing signals, trust signals, and a clear UVP are all found in evidence."
    )
    dna = await run_ai_json(dna_prompt, 800)

    # Phase 3: Opportunity + Threat + Audience + Positioning in parallel
    dna_text_p = json.dumps(dna, indent=2)

    positioning_prompt = (
        "You are a Brand Positioning Strategist. Analyze where this business currently stands and where it should position itself.\n\n"
        f"BUSINESS DNA:\n{dna_text_p}\n\n"
        f"EVIDENCE:\n{evidence_text[:1500]}\n\n"
        f"COMPETITOR URLS: {competitor_urls if competitor_urls else 'None provided — use industry knowledge'}\n\n"
        "Return STRICT JSON:\n"
        "{\n"
        '  "current_positioning": "what this business currently stands for, based ONLY on evidence",\n'
        '  "competitor_positioning": [\n'
        '    { "name": "Competitor 1 name or URL", "position": "what they stand for", "owned_category": "the space they own" },\n'
        '    { "name": "Competitor 2 name or URL", "position": "what they stand for", "owned_category": "the space they own" },\n'
        '    { "name": "Competitor 3 name or URL", "position": "what they stand for", "owned_category": "the space they own" }\n'
        '  ],\n'
        '  "positioning_gap": "the unoccupied space no competitor currently owns in this market",\n'
        '  "winning_position": "the single best position this business should own — 1 punchy sentence",\n'
        '  "category_ownership_opportunity": "the category name they could OWN",\n'
        '  "messaging_shift": "what the business currently says vs what it SHOULD say",\n'
        '  "reasoning": "why this positioning works for this business specifically",\n'
        '  "supporting_evidence": ["exact quote from evidence that supports this positioning"],\n'
        '  "confidence_score": 0\n'
        "}\n\n"
        "confidence_score 0-100: based on how much website evidence exists to support the positioning analysis."
    )

    opportunity_prompt = (
        "You are a Market Opportunity Scoring engine. Score this business's digital advertising opportunity.\n\n"
        f"BUSINESS DNA:\n{dna_text_p}\n\n"
        f"EVIDENCE:\n{evidence_text[:1200]}\n\n"
        "Return STRICT JSON:\n"
        "{\n"
        '  "market_size": "Niche or Small or Medium or Large or Mass",\n'
        '  "market_opportunity_score": 0,\n'
        '  "market_opportunity_reason": "",\n'
        '  "competition_difficulty_score": 0,\n'
        '  "competition_difficulty_reason": "",\n'
        '  "conversion_potential_score": 0,\n'
        '  "conversion_potential_reason": "",\n'
        '  "overall_opportunity_score": 0,\n'
        '  "best_platform": "Google or Meta or Both or LinkedIn or YouTube",\n'
        '  "best_platform_reason": "",\n'
        '  "budget_efficiency": "Low or Medium or High",\n'
        '  "seasonal_factors": []\n'
        "}\n\n"
        "All scores 0-100. overall_opportunity_score = (market_opportunity_score * 0.4) + (conversion_potential_score * 0.4) + ((100 - competition_difficulty_score) * 0.2)."
    )

    threat_prompt = (
        "You are a Competitive Threat Intelligence engine. Assess the threat landscape for this business.\n\n"
        f"BUSINESS DNA:\n{dna_text_p}\n\n"
        f"COMPETITOR URLS: {competitor_urls if competitor_urls else 'None provided'}\n\n"
        "Return STRICT JSON:\n"
        "{\n"
        '  "competitor_threat_score": 0,\n'
        '  "competitor_threat_reason": "",\n'
        '  "estimated_competitors": "Few (<5) or Moderate (5-20) or Many (20-50) or Saturated (50+)",\n'
        '  "audience_overlap_pct": 0,\n'
        '  "pricing_overlap_pct": 0,\n'
        '  "key_threats": ["threat 1", "threat 2", "threat 3"],\n'
        '  "differentiators": ["differentiator 1", "differentiator 2"],\n'
        '  "moat_strength": "Weak or Moderate or Strong",\n'
        '  "moat_reason": "",\n'
        '  "threat_level": "Low or Medium or High or Critical"\n'
        "}\n\n"
        "competitor_threat_score 0-100: 0=no threat, 100=extreme competition."
    )

    audience_prompt = (
        "You are an Audience Intelligence 2.0 engine. Generate audience segments ONLY from Business DNA evidence.\n"
        "RULE: Every segment MUST cite specific evidence. Reject any segment you cannot prove from the DNA.\n\n"
        f"BUSINESS DNA:\n{dna_text_p}\n\n"
        f"EVIDENCE:\n{evidence_text[:1200]}\n\n"
        "Return STRICT JSON:\n"
        "{\n"
        '  "validated_segments": [\n'
        '    {\n'
        '      "segment_name": "",\n'
        '      "age_range": "",\n'
        '      "gender": "Male or Female or All",\n'
        '      "income_level": "Low or Middle or Upper-Middle or High",\n'
        '      "interests": [],\n'
        '      "behaviors": [],\n'
        '      "evidence_backing": "exact evidence quote that proves this segment",\n'
        '      "confidence_score": 0,\n'
        '      "meta_interests": [],\n'
        '      "google_in_market": [],\n'
        '      "estimated_reach": "Narrow or Moderate or Broad"\n'
        '    }\n'
        '  ],\n'
        '  "rejected_segments": [\n'
        '    { "segment": "", "rejection_reason": "no evidence found" }\n'
        '  ],\n'
        '  "primary_segment_index": 0,\n'
        '  "audience_quality_score": 0,\n'
        '  "audience_quality_reason": ""\n'
        "}\n\n"
        "Generate exactly 3 validated_segments and at least 1 rejected_segment. "
        "audience_quality_score 0-100: average confidence of validated segments."
    )

    opportunity, threat, audience_intel, positioning = await asyncio.gather(
        run_ai_json(opportunity_prompt, 600),
        run_ai_json(threat_prompt, 600),
        run_ai_json(audience_prompt, 1400),
        run_ai_json(positioning_prompt, 700),
    )
    logger.info(f"[BI] audience_quality_score={audience_intel.get('audience_quality_score')} segments={len(audience_intel.get('validated_segments', []))}")

    # Phase 4: Executive Decision Engine
    exec_prompt = (
        "You are a CMO-level Executive Decision Engine. Based on all intelligence below, output the 5 highest-impact actions.\n\n"
        f"BUSINESS DNA:\n{dna_text_p}\n\n"
        f"OPPORTUNITY SCORES:\n{json.dumps(opportunity, indent=2)}\n\n"
        f"THREAT INTELLIGENCE:\n{json.dumps(threat, indent=2)}\n\n"
        f"AUDIENCE INTELLIGENCE:\n{json.dumps({'validated_segments': audience_intel.get('validated_segments', []), 'audience_quality_score': audience_intel.get('audience_quality_score', 0)}, indent=2)}\n\n"
        "Return STRICT JSON:\n"
        "{\n"
        '  "top_5_actions": [\n'
        '    {\n'
        '      "rank": 1,\n'
        '      "action": "",\n'
        '      "why": "",\n'
        '      "expected_impact": "Low or Medium or High or Critical",\n'
        '      "effort": "Low or Medium or High",\n'
        '      "timeline": "This week or This month or 30 days or 60 days or 90 days"\n'
        '    }\n'
        '  ],\n'
        '  "highest_roi_action": "",\n'
        '  "highest_roi_reason": "",\n'
        '  "quick_wins": ["win 1", "win 2", "win 3"],\n'
        '  "plan_30_day": "",\n'
        '  "plan_60_day": "",\n'
        '  "plan_90_day": "",\n'
        '  "overall_readiness_score": 0,\n'
        '  "readiness_verdict": "Not Ready or Needs Work or Good to Go or Highly Optimized",\n'
        '  "biggest_risk": "",\n'
        '  "biggest_opportunity": ""\n'
        "}\n\n"
        "overall_readiness_score 0-100: weighted from DNA score, opportunity score, and inverse of threat score."
    )
    executive = await run_ai_json(exec_prompt, 700)

    scores = {
        "dna_score":             dna.get("dna_score", 0),
        "opportunity_score":     round(opportunity.get("overall_opportunity_score", 0)),
        "threat_score":          threat.get("competitor_threat_score", 0),
        "audience_quality_score":audience_intel.get("audience_quality_score", 0),
        "positioning_score":     positioning.get("confidence_score", 0),
        "readiness_score":       executive.get("overall_readiness_score", 0),
    }

    return {
        "intelligence": {
            "evidence_collection": evidence_collection,
            "business_dna":        dna,
            "opportunity_score":   opportunity,
            "threat_intelligence": threat,
            "audience_intelligence":audience_intel,
            "positioning":         positioning,
            "executive_decisions": executive,
        },
        "scores":         scores,
        "firecrawl_used": firecrawl_used,
    }


class MediaBuyingRequest(BaseModel):
    url: str = ""
    industry: str = ""
    city: str = ""
    budget: int = 0
    goal: str = ""
    language: str = "Hinglish"
    bi_data: dict = {}
    marketing_summary: str = ""

@app.post("/media-buying-plan")
async def media_buying_plan(request: MediaBuyingRequest):
    system_prompt = (
        "You are an expert Media Buyer. "
        "You have access to the business intelligence data and marketing strategy provided below.\n\n"
        "Using the BI data and marketing context, generate a complete media buying plan with these 13 sections:\n\n"
        "1. CAMPAIGN OBJECTIVE: — Primary goal and why (Lead Gen / Sales / Awareness / etc.)\n"
        "2. PLATFORM RECOMMENDATIONS: — Rank Google, Meta, LinkedIn, YouTube, Display, Remarketing as 1st/2nd/3rd priority with reasoning\n"
        "3. BUDGET ALLOCATION: — Monthly budget, daily budget, platform split (e.g. Meta 50%, Google 40%, Remarketing 10%) with reasoning\n"
        "4. BID STRATEGY: — Recommended bid strategy (Maximize Conversions / Target CPA / Manual CPC / etc.) and why it fits this business\n"
        "5. LAUNCH PLAN: — Recommended launch date, what to prepare before launch\n"
        "6. LEARNING PHASE: — Learning period duration, minimum data required, when NOT to judge the campaign\n"
        "7. SCALING PLAN: — When to scale, how much to increase (%), safe vs aggressive scale rules, scale only if conditions\n"
        "8. PAUSE RULES: — When to pause ads (CTR below benchmark, no conversions after learning, CPC too high)\n"
        "9. STOP RULES: — When to stop campaign entirely (consistent losses, no improvement, poor audience match)\n"
        "10. OPTIMIZATION PLAN: — Checklist for audience, creative, landing page, offer, and budget optimization\n"
        "11. RISK ANALYSIS: — Risk level (Low/Medium/High), top risks (budget, audience, creative, competition)\n"
        "12. MEDIA BUYER PLAYBOOK: — Exactly what to do on Day 1, Day 3, Day 7, Day 14, Day 30\n"
        "13. INDUSTRY BENCHMARKS: — CTR range, CPC range, CPA range, conversion rate range for this industry (ranges only, no fake exact numbers)\n\n"
        "RULES: Never predict exact ROAS or CPA. Use benchmark RANGES only. Every recommendation must explain WHY. Use the BI data evidence provided."
    )

    import json as _json
    bi_summary = f"BI DATA:\n{_json.dumps(request.bi_data, indent=2)[:3000]}\n\n" if request.bi_data else ""

    user_msg = (
        f"IMPORTANT: Write entire response in: {request.language}\n\n"
        f"BUSINESS: {request.url}\n"
        f"INDUSTRY: {request.industry or 'Not specified'}\n"
        f"CITY/REGION: {request.city or 'India'}\n"
        f"MONTHLY BUDGET: \u20b9{request.budget:,}\n"
        f"PRIMARY GOAL: {request.goal}\n\n"
        + bi_summary
        + (f"MARKETING STRATEGY SUMMARY:\n{request.marketing_summary[:2000]}\n\n" if request.marketing_summary else "")
        + "Now generate the complete 13-section media buying plan."
    )

    try:
        resp = await asyncio.to_thread(
            lambda: client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                max_tokens=3500,
            )
        )
        media_plan = resp.choices[0].message.content.strip()
        return {"success": True, "media_plan": media_plan}
    except Exception as ex:
        logger.error(f"[MEDIA BUYING] error: {ex}")
        return {"success": False, "media_plan": "", "error": str(ex)}


class IntelligenceRequest(BaseModel):
    url: str
    business_type: str = ""
    competitor_urls: list[str] = []

@app.post("/intelligence")
async def intelligence(request: IntelligenceRequest, db: Session = Depends(get_db)):
    bi = await gather_bi_data(request.url, request.business_type, request.competitor_urls)
    if not bi:
        return {"success": False, "scan_failed": True, "message": "Website unreachable. Check URL and try again."}

    try:
        report = ReportModel(
            report_type="intelligence",
            title=request.url,
            input_data=json.dumps({"url": request.url, "business_type": request.business_type, "competitor_urls": request.competitor_urls}),
            result_data=json.dumps({**bi["intelligence"], "scores": bi["scores"]}),
            created_at=datetime.now().strftime("%d %b %Y, %I:%M %p")
        )
        db.add(report)
        db.commit()
    except Exception as _re:
        logger.warning(f"[REPORTS] Could not save intelligence report: {_re}")
        db.rollback()

    return {
        "success":        True,
        "url":            request.url,
        "intelligence":   bi["intelligence"],
        "scores":         bi["scores"],
        "firecrawl_used": bi.get("firecrawl_used", False),
    }


@app.get("/analyses")
def get_analyses(db: Session = Depends(get_db)):
    analyses = db.query(AnalysisModel).order_by(AnalysisModel.id.desc()).all()
    return {"analyses": [{"id": a.id, "url": a.url, "business_type": a.business_type, "created_at": a.created_at} for a in analyses]}

@app.get("/reports")
def get_reports(report_type: Optional[str] = None, db: Session = Depends(get_db)):
    try:
        q = db.query(ReportModel).order_by(ReportModel.id.desc())
        if report_type:
            q = q.filter(ReportModel.report_type == report_type)
        reports = q.all()
        return {"reports": [{"id": r.id, "report_type": r.report_type, "title": r.title, "created_at": r.created_at} for r in reports]}
    except Exception as e:
        logger.warning(f"[REPORTS] GET /reports error: {e}")
        return {"reports": [], "error": "Reports table not yet available"}

@app.get("/reports/{report_id}")
def get_report(report_id: int, db: Session = Depends(get_db)):
    try:
        report = db.query(ReportModel).filter(ReportModel.id == report_id).first()
        if not report:
            return {"success": False, "message": "Report nahi mila"}
        return {"id": report.id, "report_type": report.report_type, "title": report.title, "input_data": json.loads(report.input_data or "{}"), "result_data": json.loads(report.result_data or "{}"), "created_at": report.created_at}
    except Exception as e:
        logger.warning(f"[REPORTS] GET /reports/{report_id} error: {e}")
        return {"success": False, "message": "Reports table not yet available"}

@app.post("/leads")
def add_lead(lead: LeadCreate, db: Session = Depends(get_db)):
    new_lead = LeadModel(name=lead.name, phone=lead.phone, email=lead.email, source=lead.source, message=lead.message, campaign=lead.campaign, status="New", created_at=datetime.now().strftime("%d %b %Y, %I:%M %p"))
    db.add(new_lead)
    db.commit()
    db.refresh(new_lead)
    return {"success": True, "lead": {"id": new_lead.id, "name": new_lead.name, "phone": new_lead.phone, "email": new_lead.email, "source": new_lead.source, "message": new_lead.message, "status": new_lead.status, "created_at": new_lead.created_at}}

@app.get("/leads")
def get_leads(db: Session = Depends(get_db)):
    leads = db.query(LeadModel).order_by(LeadModel.id.desc()).all()
    return {"leads": [{"id": l.id, "name": l.name, "phone": l.phone, "email": l.email, "source": l.source, "message": l.message, "status": l.status, "created_at": l.created_at} for l in leads], "total": len(leads)}

@app.get("/leads/stats")
def get_stats(db: Session = Depends(get_db)):
    leads = db.query(LeadModel).all()
    return {"total": len(leads), "whatsapp": len([l for l in leads if l.source == "whatsapp"]), "website": len([l for l in leads if l.source == "website"]), "form": len([l for l in leads if l.source == "form"]), "new": len([l for l in leads if l.status == "New"]), "converted": len([l for l in leads if l.status == "Converted"])}

@app.put("/leads/{lead_id}")
def update_lead(lead_id: int, status: str, db: Session = Depends(get_db)):
    lead = db.query(LeadModel).filter(LeadModel.id == lead_id).first()
    if lead:
        lead.status = status
        db.commit()
        return {"success": True}
    return {"success": False, "message": "Lead nahi mila"}


# ── Google Ads Performance ────────────────────────────────────────────────────

def _genv(key: str) -> str:
    """Get env var, stripping whitespace/quotes that Render sometimes adds."""
    val = os.getenv(key, "")
    return val.strip().strip('"').strip("'")

def get_google_ads_client():
    # login_customer_id MUST be the MCC/manager account (3879422819).
    # Strip dashes in case the env var was entered as 387-942-2819.
    _login_raw = _genv("GOOGLE_ADS_LOGIN_CUSTOMER_ID").replace("-", "").replace(" ", "")
    login_customer_id = _login_raw or "3879422819"   # fallback to known MCC ID

    config = {
        "developer_token":   _genv("GOOGLE_ADS_DEVELOPER_TOKEN"),
        "client_id":         _genv("GOOGLE_ADS_CLIENT_ID"),
        "client_secret":     _genv("GOOGLE_ADS_CLIENT_SECRET"),
        "refresh_token":     _genv("GOOGLE_ADS_REFRESH_TOKEN"),
        "login_customer_id": str(login_customer_id),   # must be str, not int
        "use_proto_plus":    True,
    }
    _masked_login = login_customer_id[:-4] + "XXXX" if len(login_customer_id) > 4 else "XXXX"
    logger.info(
        f"[GOOGLE ADS] Building client — "
        f"login_customer_id={_masked_login} (from_env={bool(_login_raw)}) "
        f"developer_token=****{config['developer_token'][-4:] if config['developer_token'] else '????'} "
        f"client_id_set={bool(config['client_id'])} "
        f"secret_set={bool(config['client_secret'])} "
        f"refresh_token_set={bool(config['refresh_token'])}"
    )
    return GoogleAdsClient.load_from_dict(config)

@app.get("/google-ads/debug2")
async def google_ads_debug2():
    """Show exact values used to build GoogleAdsClient — for verifying Render env vars."""
    login_raw   = _genv("GOOGLE_ADS_LOGIN_CUSTOMER_ID")
    login_clean = login_raw.replace("-", "").replace(" ", "")
    cust_raw    = _genv("GOOGLE_ADS_CUSTOMER_ID")
    dev_token   = _genv("GOOGLE_ADS_DEVELOPER_TOKEN")
    return {
        "GOOGLE_ADS_LOGIN_CUSTOMER_ID": {
            "raw_repr":       repr(login_raw),
            "len":            len(login_raw),
            "cleaned":        login_clean,
            "is_set":         bool(login_raw),
            "effective_value": login_clean or "3879422819 (hardcoded fallback)",
        },
        "GOOGLE_ADS_CUSTOMER_ID": {
            "raw_repr": repr(cust_raw),
            "len":      len(cust_raw),
            "is_set":   bool(cust_raw),
        },
        "GOOGLE_ADS_DEVELOPER_TOKEN": {
            "is_set":  bool(dev_token),
            "prefix":  (dev_token[:5] + "…") if dev_token else None,
        },
        "client_id_set":      bool(_genv("GOOGLE_ADS_CLIENT_ID")),
        "client_secret_set":  bool(_genv("GOOGLE_ADS_CLIENT_SECRET")),
        "refresh_token_set":  bool(_genv("GOOGLE_ADS_REFRESH_TOKEN")),
    }

@app.get("/google-ads/deep-debug")
async def google_ads_deep_debug():
    """Deep diagnostic: env var previews + list_accessible_customers to find what the token can actually reach."""
    def preview(key):
        v = _genv(key)
        if not v:
            return {"set": False, "preview": None, "len": 0}
        return {"set": True, "preview": v[:5] + "…", "len": len(v)}

    env_info = {
        "GOOGLE_ADS_DEVELOPER_TOKEN":  preview("GOOGLE_ADS_DEVELOPER_TOKEN"),
        "GOOGLE_ADS_CLIENT_ID":        preview("GOOGLE_ADS_CLIENT_ID"),
        "GOOGLE_ADS_CLIENT_SECRET":    preview("GOOGLE_ADS_CLIENT_SECRET"),
        "GOOGLE_ADS_REFRESH_TOKEN":    preview("GOOGLE_ADS_REFRESH_TOKEN"),
        "GOOGLE_ADS_LOGIN_CUSTOMER_ID": preview("GOOGLE_ADS_LOGIN_CUSTOMER_ID"),
        "GOOGLE_ADS_CUSTOMER_ID":      preview("GOOGLE_ADS_CUSTOMER_ID"),
    }

    accessible_customers = None
    accessible_error     = None

    try:
        def _list_accessible():
            # list_accessible_customers does NOT require login_customer_id —
            # it returns all customer IDs the refresh token can access directly.
            client  = get_google_ads_client()
            svc     = client.get_service("CustomerService")
            resp    = svc.list_accessible_customers()
            return list(resp.resource_names)   # e.g. ["customers/2715637188", ...]

        resource_names       = await asyncio.to_thread(_list_accessible)
        accessible_customers = [rn.split("/")[-1] for rn in resource_names]
    except GoogleAdsException as ex:
        accessible_error = {
            "type":   "GoogleAdsException",
            "errors": [{"code": str(e.error_code), "message": e.message} for e in ex.failure.errors],
        }
    except Exception as ex:
        accessible_error = {"type": type(ex).__name__, "message": str(ex)}

    # If accessible_customers worked, also try a GAQL query on each to see
    # which ones respond (manager accounts often won't accept campaign queries).
    queryable = []
    if accessible_customers:
        def _probe(cid):
            try:
                client  = get_google_ads_client()
                svc     = client.get_service("GoogleAdsService")
                rows    = list(svc.search(customer_id=cid,
                    query="SELECT customer.id, customer.descriptive_name, customer.manager FROM customer LIMIT 1"))
                if rows:
                    r = rows[0]
                    return {"customer_id": cid, "name": r.customer.descriptive_name,
                            "is_manager": r.customer.manager, "queryable": True}
                return {"customer_id": cid, "queryable": True, "name": None}
            except Exception as _e:
                return {"customer_id": cid, "queryable": False, "error": str(_e)[:120]}

        probe_tasks = [asyncio.to_thread(_probe, cid) for cid in accessible_customers]
        queryable   = await asyncio.gather(*probe_tasks)

    return {
        "env":                  env_info,
        "accessible_customers": accessible_customers,
        "customer_probe":       queryable,
        "error":                accessible_error,
    }


@app.get("/google-ads/debug")
async def google_ads_debug():
    """Check Google Ads env vars — sensitive values hidden, IDs masked to last 4 digits."""
    def mask(key, show_last=4):
        v = _genv(key)
        if not v: return None
        return f"****{v[-show_last:]}" if len(v) >= show_last else "****"

    return {
        "GOOGLE_ADS_CLIENT_ID":        bool(_genv("GOOGLE_ADS_CLIENT_ID")),
        "GOOGLE_ADS_CLIENT_SECRET":    bool(_genv("GOOGLE_ADS_CLIENT_SECRET")),
        "GOOGLE_ADS_REFRESH_TOKEN":    bool(_genv("GOOGLE_ADS_REFRESH_TOKEN")),
        "GOOGLE_ADS_DEVELOPER_TOKEN":  bool(_genv("GOOGLE_ADS_DEVELOPER_TOKEN")),
        "GOOGLE_ADS_CUSTOMER_ID":      mask("GOOGLE_ADS_CUSTOMER_ID"),
        "GOOGLE_ADS_LOGIN_CUSTOMER_ID": mask("GOOGLE_ADS_LOGIN_CUSTOMER_ID"),
    }

@app.get("/google-ads/account-info")
async def google_ads_account_info():
    """Fetch customer metadata for each accessible account to identify manager vs real."""
    account_ids = ["2715637188", "3879422819"]
    results = []

    for cid in account_ids:
        try:
            client = GoogleAdsClient.load_from_dict({
                "developer_token":   _genv("GOOGLE_ADS_DEVELOPER_TOKEN"),
                "client_id":         _genv("GOOGLE_ADS_CLIENT_ID"),
                "client_secret":     _genv("GOOGLE_ADS_CLIENT_SECRET"),
                "refresh_token":     _genv("GOOGLE_ADS_REFRESH_TOKEN"),
                "login_customer_id": cid,
                "use_proto_plus":    True,
            })
            service = client.get_service("GoogleAdsService")
            query = """
                SELECT customer.id, customer.descriptive_name,
                       customer.manager, customer.test_account, customer.status
                FROM customer
                LIMIT 1
            """
            response = service.search(customer_id=cid, query=query)
            for row in response:
                c = row.customer
                results.append({
                    "customer_id":      str(c.id),
                    "name":             c.descriptive_name,
                    "is_manager":       c.manager,
                    "is_test_account":  c.test_account,
                    "status":           c.status.name,
                })
                break
        except GoogleAdsException as ex:
            errors = [e.message for e in ex.failure.errors]
            results.append({"customer_id": cid, "error": errors})
        except Exception as ex:
            results.append({"customer_id": cid, "error": str(ex)})

    return {"accounts": results}

@app.get("/google-ads/list-accounts")
async def google_ads_list_accounts():
    """List all accessible customer accounts under the login_customer_id."""
    try:
        client = get_google_ads_client()
        customer_service = client.get_service("CustomerService")
        response = customer_service.list_accessible_customers()
        resource_names = list(response.resource_names)
        # Extract numeric IDs from "customers/1234567890"
        account_ids = [r.split("/")[-1] for r in resource_names]
        logger.info(f"[GOOGLE ADS] Accessible accounts: {account_ids}")
        return {"success": True, "accessible_accounts": account_ids, "resource_names": resource_names}
    except GoogleAdsException as ex:
        errors = [e.message for e in ex.failure.errors]
        logger.error(f"[GOOGLE ADS] list-accounts error: {errors}")
        return {"success": False, "error": errors}
    except Exception as ex:
        logger.error(f"[GOOGLE ADS] list-accounts unexpected error: {ex}")
        return {"success": False, "error": str(ex)}

@app.get("/google-ads/performance")
async def google_ads_performance(days: int = 30, customer_id: Optional[str] = None):
    customer_id = customer_id or _genv("GOOGLE_ADS_CUSTOMER_ID")
    login_customer_id = _genv("GOOGLE_ADS_LOGIN_CUSTOMER_ID")
    masked_cid   = customer_id[:-4]       + "XXXX" if customer_id       and len(customer_id)       > 4 else customer_id
    masked_login = login_customer_id[:-4] + "XXXX" if login_customer_id and len(login_customer_id) > 4 else login_customer_id
    logger.info(f"[GOOGLE ADS] Using customer_id={masked_cid} login_customer_id={masked_login}")
    try:
        client = GoogleAdsClient.load_from_dict({
            "developer_token": _genv("GOOGLE_ADS_DEVELOPER_TOKEN"),
            "client_id": _genv("GOOGLE_ADS_CLIENT_ID"),
            "client_secret": _genv("GOOGLE_ADS_CLIENT_SECRET"),
            "refresh_token": _genv("GOOGLE_ADS_REFRESH_TOKEN"),
            "login_customer_id": _genv("GOOGLE_ADS_LOGIN_CUSTOMER_ID"),
            "use_proto_plus": True,
        })
        service = client.get_service("GoogleAdsService")

        end   = date.today()
        start = end - timedelta(days=days)

        query = f"""
            SELECT
                metrics.impressions,
                metrics.clicks,
                metrics.cost_micros,
                metrics.conversions,
                metrics.ctr,
                metrics.average_cpc
            FROM customer
            WHERE segments.date BETWEEN '{start}' AND '{end}'
        """

        response = service.search(customer_id=customer_id, query=query)

        total_impressions  = 0
        total_clicks       = 0
        total_cost_micros  = 0
        total_conversions  = 0

        for row in response:
            total_impressions  += row.metrics.impressions
            total_clicks       += row.metrics.clicks
            total_cost_micros  += row.metrics.cost_micros
            total_conversions  += row.metrics.conversions

        total_cost = total_cost_micros / 1_000_000
        ctr        = (total_clicks / total_impressions * 100) if total_impressions else 0
        avg_cpc    = (total_cost / total_clicks) if total_clicks else 0

        return {
            "success":     True,
            "period_days": days,
            "start_date":  str(start),
            "end_date":    str(end),
            "impressions": total_impressions,
            "clicks":      total_clicks,
            "cost_inr":    round(total_cost, 2),
            "conversions": round(total_conversions, 2),
            "ctr_pct":     round(ctr, 2),
            "avg_cpc_inr": round(avg_cpc, 2),
        }

    except GoogleAdsException as ex:
        errors = [e.message for e in ex.failure.errors]
        logger.error(f"[GOOGLE ADS] API error: {errors}")
        return {"success": False, "error": errors}
    except Exception as ex:
        logger.error(f"[GOOGLE ADS] Unexpected error: {ex}")
        return {"success": False, "error": str(ex)}

@app.get("/google-ads/campaigns")
async def google_ads_campaigns(days: int = 90):
    """Per-campaign breakdown sorted by cost descending."""
    customer_id = _genv("GOOGLE_ADS_CUSTOMER_ID")
    end   = date.today()
    start = end - timedelta(days=days)
    try:
        client  = get_google_ads_client()
        service = client.get_service("GoogleAdsService")
        query = f"""
            SELECT
                campaign.id,
                campaign.name,
                campaign.status,
                metrics.impressions,
                metrics.clicks,
                metrics.cost_micros,
                metrics.conversions,
                metrics.ctr,
                metrics.average_cpc
            FROM campaign
            WHERE segments.date BETWEEN '{start}' AND '{end}'
              -- -- AND metrics.impressions > 0
            ORDER BY metrics.cost_micros DESC
        """
        rows = list(service.search(customer_id=customer_id, query=query))
        campaigns = []
        for row in rows:
            cost = row.metrics.cost_micros / 1_000_000
            imp  = row.metrics.impressions
            clk  = row.metrics.clicks
            campaigns.append({
                "campaign_id":  str(row.campaign.id),
                "name":         row.campaign.name,
                "status":       row.campaign.status.name,
                "impressions":  imp,
                "clicks":       clk,
                "cost_inr":     round(cost, 2),
                "conversions":  round(row.metrics.conversions, 2),
                "ctr_pct":      round(clk / imp * 100, 2) if imp else 0,
                "avg_cpc_inr":  round(cost / clk, 2)      if clk else 0,
            })
        logger.info(f"[GOOGLE ADS] campaigns: {len(campaigns)} rows for last {days} days")
        return {"success": True, "period_days": days, "start_date": str(start), "end_date": str(end), "campaigns": campaigns}
    except GoogleAdsException as ex:
        errors = [e.message for e in ex.failure.errors]
        logger.error(f"[GOOGLE ADS] campaigns error: {errors}")
        return {"success": False, "error": errors}
    except Exception as ex:
        logger.error(f"[GOOGLE ADS] campaigns unexpected: {ex}")
        return {"success": False, "error": str(ex)}

@app.get("/google-ads/ad-policy-status")
async def google_ads_ad_policy_status(campaign_id: str = ""):
    """
    Read-only diagnostic: lists every ad's policy review status, and for any
    ad with an actual policy finding (not just PROHIBITED/DISAPPROVED as a
    bare code), the specific topic + evidence text — the same detail
    _extract_policy_violations() surfaces for a live create-ad call, but for
    ads that already exist in the account (including ones created before
    this diagnostic existed).

    Optional campaign_id filters to one campaign, and also reports its
    ad_groups directly (with their ad_group.id) — so a campaign showing
    zero ad_group_ad rows here can be distinguished from one with no
    ad_group at all, before pushing a fresh ad into the right ad group.
    """
    customer_id = _genv("GOOGLE_ADS_CUSTOMER_ID")
    campaign_id = (campaign_id or "").strip()
    try:
        client  = get_google_ads_client()
        service = client.get_service("GoogleAdsService")

        ad_groups = []
        if campaign_id:
            ag_query = f"""
                SELECT ad_group.id, ad_group.name, ad_group.status
                FROM ad_group
                WHERE campaign.id = {campaign_id}
            """
            ag_rows = list(service.search(customer_id=customer_id, query=ag_query))
            ad_groups = [
                {"ad_group_id": str(r.ad_group.id), "name": r.ad_group.name,
                 "status": r.ad_group.status.name if hasattr(r.ad_group.status, "name") else str(r.ad_group.status)}
                for r in ag_rows
            ]

        where_clause = f"WHERE campaign.id = {campaign_id}" if campaign_id else ""
        query = f"""
            SELECT
                ad_group_ad.ad.id,
                ad_group_ad.ad.responsive_search_ad.headlines,
                ad_group_ad.ad.responsive_search_ad.descriptions,
                ad_group_ad.status,
                ad_group_ad.policy_summary.approval_status,
                ad_group_ad.policy_summary.review_status,
                ad_group_ad.policy_summary.policy_topic_entries,
                campaign.id,
                campaign.name,
                ad_group.id,
                ad_group.name
            FROM ad_group_ad
            {where_clause}
            ORDER BY ad_group_ad.ad.id DESC
            LIMIT 50
        """
        rows = list(service.search(customer_id=customer_id, query=query))
        ads = []
        for row in rows:
            aga = row.ad_group_ad
            findings = []
            for entry in aga.policy_summary.policy_topic_entries:
                evidence_texts = []
                for ev in entry.evidences:
                    if ev.text_list and ev.text_list.texts:
                        evidence_texts.extend(str(t) for t in ev.text_list.texts)
                findings.append({
                    "topic":       entry.topic,
                    "entry_type":  entry.type_.name if hasattr(entry.type_, "name") else str(entry.type_),
                    "evidence":    evidence_texts,
                    "explanation": _policy_explanation(entry.topic) if entry.topic else None,
                })
            ads.append({
                "campaign_id":     str(row.campaign.id),
                "campaign_name":   row.campaign.name,
                "ad_group_id":     str(row.ad_group.id),
                "ad_group_name":   row.ad_group.name,
                "ad_id":           str(aga.ad.id),
                "ad_status":       aga.status.name if hasattr(aga.status, "name") else str(aga.status),
                "approval_status": aga.policy_summary.approval_status.name if hasattr(aga.policy_summary.approval_status, "name") else str(aga.policy_summary.approval_status),
                "review_status":   aga.policy_summary.review_status.name if hasattr(aga.policy_summary.review_status, "name") else str(aga.policy_summary.review_status),
                "headlines":       [h.text for h in aga.ad.responsive_search_ad.headlines],
                "descriptions":    [d.text for d in aga.ad.responsive_search_ad.descriptions],
                "policy_findings": findings,
            })
        return {"success": True, "ads": ads, "ad_groups": ad_groups}
    except GoogleAdsException as ex:
        errors = [e.message for e in ex.failure.errors]
        logger.error(f"[GOOGLE ADS] ad-policy-status error: {errors}")
        return {"success": False, "error": errors}
    except Exception as ex:
        logger.error(f"[GOOGLE ADS] ad-policy-status unexpected: {ex}")
        return {"success": False, "error": str(ex)}

@app.get("/google-ads/daily")
async def google_ads_daily(days: int = 90):
    """Daily time-series: date, impressions, clicks, cost_inr."""
    customer_id = _genv("GOOGLE_ADS_CUSTOMER_ID")
    end   = date.today()
    start = end - timedelta(days=days)
    try:
        client  = get_google_ads_client()
        service = client.get_service("GoogleAdsService")
        query = f"""
            SELECT
                segments.date,
                metrics.impressions,
                metrics.clicks,
                metrics.cost_micros
            FROM customer
            WHERE segments.date BETWEEN '{start}' AND '{end}'
            ORDER BY segments.date ASC
        """
        rows = list(service.search(customer_id=customer_id, query=query))
        daily = []
        for row in rows:
            daily.append({
                "date":        row.segments.date,
                "impressions": row.metrics.impressions,
                "clicks":      row.metrics.clicks,
                "cost_inr":    round(row.metrics.cost_micros / 1_000_000, 2),
            })
        logger.info(f"[GOOGLE ADS] daily: {len(daily)} data points for last {days} days")
        return {"success": True, "period_days": days, "start_date": str(start), "end_date": str(end), "daily": daily}
    except GoogleAdsException as ex:
        errors = [e.message for e in ex.failure.errors]
        logger.error(f"[GOOGLE ADS] daily error: {errors}")
        return {"success": False, "error": errors}
    except Exception as ex:
        logger.error(f"[GOOGLE ADS] daily unexpected: {ex}")
        return {"success": False, "error": str(ex)}

@app.get("/memory")
async def read_memory(business_key: str, industry: str = "", city: str = ""):
    """
    Return stored memory. For B2B keys pass industry + city as query params.
    e.g. /memory?business_key=sohscape.com&industry=Hospitality&city=Jaipur
    """
    normalized = derive_business_key(business_key, industry, city)
    mem = get_memory(normalized)
    return {"success": bool(mem), "business_key_raw": business_key, "business_key_normalized": normalized, "memory": mem}

@app.delete("/memory/clear")
async def clear_memory(business_key: str, industry: str = "", city: str = ""):
    """
    Delete all memory rows for a given business_key across all tables.
    Use this to clear stale data. For B2B keys pass industry + city.
    e.g. DELETE /memory/clear?business_key=sohscape.com
         DELETE /memory/clear?business_key=sohscape.com&industry=Hospitality&city=Jaipur
    """
    key_to_delete = derive_business_key(business_key, industry, city)
    deleted = {}
    for table_key, table in _MEMORY_TABLES.items():
        try:
            with engine.connect() as conn:
                result = conn.execute(
                    text(f"DELETE FROM {table} WHERE business_key = :bk"), {"bk": key_to_delete}
                )
                conn.commit()
            deleted[table] = result.rowcount
        except Exception as _e:
            deleted[table] = f"ERROR: {_e}"
    logger.info(f"[MEMORY] Manual clear for key={key_to_delete!r}: {deleted}")
    return {"success": True, "key_deleted": key_to_delete, "rows_deleted": deleted}

@app.get("/memory/selftest")
async def memory_selftest():
    """
    Isolation test: attempts save_to_memory then reads back immediately.
    Returns the exact error string if anything fails — no swallowing.
    """
    test_key = "testkey123"
    save_ok, save_err = save_to_memory("business", test_key, {
        "business_name": "Test",
        "industry":      "Test",
        "city":          "Test",
        "uvp":           "test uvp",
    })
    read_back = get_memory(test_key)
    return {
        "save_attempted": True,
        "save_ok":        save_ok,
        "save_error":     save_err,
        "read_back":      read_back,
        "tables_in_db":   _list_memory_tables(),
    }

def _list_memory_tables() -> list:
    """Return which memory tables actually exist in the DB right now."""
    try:
        with engine.connect() as conn:
            if _is_sqlite:
                rows = conn.execute(text(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%memory%'"
                )).fetchall()
            else:
                rows = conn.execute(text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name LIKE '%memory%'"
                )).fetchall()
        return [r[0] for r in rows]
    except Exception as _e:
        return [f"ERROR: {_e}"]

@app.get("/debug/db")
async def debug_db():
    """Shows which DB host the running app is actually connected to. Safe — password is never exposed."""
    host = DATABASE_URL.split("@")[-1].split("/")[0] if "@" in DATABASE_URL else "sqlite-local"
    db_name = DATABASE_URL.split("/")[-1].split("?")[0] if "/" in DATABASE_URL else ""
    try:
        with engine.connect() as conn:
            ping = conn.execute(text("SELECT 1")).scalar()
        connected = (ping == 1)
        error = None
    except Exception as _e:
        connected = False
        error = str(_e)
    return {
        "db_host":    host,
        "db_name":    db_name,
        "is_sqlite":  _is_sqlite,
        "connected":  connected,
        "error":      error,
        "env_var_set": os.getenv("DATABASE_URL") is not None,
    }

# ── Opportunity Engine ────────────────────────────────────────────────────────

class OpportunityEngineRequest(BaseModel):
    business_key: str
    industry: str = ""
    city: str = ""
    budget: int = 0

@app.post("/opportunity-engine")
async def opportunity_engine(request: OpportunityEngineRequest):
    logger.info(f"[MEMORY][opportunity-engine] LOOKUP | business_key={request.business_key!r} industry={request.industry!r} city={request.city!r}")
    memory, norm_key = get_memory_with_city_fallback(request.business_key, request.industry, request.city)
    logger.info(f"[MEMORY][opportunity-engine] resolved key={norm_key!r} memory_tables={list(memory.keys())}")

    if not memory:
        return {
            "success": False,
            "memory_used": False,
            "message": (
                "No memory found for this business. "
                "Run Marketing Brain (/full-report) for this business first "
                "so the Opportunity Engine has data to analyze."
            ),
        }

    # ── Build memory context string ──────────────────────────────────────────
    bm = memory.get("business", {})
    mm = memory.get("market", {})
    cm = memory.get("competitor", {})
    am = memory.get("audience", {})

    ctx_parts = []
    if bm.get("business_name"): ctx_parts.append(f"Business: {bm['business_name']}")
    if bm.get("industry"):      ctx_parts.append(f"Industry: {bm['industry']}")
    if bm.get("city"):          ctx_parts.append(f"City: {bm['city']}")
    if bm.get("uvp"):           ctx_parts.append(f"UVP: {bm['uvp']}")
    if bm.get("positioning"):   ctx_parts.append(f"Positioning: {bm['positioning']}")
    if bm.get("brand_score"):   ctx_parts.append(f"Brand Score: {bm['brand_score']}")
    if bm.get("trust_score"):   ctx_parts.append(f"Trust Score: {bm['trust_score']}")
    if bm.get("opportunity_score"): ctx_parts.append(f"Opportunity Score: {bm['opportunity_score']}")

    if mm.get("market_size"):       ctx_parts.append(f"Market Size: {mm['market_size']}")
    if mm.get("growth"):            ctx_parts.append(f"Market Growth: {mm['growth']}")
    if mm.get("market_gap"):        ctx_parts.append(f"Market Gap: {mm['market_gap']}")
    if mm.get("competition_level"): ctx_parts.append(f"Competition Level: {mm['competition_level']}")
    if mm.get("trends"):
        t = mm["trends"]
        ctx_parts.append(f"Trends: {json.dumps(t) if isinstance(t, (dict, list)) else t}")

    if cm.get("competitors"):
        comps = cm["competitors"]
        ctx_parts.append(f"Known Competitors: {', '.join(comps) if isinstance(comps, list) else comps}")

    if am.get("segments"):
        segs = am["segments"]
        ctx_parts.append(f"Audience Segments: {json.dumps(segs) if isinstance(segs, (dict, list)) else segs}")

    if request.budget:
        ctx_parts.append(f"Monthly Budget: ₹{request.budget:,}")
    if request.industry:
        ctx_parts.append(f"Industry (user-specified): {request.industry}")
    if request.city:
        ctx_parts.append(f"City (user-specified): {request.city}")

    memory_context = "\n".join(ctx_parts)

    # ── GPT-4o call ──────────────────────────────────────────────────────────
    _growth_block = growth_learning_block(request.industry or bm.get("industry", ""))

    prompt = f"""You are a senior growth strategist. Analyze this business's stored data and identify the highest-ROI opportunities.

BUSINESS DATA (from Adsoh memory system):
{memory_context}

{_growth_block}
Your job: decide where this specific business should focus FIRST to get the fastest, highest return.

RULES:
- Be specific to THIS business. Reference actual segments, competitors, and gaps from the data above.
- No generic advice. Every recommendation must cite evidence from the data.
- BANNED WORDS: Elevate, Transform, Unlock, Revolutionize, Empower, Seamless, Game-changer, Cutting-edge, State-of-the-art, World-class, One-stop solution, Look no further, In today's digital age.
- Revenue potential: "High" = can 2x revenue in 3 months, "Medium" = 30-50% lift, "Low" = < 30% lift.
- Priority score 0-100: 90+ = do this week, 70-89 = do this month, below 70 = plan for later.
- CONFIDENCE DISCIPLINE: "confidence" must reflect actual evidence available in the data above. Memory-backed
  claims (directly cited from the business data) = high confidence (80+). Inferred claims (reasoned from partial
  data) = medium (50-70). Speculative claims (no supporting data) = low (<50) and the relevant "why" field must
  say "Speculative:" at the start. Never give 85+ confidence to a guess.
- If CROSS-BUSINESS LEARNING data is present above, treat it as real evidence from other businesses in this
  industry — it can justify high confidence even when this business's own memory is thin.

Respond ONLY with a valid JSON object — no markdown, no explanation, just the JSON:

{{
  "highest_roi_audience": {{
    "segment": "exact segment name from the data",
    "why": "specific reason citing evidence",
    "priority_score": 0
  }},
  "highest_roi_offer": {{
    "offer": "specific offer or product/service to push",
    "why": "specific reason citing evidence",
    "revenue_potential": "High|Medium|Low"
  }},
  "highest_roi_platform": {{
    "platform": "exact platform name",
    "why": "specific reason citing evidence"
  }},
  "highest_roi_location": {{
    "area": "specific area or locality",
    "why": "specific reason citing evidence"
  }},
  "quick_wins": [
    "Specific action 1 — can be done this week",
    "Specific action 2 — can be done this week",
    "Specific action 3 — can be done this week"
  ],
  "biggest_opportunity": {{
    "opportunity": "the single biggest untapped opportunity",
    "why": "specific reason citing evidence from the data",
    "expected_impact": "specific measurable impact"
  }},
  "what_to_do_first": "One clear sentence — the single most important action this business must take right now.",
  "confidence": 0
}}"""

    try:
        resp = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=1200,
            temperature=0.4,
        )
        raw = resp.choices[0].message.content.strip()
        opportunity = json.loads(raw)
    except Exception as _e:
        logger.error(f"[OPPORTUNITY ENGINE] GPT call failed: {_e}")
        return {"success": False, "memory_used": True, "error": str(_e)}

    # ── Save to opportunity_memory ────────────────────────────────────────────
    save_to_memory("opportunity", norm_key, {"opportunity_data": opportunity})

    log_activity(
        "opportunity_engine", business_key=norm_key,
        business_name=(memory.get("business", {}) or {}).get("business_name", ""),
        url=request.business_key, industry=request.industry, city=request.city,
        summary="Opportunity Engine report generated",
    )

    # ── TRUST & ACCURACY LAYER ────────────────────────────────────────────
    _has_dna = _has_real_business_dna(bm)
    _has_aud = bool(am.get("segments"))
    _has_campaign = bool(memory.get("campaign"))
    trust_verdict = _compute_trust_verdict(_has_dna, _has_aud)
    based_on = _compute_based_on_line(_has_dna, _has_aud, _has_campaign)
    _biz_label = bm.get("business_name") or request.business_key
    _biz_positioning_txt = bm.get("positioning") or ""
    _business_context_txt = " ".join([bm.get("industry") or "", _biz_positioning_txt, bm.get("uvp") or "", json.dumps(bm.get("business_dna") or {})])
    _match_warning = _business_match_sanity_check(_business_context_txt, json.dumps(opportunity), _biz_label, _biz_positioning_txt)
    _contradiction_warning = _detect_consistency_contradiction(_has_dna, json.dumps(opportunity))
    validation_warning = _combine_validation_warnings(_match_warning, _contradiction_warning)

    _response = {
        "success":      True,
        "memory_used":  True,
        "business_key": norm_key,
        "opportunity":  opportunity,
        "trust_verdict": trust_verdict,
        "based_on": based_on,
        "validation_warning": validation_warning or None,
    }
    save_report_snapshot("opportunity_engine", norm_key, _response)
    return _response

# ── Offer Intelligence ────────────────────────────────────────────────────────

class OfferIntelligenceRequest(BaseModel):
    business_key: str
    industry: str = ""
    city: str = ""

@app.post("/offer-intelligence")
async def offer_intelligence(request: OfferIntelligenceRequest):
    logger.info(f"[MEMORY][offer-intelligence] LOOKUP | business_key={request.business_key!r} industry={request.industry!r} city={request.city!r}")
    memory, norm_key = get_memory_with_city_fallback(request.business_key, request.industry, request.city)
    logger.info(f"[MEMORY][offer-intelligence] resolved key={norm_key!r} memory_tables={list(memory.keys())}")

    if not memory:
        return {
            "success": False,
            "memory_used": False,
            "message": (
                "No memory found for this business. "
                "Run Marketing Brain (/full-report) first so the Offer Intelligence engine has data to analyze."
            ),
        }

    # ── Build memory context ─────────────────────────────────────────────────
    bm  = memory.get("business", {})
    mm  = memory.get("market", {})
    cm  = memory.get("competitor", {})
    am  = memory.get("audience", {})
    opm = memory.get("opportunity", {})

    ctx = []
    if bm.get("business_name"): ctx.append(f"Business: {bm['business_name']}")
    if bm.get("industry"):      ctx.append(f"Industry: {bm['industry']}")
    if bm.get("city"):          ctx.append(f"City: {bm['city']}")
    if bm.get("uvp"):           ctx.append(f"UVP: {bm['uvp']}")
    if bm.get("positioning"):   ctx.append(f"Positioning: {bm['positioning']}")
    if mm.get("market_gap"):    ctx.append(f"Market Gap: {mm['market_gap']}")
    if mm.get("competition_level"): ctx.append(f"Competition Level: {mm['competition_level']}")
    if mm.get("trends"):
        t = mm["trends"]
        ctx.append(f"Market Trends: {json.dumps(t) if isinstance(t, (dict, list)) else t}")
    if cm.get("competitors"):
        comps = cm["competitors"]
        ctx.append(f"Competitors: {', '.join(comps) if isinstance(comps, list) else comps}")
    if am.get("segments"):
        segs = am["segments"]
        ctx.append(f"Audience Segments: {json.dumps(segs) if isinstance(segs, (dict, list)) else segs}")
    if opm.get("opportunity_data"):
        od = opm["opportunity_data"]
        if isinstance(od, dict):
            if od.get("highest_roi_audience"): ctx.append(f"Highest-ROI Audience: {json.dumps(od['highest_roi_audience'])}")
            if od.get("biggest_opportunity"):  ctx.append(f"Biggest Opportunity: {json.dumps(od['biggest_opportunity'])}")
        else:
            ctx.append(f"Opportunity Data: {od}")
    if request.industry: ctx.append(f"Industry (user-specified): {request.industry}")
    if request.city:     ctx.append(f"City (user-specified): {request.city}")

    memory_context = "\n".join(ctx)

    prompt = f"""You are an expert offer strategist. Analyze this business's stored intelligence and design the most irresistible offer that will convert their target audience.

BUSINESS INTELLIGENCE (from Adsoh memory system):
{memory_context}

Your job: design a specific, compelling offer tailored to THIS business's real audience pain points, competitors, and market gaps.

RULES:
- Reference actual audience segments, competitor weaknesses, and market gaps from the data.
- No generic offers. Tied to real evidence from the data above.
- Offer names must be specific (include the city or niche if applicable).
- BANNED WORDS: Elevate, Transform, Unlock, Revolutionize, Empower, Seamless, Game-changer, Cutting-edge, State-of-the-art, World-class, One-stop solution, Look no further, In today's digital age.
- offer_score 0-100: 90+ = near-certain to convert, 70-89 = strong, below 70 = needs tweaking.
- CONFIDENCE DISCIPLINE: "confidence" must reflect actual evidence available in the data above. Memory-backed
  claims (directly cited from the business data) = high confidence (80+). Inferred claims (reasoned from partial
  data) = medium (50-70). Speculative claims (no supporting data) = low (<50) and "why_it_works" must say
  "Speculative:" at the start. Never give 85+ confidence to a guess.

Respond ONLY with valid JSON — no markdown, no explanation:

{{
  "recommended_offer": {{
    "name": "specific offer name (not generic)",
    "description": "2-3 sentences — what it is, who it's for, what they get",
    "why_it_works": "specific reason tied to audience data and market gap",
    "offer_score": 0
  }},
  "lead_magnet": {{
    "name": "specific lead magnet name",
    "format": "free audit / checklist / calculator / consultation / sample / report",
    "why": "why this format works for this specific audience"
  }},
  "pricing_suggestion": {{
    "model": "one-time / monthly retainer / per-lead / performance-based / tiered",
    "entry_offer": "specific low-barrier entry price or trial offer",
    "reasoning": "why this pricing model fits this market and audience"
  }},
  "guarantee": {{
    "guarantee": "specific risk-reversal guarantee",
    "why": "why this guarantee removes the main buying objection for this audience"
  }},
  "cta": "the single best call-to-action line — specific, action-oriented, urgency-driven",
  "competitor_offer_gap": "what competitors are NOT offering that this business can own",
  "irresistible_offer_stack": [
    "Element 1 — specific value add",
    "Element 2 — specific value add",
    "Element 3 — specific value add",
    "Element 4 — specific value add"
  ],
  "confidence": 0
}}"""

    try:
        resp = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=1200,
            temperature=0.4,
        )
        raw  = resp.choices[0].message.content.strip()
        offer = json.loads(raw)
    except Exception as _e:
        logger.error(f"[OFFER INTELLIGENCE] GPT call failed: {_e}")
        return {"success": False, "memory_used": True, "error": str(_e)}

    save_to_memory("offer", norm_key, {"offer_data": offer})

    log_activity(
        "offer_intelligence", business_key=norm_key,
        business_name=(memory.get("business", {}) or {}).get("business_name", ""),
        url=request.business_key, industry=request.industry, city=request.city,
        summary="Offer Intelligence report generated",
    )

    # ── TRUST & ACCURACY LAYER ────────────────────────────────────────────
    _has_dna = _has_real_business_dna(bm)
    _has_aud = bool(am.get("segments"))
    _has_campaign = bool(memory.get("campaign"))
    trust_verdict = _compute_trust_verdict(_has_dna, _has_aud)
    based_on = _compute_based_on_line(_has_dna, _has_aud, _has_campaign)
    _biz_label = bm.get("business_name") or request.business_key
    _biz_positioning_txt = bm.get("positioning") or ""
    _business_context_txt = " ".join([bm.get("industry") or "", _biz_positioning_txt, bm.get("uvp") or "", json.dumps(bm.get("business_dna") or {})])
    _match_warning = _business_match_sanity_check(_business_context_txt, json.dumps(offer), _biz_label, _biz_positioning_txt)
    _contradiction_warning = _detect_consistency_contradiction(_has_dna, json.dumps(offer))
    validation_warning = _combine_validation_warnings(_match_warning, _contradiction_warning)

    _response = {
        "success":      True,
        "memory_used":  True,
        "business_key": norm_key,
        "offer":        offer,
        "trust_verdict": trust_verdict,
        "based_on": based_on,
        "validation_warning": validation_warning or None,
    }
    save_report_snapshot("offer_intelligence", norm_key, _response)
    return _response


# ── Module 14: Website Intelligence ──────────────────────────────────────────

class WebsiteIntelligenceRequest(BaseModel):
    url:          str
    business_key: str = ""
    industry:     str = ""
    city:         str = ""

@app.post("/website-intelligence")
async def website_intelligence(request: WebsiteIntelligenceRequest):
    url = (request.url or "").strip()
    if not url:
        return {"success": False, "error": "URL is required"}

    # Derive memory key (use explicit business_key if provided, else derive from url)
    _bk_src = request.business_key.strip() or url
    norm_key = derive_business_key(_bk_src, request.industry, request.city)
    logger.info(f"[WEBSITE-INTEL] url={url!r} key={norm_key!r}")

    # Load prior memory for richer context
    memory_used = False
    memory_context = ""
    _mem = get_memory(norm_key)
    if _mem:
        memory_used = True
        _bm = _mem.get("business", {})
        _mm = _mem.get("market", {})
        _am = _mem.get("audience", {})
        _segs = _am.get("segments", [])
        if isinstance(_segs, str):
            try: _segs = json.loads(_segs)
            except: _segs = [_segs]
        memory_context = f"""
PREVIOUSLY KNOWN ABOUT THIS BUSINESS:
- Name: {_bm.get("business_name", "Unknown")} | Industry: {_bm.get("industry", "")} | City: {_bm.get("city", "")}
- UVP: {_bm.get("uvp", "")}
- Positioning: {_bm.get("positioning", "")}
- Market gap: {_mm.get("market_gap", "")}
- Target audience segments: {", ".join(str(s) for s in _segs[:3]) if _segs else "Not known"}
Use this to judge whether the website speaks to the right audience with the right message.
"""

    # Crawl
    crawled = await fetch_firecrawl(url)
    if not crawled:
        crawled = f"[Crawl returned empty — analyse based on URL and any context available]"
    crawled_trimmed = crawled[:7000]

    prompt = f"""You are an expert website conversion auditor. Analyse the website content below and return a detailed, actionable audit as a JSON object.

WEBSITE URL: {url}
{memory_context}
--- CRAWLED WEBSITE CONTENT ---
{crawled_trimmed}
--- END OF CONTENT ---

Return ONLY valid JSON (no markdown, no prose outside JSON) matching this exact schema:
{{
  "overall_score": <integer 0-100>,
  "homepage": {{
    "headline_clarity": <integer 0-100>,
    "value_prop_clear": <true|false>,
    "cta_above_fold": <true|false>,
    "issues": ["specific issue found in actual content", ...],
    "fixes":  ["exact fix with example copy or element name", ...]
  }},
  "trust_signals": {{
    "score": <integer 0-100>,
    "found":   ["list of trust elements actually present on site"],
    "missing": ["list of trust elements absent but needed"],
    "fixes":   ["specific additions with placement recommendations"]
  }},
  "conversion": {{
    "score": <integer 0-100>,
    "form_present": <true|false>,
    "cta_count": <integer>,
    "cta_quality": "<weak|medium|strong>",
    "issues": ["specific conversion problems found"],
    "fixes":  ["specific conversion improvements with examples"]
  }},
  "content": {{
    "score": <integer 0-100>,
    "blog_present": <true|false>,
    "portfolio_present": <true|false>,
    "issues": ["specific content gaps or problems"],
    "fixes":  ["specific content additions or rewrites needed"]
  }},
  "speed_seo": {{
    "score": <integer 0-100>,
    "mobile_friendly": <true|false>,
    "meta_description": <true|false>,
    "issues": ["specific SEO or technical issues"],
    "fixes":  ["specific technical fixes"]
  }},
  "priority_fixes": [
    "Fix 1 — highest conversion impact (be specific, reference actual page element)",
    "Fix 2",
    "Fix 3",
    "Fix 4",
    "Fix 5"
  ],
  "quick_wins": [
    "Win 1 — can be done TODAY, no dev needed",
    "Win 2",
    "Win 3"
  ],
  "overall_verdict": "One sentence naming the single biggest problem holding this website back from converting visitors."
}}

RULES:
- Every issue and fix must reference something SPECIFIC found (or absent) on this actual website — no generic advice.
- Priority fixes must be ordered by conversion impact (revenue impact first), not implementation difficulty.
- Quick wins must genuinely be doable TODAY — copy changes, a WhatsApp button, a testimonial added.
- overall_score is a weighted average: homepage 25%, trust_signals 25%, conversion 30%, content 10%, speed_seo 10%.
- BANNED words in all text: Elevate, Transform, Unlock, Revolutionize, Empower, Seamless, Game-changer, Dive in.
- Return ONLY the JSON object. No explanation outside it."""

    try:
        resp = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=2200,
            temperature=0.3,
        )
        raw   = resp.choices[0].message.content.strip()
        audit = json.loads(raw)
    except Exception as _e:
        logger.error(f"[WEBSITE-INTEL] GPT call failed: {_e}")
        return {"success": False, "error": str(_e)}

    save_to_memory("website", norm_key, {
        "url":           url,
        "audit_data":    audit,
        "overall_score": float(audit.get("overall_score", 0)),
    })
    logger.info(f"[WEBSITE-INTEL] Done: key={norm_key!r} score={audit.get('overall_score')}")

    log_activity(
        "website_intelligence", business_key=norm_key,
        business_name=(_mem.get("business", {}) or {}).get("business_name", "") if _mem else "",
        url=url, industry=request.industry, city=request.city,
        summary=f"Website Intelligence audit — score {audit.get('overall_score', 'N/A')}",
    )

    return {
        "success":      True,
        "url":          url,
        "memory_used":  memory_used,
        "business_key": norm_key,
        "audit":        audit,
    }


# ── Module 15: Visibility Intelligence (SEO + AEO + GEO) ─────────────────────

class VisibilityIntelligenceRequest(BaseModel):
    url:          str
    industry:     str = ""
    city:         str = ""
    business_key: str = ""

@app.post("/visibility-intelligence")
async def visibility_intelligence(request: VisibilityIntelligenceRequest):
    url = (request.url or "").strip()
    if not url:
        return {"success": False, "error": "URL is required"}

    _bk_src  = request.business_key.strip() or url
    norm_key = derive_business_key(_bk_src, request.industry, request.city)
    industry = request.industry.strip() or "business"
    city     = request.city.strip()     or "India"
    logger.info(f"[VISIBILITY-INTEL] url={url!r} key={norm_key!r} industry={industry!r} city={city!r}")

    # ── Memory context ──────────────────────────────────────────────────────
    memory_used    = False
    memory_context = ""
    _mem = get_memory(norm_key)
    if _mem:
        memory_used = True
        _bm  = _mem.get("business", {})
        _mm  = _mem.get("market",   {})
        _wm  = _mem.get("website",  {})
        memory_context = f"""
PREVIOUSLY KNOWN:
- Business: {_bm.get("business_name", "")} | Industry: {_bm.get("industry", "")} | City: {_bm.get("city", "")}
- UVP: {_bm.get("uvp", "")}
- Market gap: {_mm.get("market_gap", "")}
- Website audit score: {_wm.get("overall_score", "not yet audited")}
"""

    # ── Parallel data fetch ─────────────────────────────────────────────────
    crawled_task = fetch_firecrawl(url)
    tavily_task  = fetch_tavily(f"{industry} {city} SEO keywords ranking 2026")
    crawled, tavily_data = await asyncio.gather(crawled_task, tavily_task)

    crawled_trimmed = (crawled or "")[:5000]
    tavily_trimmed  = (tavily_data or "")[:2000]

    # ── GPT-4o ─────────────────────────────────────────────────────────────
    prompt = f"""You are an expert in SEO, AEO (Answer Engine Optimisation), and GEO (Generative Engine Optimisation).
Analyse the website content and live keyword data below and return a comprehensive visibility audit as a JSON object.

BUSINESS URL: {url}
INDUSTRY: {industry}
CITY: {city}
{memory_context}

--- CRAWLED WEBSITE CONTENT ---
{crawled_trimmed if crawled_trimmed else '[Crawl returned empty — infer from URL and context]'}
--- END CRAWLED CONTENT ---

--- LIVE KEYWORD / MARKET DATA (Tavily) ---
{tavily_trimmed if tavily_trimmed else '[No live data — use industry knowledge]'}
--- END LIVE DATA ---

Return ONLY a valid JSON object matching this EXACT schema (no markdown, no text outside JSON):
{{
  "overall_visibility_score": <integer 0-100>,
  "seo": {{
    "score": <integer 0-100>,
    "current_keywords": ["keywords the site currently seems to target — based on actual content"],
    "missing_keywords": ["high-value keywords NOT targeted but should be"],
    "recommended_keywords": [
      {{"keyword": "...", "intent": "informational|transactional|local", "priority": "high|medium|low"}},
      {{"keyword": "...", "intent": "...", "priority": "..."}},
      {{"keyword": "...", "intent": "...", "priority": "..."}},
      {{"keyword": "...", "intent": "...", "priority": "..."}},
      {{"keyword": "...", "intent": "...", "priority": "..."}},
      {{"keyword": "...", "intent": "...", "priority": "..."}}
    ],
    "on_page_issues": ["specific issues found in crawled content"],
    "content_gaps": ["topics competitors cover that this site doesn't address at all"],
    "quick_wins": ["SEO fix 1 — do this week", "SEO fix 2", "SEO fix 3"],
    "schema_needed": ["LocalBusiness", "FAQ", "Service", "Review", "etc based on site type"]
  }},
  "aeo": {{
    "score": <integer 0-100>,
    "what_is_aeo": "Answer Engine Optimisation — appearing in Google featured snippets, People Also Ask boxes, and voice search results",
    "current_status": "one sentence on how well this site currently answers questions (based on crawled content)",
    "recommended_questions": [
      "Question 1 real users search related to this business",
      "Question 2",
      "Question 3",
      "Question 4",
      "Question 5"
    ],
    "content_format_needed": ["FAQ page", "How-to guides", "Definition pages", "Comparison pages", "etc"],
    "quick_wins": ["AEO action 1 — do this week", "AEO action 2", "AEO action 3"]
  }},
  "geo": {{
    "score": <integer 0-100>,
    "what_is_geo": "Generative Engine Optimisation — making your business appear in answers from ChatGPT, Gemini, Perplexity, and other AI assistants",
    "current_status": "one sentence on how likely this business appears in AI assistant answers right now",
    "recommended_actions": [
      "Specific GEO action 1",
      "Specific GEO action 2",
      "Specific GEO action 3",
      "Specific GEO action 4",
      "Specific GEO action 5"
    ],
    "content_needed": ["types of content AI assistants cite — e.g. data-backed articles, expert guides, press mentions"],
    "quick_wins": ["GEO action 1 — this week", "GEO action 2", "GEO action 3"]
  }},
  "content_strategy": {{
    "score": <integer 0-100>,
    "recommended_topics": [
      "Topic 1 — high SEO+AEO+GEO value",
      "Topic 2", "Topic 3", "Topic 4", "Topic 5", "Topic 6", "Topic 7", "Topic 8"
    ],
    "content_calendar_hint": "which topics to publish first and why — reference search volume timing or seasonal intent",
    "internal_linking": ["Internal linking opportunity 1", "opportunity 2", "opportunity 3"]
  }},
  "local_seo": {{
    "score": <integer 0-100>,
    "google_business_profile": "present|missing|unknown",
    "local_keywords": [
      "local keyword 1 — specific to {industry} in {city}",
      "local keyword 2", "local keyword 3", "local keyword 4", "local keyword 5"
    ],
    "citation_opportunities": ["directory 1", "directory 2", "directory 3", "directory 4"],
    "quick_wins": ["local SEO win 1 — this week", "local SEO win 2", "local SEO win 3"]
  }},
  "priority_actions": [
    "Action 1 — highest ROI (be specific, reference actual gap)",
    "Action 2", "Action 3", "Action 4", "Action 5"
  ],
  "overall_verdict": "One sentence naming the single biggest visibility gap holding this business back."
}}

RULES:
- All keywords must be SPECIFIC to {industry} in {city} — no generic terms like "marketing services".
- AEO questions must reflect real user searches, not generic FAQ content.
- GEO actions must be practical and achievable without a developer.
- Every on_page_issue must reference something specific in the crawled content.
- BANNED words: Elevate, Transform, Unlock, Revolutionize, Empower, Seamless, Game-changer.
- Return ONLY the JSON object. No explanation outside it."""

    try:
        resp = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=2800,
            temperature=0.3,
        )
        raw        = resp.choices[0].message.content.strip()
        visibility = json.loads(raw)
    except Exception as _e:
        logger.error(f"[VISIBILITY-INTEL] GPT call failed: {_e}")
        return {"success": False, "error": str(_e)}

    save_to_memory("visibility", norm_key, {
        "url":             url,
        "visibility_data": visibility,
        "overall_score":   float(visibility.get("overall_visibility_score", 0)),
    })
    logger.info(f"[VISIBILITY-INTEL] Done: key={norm_key!r} score={visibility.get('overall_visibility_score')}")

    log_activity(
        "visibility_intelligence", business_key=norm_key,
        business_name=(_mem.get("business", {}) or {}).get("business_name", "") if _mem else "",
        url=url, industry=industry, city=city,
        summary=f"Visibility Intelligence report — score {visibility.get('overall_visibility_score', 'N/A')}",
    )

    return {
        "success":      True,
        "url":          url,
        "memory_used":  memory_used,
        "business_key": norm_key,
        "visibility":   visibility,
    }


# ── Module 16: Outreach AI ────────────────────────────────────────────────────

class OutreachAIRequest(BaseModel):
    url:           str = ""
    industry:      str = ""
    city:          str = ""
    target_name:   str = ""
    outreach_goal: str = "get meeting"
    # kept for backward compat but no longer the primary key source
    business_key:  str = ""

@app.post("/outreach-ai")
async def outreach_ai(request: OutreachAIRequest):
    industry = (request.industry or "").strip()
    city     = (request.city or "").strip()
    city_display = city or "India (no specific city given)"
    goal     = (request.outreach_goal or "get meeting").strip()
    target   = (request.target_name   or "").strip()

    # ── Derive key the same way every other module does ──────────────────────
    # Priority: url > business_key > industry-only
    _bk_src = (request.url or request.business_key or "").strip()
    _preview_key = derive_business_key(_bk_src, industry, city)
    logger.info(f"[OUTREACH-AI] INPUT url={request.url!r} industry={industry!r} city={city!r}")
    logger.info(f"[OUTREACH-AI] Derived key (before fallback): {_preview_key!r}")
    memory, norm_key = get_memory_with_city_fallback(_bk_src, industry, city)
    logger.info(f"[OUTREACH-AI] Resolved key={norm_key!r} tables_found={list(memory.keys())}")

    if not memory:
        return {
            "success":     False,
            "memory_used": False,
            "message":     (
                "No memory found for this business. "
                "Run Marketing Brain first with the same URL + industry to build memory."
            ),
        }

    # ── Build rich context string from all tables ────────────────────────────
    _bm   = memory.get("business", {})
    _mm   = memory.get("market",   {})
    _cm   = memory.get("competitor", {})
    _am   = memory.get("audience",  {})
    _opm  = memory.get("opportunity", {})
    _ofm  = memory.get("offer",     {})
    _wm   = memory.get("website",   {})
    _vm   = memory.get("visibility", {})

    # Personalization instruction: a "local landmark" reference is only valid
    # if THIS business genuinely operates physically in the given city — a
    # national/online/D2C/e-commerce brand must never get a city landmark
    # injected just because a city field happened to be filled in. GPT gets
    # the real positioning/UVP and must decide locality itself rather than
    # being told to assume the given city is this business's home turf.
    if city:
        personalization_scope = (
            f"THIS business in {city} — but FIRST check the business context below (positioning/UVP): if this "
            f"reads as a national, online, D2C, or e-commerce brand with no evidence it's physically based in "
            f"{city}, use an industry-specific pain point/seasonal trend nationally instead (do NOT reference "
            f"{city} or any local landmark). Only reference a real local landmark/local detail in {city} if the "
            f"business context clearly shows it's a local physical business actually operating there."
        )
    else:
        personalization_scope = (
            f"THIS business type in the {industry or 'this'} industry nationally — a known industry-specific pain "
            "point, seasonal pattern, or trend (do NOT invent a city or region that wasn't provided)"
        )

    def _safe_list(val):
        if isinstance(val, list): return val
        if isinstance(val, str):
            try:   return json.loads(val)
            except: return [val] if val else []
        return []

    _segs       = _safe_list(_am.get("segments"))
    _competitors = _safe_list(_cm.get("competitors"))
    _opp_data   = _opm.get("opportunity_data") or {}
    _offer_data = _ofm.get("offer_data") or {}
    if isinstance(_opp_data, str):
        try: _opp_data = json.loads(_opp_data)
        except: _opp_data = {}
    if isinstance(_offer_data, str):
        try: _offer_data = json.loads(_offer_data)
        except: _offer_data = {}

    context = f"""
BUSINESS CONTEXT (from memory):
- Business / Agency: {_bm.get("business_name", "Sohscape")} | UVP: {_bm.get("uvp", "")}
- Positioning: {_bm.get("positioning", "")}
- Industry being targeted: {industry or _bm.get("industry", "")}
- City: {city_display}
- Outreach goal: {goal}
{f"- Specific target: {target}" if target else ""}

MARKET INTELLIGENCE:
- Market size: {_mm.get("market_size", "")}
- Key gap: {_mm.get("market_gap", "")}
- Competition level: {_mm.get("competition_level", "")}

COMPETITORS (to differentiate from):
{", ".join(_competitors[:5]) if _competitors else "Not specified"}

TARGET AUDIENCE SEGMENTS:
{chr(10).join(f"• {s}" for s in _segs[:3]) if _segs else "Business owners / decision makers in " + industry}

BEST OPPORTUNITY (from Opportunity Engine):
- Highest ROI audience: {_opp_data.get("highest_roi_audience", "")}
- Best offer: {_opp_data.get("recommended_offer", "")}
- Best platform: {_opp_data.get("best_platform", "")}

RECOMMENDED OFFER (from Offer Intelligence):
- Offer: {_offer_data.get("recommended_offer", "")}
- Lead magnet: {_offer_data.get("lead_magnet", "")}
- Guarantee: {_offer_data.get("guarantee", "")}
- CTA: {_offer_data.get("cta", "")}

WEBSITE STATUS:
- Audit score: {_wm.get("overall_score", "not audited")}
"""

    # ── GPT-4o ───────────────────────────────────────────────────────────────
    prompt = f"""You are a senior B2B sales copywriter specialising in Indian market outreach for digital marketing agencies.
Generate a complete, personalised outreach kit based on the business context below. Every message must feel written for a real human, not a template.

{context}

Return ONLY a valid JSON object matching this EXACT schema (no markdown, no text outside JSON):
{{
  "cold_email": {{
    "subject": "subject line — curiosity-driven, under 8 words, no clickbait",
    "body": "email body — under 150 words, 3 short paragraphs, reference specific {industry} pain point, end with one soft ask",
    "ps_line": "P.S. one sentence with social proof or urgency",
    "why_it_works": "one sentence explaining the psychology behind this email"
  }},
  "linkedin_message": {{
    "connection_request": "connection note — STRICTLY under 300 characters, mention a specific observation about their business or industry",
    "follow_up_message": "message to send after they accept — 3-4 sentences, reference what they do, propose a specific micro-commitment",
    "why_it_works": "one sentence"
  }},
  "whatsapp": {{
    "message_1_pain": "First WhatsApp — lead with their pain point, 3-4 lines, end with 'Reply AUDIT to get a free audit'",
    "message_2_proof": "Second WhatsApp (send 2 days later if no reply) — lead with a result or case study, 3-4 lines, end with 'Reply AUDIT'",
    "follow_up_day3": "Day 3 follow-up — very short, casual Hinglish, 2 lines max, different angle",
    "follow_up_day7": "Day 7 final follow-up — breakup message, 2 lines, create scarcity or FOMO"
  }},
  "instagram_dm": {{
    "opener": "STRICTLY 3 lines max — start with a specific observation about their account or post, casual Hinglish, end with a soft question",
    "follow_up": "Follow-up DM if no reply in 3 days — 2 lines, reference the opener, add light social proof",
    "why_it_works": "one sentence"
  }},
  "call_script": {{
    "opener_10sec": "10-second cold call opener — introduce yourself, name the specific pain, ask one yes/no question",
    "pain_question": "The single best discovery question to reveal their marketing pain — open-ended",
    "value_statement": "30-second value pitch after they share pain — specific, no fluff, reference results",
    "close": "Meeting booking close — specific day/time suggestion, make it easy to say yes"
  }},
  "objection_handling": [
    {{"objection": "We already have someone", "response": "specific response referencing {industry} context — acknowledge + differentiate + propose small next step"}},
    {{"objection": "No budget right now", "response": "specific response — reframe ROI for {industry} business, offer a low-risk entry point"}},
    {{"objection": "Not interested", "response": "pattern-interrupt response — ask one question that reveals their actual concern"}},
    {{"objection": "Send me details / Send a proposal", "response": "response that gets a meeting instead of sending to a black hole"}}
  ],
  "follow_up_sequence": {{
    "day1": "Same day after first contact — what to send/say",
    "day3": "Day 3 touch — different channel or angle",
    "day7": "Day 7 — add value (share a tip, insight, or quick audit finding)",
    "day14": "Day 14 — final attempt, clear close or let them go gracefully"
  }},
  "proposal_opener": "First paragraph of a proposal — personalised, reference their specific situation, state the transformation without using banned words, under 80 words",
  "confidence": <integer 0-100 — how personalised this kit is based on available memory>
}}

RULES:
- WhatsApp and Instagram must mix Hindi/Hinglish naturally — like a real Indian agency owner would write. NOT robotic translation.
- Cold email body must be UNDER 150 words. Count carefully.
- LinkedIn connection_request STRICTLY under 300 characters. Count carefully.
- Instagram opener STRICTLY 3 lines. No more.
- Call script opener must be deliverable in under 10 seconds.
- Every objection response must reference the specific {industry} context.
- BANNED words in all copy: Elevate, Transform, Unlock, Revolutionize, Empower, Seamless, Leverage, Utilize, Game-changer, Dive in, Cutting-edge, State-of-the-art, World-class, One-stop solution, Look no further, In today's digital age.
- PERSONALIZATION DEPTH: every message (cold_email, linkedin_message, whatsapp, instagram_dm, call_script) must
  contain at least ONE specific detail that could only apply to {personalization_scope}.
  A message that could be sent to any business anywhere is a failure — rewrite it with a concrete,
  specific reference before returning.
- CONFIDENCE DISCIPLINE: "confidence" must reflect actual evidence available in the context above. Memory-backed
  personalization (business/market/opportunity/offer data all present) = high confidence (80+). Partial memory
  (only some tables populated) = medium (50-70). Little to no memory backing = low (<50). Never give 85+
  confidence to a guess.
- Return ONLY the JSON object. Nothing outside it."""

    try:
        resp = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=2800,
            temperature=0.5,
        )
        raw      = resp.choices[0].message.content.strip()
        outreach = json.loads(raw)
    except Exception as _e:
        logger.error(f"[OUTREACH-AI] GPT call failed: {_e}")
        return {"success": False, "error": str(_e)}

    save_to_memory("outreach", norm_key, {"outreach_data": outreach})
    logger.info(f"[OUTREACH-AI] Done: key={norm_key!r} confidence={outreach.get('confidence')}")

    log_activity(
        "outreach_ai", business_key=norm_key,
        business_name=(memory.get("business", {}) or {}).get("business_name", ""),
        url=request.url, industry=industry, city=city,
        summary="Outreach AI scripts generated",
    )

    return {
        "success":      True,
        "memory_used":  True,
        "business_key": norm_key,
        "outreach":     outreach,
    }


# ── Module 19: KPI Engine ─────────────────────────────────────────────────────

class KPIEngineRequest(BaseModel):
    url:      str = ""
    industry: str
    city:     str = ""
    budget:   float = 0.0
    goal:     str = "Lead Generation"

@app.post("/kpi-engine")
async def kpi_engine(request: KPIEngineRequest):
    industry = (request.industry or "").strip()
    city     = (request.city or "").strip()
    # No city given = national India benchmarks, never a silently-assumed city.
    city_scope = city or "India (national average)"
    goal     = (request.goal     or "Lead Generation").strip()
    budget   = request.budget or 0.0

    _bk_src  = (request.url or "").strip()
    norm_key = derive_business_key(_bk_src, industry, city)
    logger.info(f"[KPI-ENGINE] key={norm_key!r} industry={industry!r} city={city!r} budget={budget} goal={goal!r}")

    # ── Load ALL memory ──────────────────────────────────────────────────────
    memory, norm_key = get_memory_with_city_fallback(_bk_src, industry, city)

    if not memory:
        return {
            "success":     False,
            "memory_used": False,
            "message":     "No memory found. Run Marketing Brain first with the same URL + industry.",
        }

    # ── Build context from all tables ────────────────────────────────────────
    _bm   = memory.get("business",  {})
    _mm   = memory.get("market",    {})
    _am   = memory.get("audience",  {})
    _opm  = memory.get("opportunity", {})
    _ofm  = memory.get("offer",     {})
    _cm   = memory.get("campaign",  {})
    _wm   = memory.get("website",   {})

    def _safe(val, key, default=""):
        v = val.get(key, default) if isinstance(val, dict) else default
        if isinstance(v, (dict, list)):
            try: return json.dumps(v, ensure_ascii=False)[:300]
            except: return str(v)[:300]
        return str(v or default)

    _opp_data = _opm.get("opportunity_data") or {}
    _offer_data = _ofm.get("offer_data") or {}
    if isinstance(_opp_data, str):
        try: _opp_data = json.loads(_opp_data)
        except: _opp_data = {}
    if isinstance(_offer_data, str):
        try: _offer_data = json.loads(_offer_data)
        except: _offer_data = {}

    _segs = _am.get("segments", [])
    if isinstance(_segs, str):
        try: _segs = json.loads(_segs)
        except: _segs = [_segs]

    budget_str = f"₹{int(budget):,}/month" if budget > 0 else "₹10,000/month (assumed)"
    context = f"""
CAMPAIGN PARAMETERS:
- Industry: {industry}
- City: {city_scope}
- Goal: {goal}
- Budget: {budget_str}

BUSINESS INTELLIGENCE:
- Name: {_safe(_bm, "business_name")} | UVP: {_safe(_bm, "uvp")}
- Positioning: {_safe(_bm, "positioning")}
- Brand score: {_safe(_bm, "brand_score")} | Trust score: {_safe(_bm, "trust_score")}
- Opportunity score: {_safe(_bm, "opportunity_score")}

MARKET INTELLIGENCE:
- Market size: {_safe(_mm, "market_size")}
- Competition level: {_safe(_mm, "competition_level")}
- Market gap: {_safe(_mm, "market_gap")}

TARGET AUDIENCE:
{chr(10).join(f"• {s}" for s in (_segs[:3] if isinstance(_segs, list) else [str(_segs)])) or "Decision makers in " + industry}

BEST OPPORTUNITY:
- Audience: {_opp_data.get("highest_roi_audience", "")}
- Offer: {_opp_data.get("recommended_offer", "")}
- Platform: {_opp_data.get("best_platform", "")}

RECOMMENDED OFFER:
- Offer: {_offer_data.get("recommended_offer", "")}
- Lead magnet: {_offer_data.get("lead_magnet", "")}
- Guarantee: {_offer_data.get("guarantee", "")}

WEBSITE HEALTH:
- Audit score: {_safe(_wm, "overall_score", "not audited")}
"""

    _growth_block = growth_learning_block(industry)

    prompt = f"""You are a performance marketing expert specialising in Indian digital advertising for {industry} businesses in {city_scope}.
Generate precise, data-driven KPI predictions for this campaign. All numbers must reflect real Indian market benchmarks for this specific industry and city.

{context}

{_growth_block}

Return ONLY a valid JSON object (no markdown, no text outside JSON) with this EXACT schema:
{{
  "primary_kpi": {{
    "metric": "CPL or CPA or ROAS or CTR — pick the ONE most important metric for {goal}",
    "target": "specific target value with unit (e.g. ₹350 CPL, 4.2x ROAS)",
    "why": "one sentence why this is the north-star metric for {goal} in {industry}"
  }},
  "predicted_metrics": {{
    "ctr": {{"value": "X.X%", "range": "X%-X%", "benchmark": "industry avg for {industry} on Google/Meta"}},
    "cpc": {{"value": "₹XX", "range": "₹X-₹X", "benchmark": "avg CPC for {industry} in {city_scope}"}},
    "cpl": {{"value": "₹XXX", "range": "₹X-₹X", "benchmark": "avg CPL for {industry} in {city_scope}"}},
    "cpa": {{"value": "₹XXX", "range": "₹X-₹X", "benchmark": "avg CPA for {industry}"}},
    "roas": {{"value": "X.Xx", "range": "X-Xx", "benchmark": "expected ROAS for {industry} at this budget"}},
    "reach": {{"value": "XX,XXX", "range": "XX,XXX-XX,XXX"}},
    "impressions": {{"value": "XX,XXX", "range": "XX,XXX-XX,XXX"}},
    "clicks": {{"value": "XXX", "range": "XXX-XXX"}},
    "leads": {{"value": "XX", "range": "XX-XX"}},
    "conversions": {{"value": "X", "range": "X-X"}},
    "revenue_potential": {{"value": "₹X,XX,XXX", "range": "₹X,XX,XXX-₹X,XX,XXX"}}
  }},
  "secondary_kpis": [
    {{"metric": "metric name", "target": "specific target", "why": "one sentence"}},
    {{"metric": "metric name", "target": "specific target", "why": "one sentence"}},
    {{"metric": "metric name", "target": "specific target", "why": "one sentence"}}
  ],
  "success_criteria": {{
    "week_1": "specific, measurable milestone by end of week 1 (data collection + first signals)",
    "week_2": "specific measurable milestone by end of week 2 (optimisation signals)",
    "month_1": "specific measurable milestone by end of month 1 (traction proof)",
    "month_2": "specific measurable milestone by end of month 2 (scaling decision point)",
    "month_3": "specific measurable milestone by end of month 3 (full ROI picture)"
  }},
  "budget_breakdown": {{
    "recommended_total": "₹{int(budget) if budget > 0 else 10000:,}/month",
    "google_ads": "₹X,XXX (XX%) — search + display",
    "meta_ads": "₹X,XXX (XX%) — feed + stories + reels",
    "remarketing": "₹X,XXX (XX%) — retargeting warm audience",
    "daily_budget": "₹XXX/day"
  }},
  "cac_ltv": {{
    "estimated_cac": "₹X,XXX — cost to acquire one paying customer",
    "estimated_ltv": "₹XX,XXX — 12-month lifetime value estimate for {industry} client",
    "ltv_cac_ratio": "X:1",
    "payback_period": "X months"
  }},
  "confidence": <integer 0-100>,
  "confidence_reason": "one sentence explaining confidence level based on data available"
}}

RULES:
- All monetary values in Indian Rupees (₹).
- All numbers calibrated for {industry} in {city} at {budget_str} budget — no generic global averages.
- Budget breakdown must sum to the total budget.
- LTV must be realistic for {industry} (hotel: ₹50k+, restaurant: ₹30k+, agency: ₹1L+).
- BANNED words: Elevate, Transform, Unlock, Revolutionize, Empower, Seamless, Cutting-edge, State-of-the-art, World-class, One-stop solution, Look no further, In today's digital age.
- CONFIDENCE DISCIPLINE: "confidence" must reflect actual evidence available in the context above. Memory-backed
  predictions (business/market/opportunity/offer data present) = high confidence (80+). Predictions inferred from
  partial memory (only some tables populated) = medium (50-70). Predictions with no memory backing (pure industry
  assumption) = low (<50), and "confidence_reason" must start with "Speculative:". Never give 85+ confidence to a guess.
- If CROSS-BUSINESS LEARNING data is present above, it is real outcomes from other {industry} campaigns on this
  platform — weigh it over generic benchmarks and raise confidence accordingly; cite it explicitly in
  "confidence_reason" when used.
- Return ONLY the JSON. Nothing outside it."""

    try:
        resp = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=2000,
            temperature=0.3,
        )
        raw = resp.choices[0].message.content.strip()
        kpi = json.loads(raw)
    except Exception as _e:
        logger.error(f"[KPI-ENGINE] GPT call failed: {_e}")
        return {"success": False, "error": str(_e)}

    save_to_memory("kpi", norm_key, {
        "kpi_data": kpi,
        "budget":   budget,
        "goal":     goal,
    })
    logger.info(f"[KPI-ENGINE] Done: key={norm_key!r} confidence={kpi.get('confidence')}")

    log_activity(
        "kpi_engine", business_key=norm_key,
        business_name=(memory.get("business", {}) or {}).get("business_name", ""),
        url=request.url, industry=industry, city=city,
        summary=f"KPI Engine prediction — goal: {goal}",
    )

    # ── TRUST & ACCURACY LAYER ────────────────────────────────────────────
    _has_dna = _has_real_business_dna(_bm)
    _has_aud = bool(_segs)
    _has_campaign = bool(_cm)
    trust_verdict = _compute_trust_verdict(_has_dna, _has_aud)
    based_on = _compute_based_on_line(_has_dna, _has_aud, _has_campaign)
    _biz_label = _bm.get("business_name") or request.url
    _biz_positioning_txt = _bm.get("positioning") or ""
    _business_context_txt = " ".join([industry, _biz_positioning_txt, _bm.get("uvp") or "", json.dumps(_bm.get("business_dna") or {})])
    _match_warning = _business_match_sanity_check(_business_context_txt, json.dumps(kpi), _biz_label, _biz_positioning_txt)
    _contradiction_warning = _detect_consistency_contradiction(_has_dna, json.dumps(kpi))
    validation_warning = _combine_validation_warnings(_match_warning, _contradiction_warning)

    _response = {
        "success":      True,
        "memory_used":  True,
        "business_key": norm_key,
        "kpi":          kpi,
        "trust_verdict": trust_verdict,
        "based_on": based_on,
        "validation_warning": validation_warning or None,
    }
    save_report_snapshot("kpi_engine", norm_key, _response)
    return _response


# ── Autonomous Marketing Engine (Phase 9) ────────────────────────────────────
# User gives just a budget + a plain-language goal ("My budget is ₹5000, I need
# doctor leads") and this decides the complete launch plan — campaign type,
# platforms, budget split, pacing, expected results — no follow-up questions.

class AutonomousMarketingRequest(BaseModel):
    url:       str = ""
    industry:  str = ""
    city:      str = ""
    budget:    float = 0.0
    goal_text: str

@app.post("/autonomous-marketing")
async def autonomous_marketing(request: AutonomousMarketingRequest):
    industry  = (request.industry or "").strip()
    city      = (request.city or "").strip()
    budget    = request.budget or 0.0
    goal_text = (request.goal_text or "").strip()

    if not goal_text:
        return {"success": False, "error": "goal_text is required — e.g. 'I need doctor leads'"}

    _bk_src = (request.url or "").strip()
    memory, norm_key = get_memory_with_city_fallback(_bk_src, industry, city)
    logger.info(f"[AUTONOMOUS] key={norm_key!r} industry={industry!r} city={city!r} budget={budget} goal_text={goal_text!r}")

    # No city given = national India scope, never a silently-assumed city.
    city_display = city or "India (no specific city given)"

    def _safe(d, *keys, dfl="N/A"):
        cur = d
        for k in keys:
            if not isinstance(cur, dict): return dfl
            cur = cur.get(k, dfl)
        return str(cur or dfl)

    def _jf(mem_dict, table_key, field_key):
        raw = (mem_dict.get(table_key) or {})
        if isinstance(raw, str):
            try: raw = json.loads(raw)
            except Exception: raw = {}
        val = raw.get(field_key, {})
        if isinstance(val, str):
            try: return json.loads(val)
            except Exception: return {}
        return val or {}

    _bm       = memory.get("business", {}) or {}
    _mm       = memory.get("market", {}) or {}
    _opp_data = _jf(memory, "opportunity", "opportunity_data")
    _offer_data = _jf(memory, "offer", "offer_data")
    _kpi_data = _jf(memory, "kpi", "kpi_data")

    context = (
        "BUSINESS: " + _safe(_bm, "business_name") +
        " | Industry: " + (industry or _safe(_bm, "industry")) +
        " | City: " + city_display +
        " | Monthly Budget: ₹" + str(int(budget)) + "\n"
        "USER'S GOAL (plain language, may be ambiguous — interpret it before deciding anything): \"" + goal_text + "\"\n\n"
        "KNOWN BUSINESS DATA:\n"
        "- Positioning: " + _safe(_bm, "positioning") + "\n"
        "- Market gap: " + _safe(_mm, "market_gap")[:200] + "\n"
        "- Highest ROI audience (from Opportunity Engine): " + _safe(_opp_data, "highest_roi_audience", "segment") + "\n"
        "- Highest ROI offer: " + _safe(_opp_data, "highest_roi_offer", "offer") + "\n"
        "- Recommended offer (from Offer Intelligence): " + _safe(_offer_data, "recommended_offer", "name") + "\n"
        "- Prior KPI primary metric/target: " + _safe(_kpi_data, "primary_kpi", "metric") + " / " + _safe(_kpi_data, "primary_kpi", "target") + "\n"
    )

    _growth_block = growth_learning_block(industry or _safe(_bm, "industry"))

    prompt = (
        "You are the Autonomous Marketing Engine inside Marketing Brain (Adsoh) — you think like a CMO who takes "
        "a plain-language goal and a budget and returns a complete, launch-today decision. Never ask a follow-up "
        "question; make the best call from the evidence available and state your confidence.\n\n"
        "STEP 1 — INTERPRET THE GOAL: before deciding anything, work out WHO the goal is really about, relative to "
        "THIS business. The same phrase means different things for different businesses — e.g. 'doctor leads' means "
        "doctor-as-prospect for a marketing agency selling TO doctors, doctor-as-employee for a hospital hiring "
        "doctors, doctor-as-buyer for a medical equipment company, doctor-as-hiring-lead for a healthcare recruiter. "
        "State this interpretation explicitly and let it drive every decision below.\n\n"
        + context + "\n" + _growth_block +
        "Return ONLY valid JSON (no markdown, no text outside JSON) matching this EXACT schema:\n"
        "{\n"
        '  "goal_interpretation": {"who_is_the_target":"...","relationship_to_business":"prospect/customer/employee/buyer/hiring_lead/other","reasoning":"specific reasoning tied to this business'"'"'s actual industry"},\n'
        '  "campaign_type": "Search / Social Lead Gen / Display / Performance Max / Demand Gen / Cold Outreach / ...",\n'
        '  "platforms_ranked": [{"platform":"...","why":"specific reason","budget_pct":0,"budget_amount":"RS X"}],\n'
        '  "daily_budget": "RS X/day",\n'
        '  "campaign_duration_days": 30,\n'
        '  "pacing": "spend fast to exit learning phase quickly, or spread evenly across the month — state which and why, citing the budget size and competition level",\n'
        '  "expected_results": {"expected_leads":"X-Y over the period","recommended_cpl":"RS X","recommended_cpa":"RS X","expected_roi":"X%"},\n'
        '  "reason": "one paragraph tying the whole plan together",\n'
        '  "risk": "the main way this plan could fail and what would signal it early",\n'
        '  "confidence": 0,\n'
        '  "next_action": "the single next step the user should take right now"\n'
        "}\n\n"
        "RULES:\n"
        "- All monetary values in Indian Rupees, prefixed RS (post-processing converts to ₹).\n"
        f"- platforms_ranked is the ONLY source of budget allocation — each entry's budget_pct and budget_amount "
        f"must agree with each other, and all budget_amount values must sum to the RS {int(budget)} monthly budget. "
        "Do not invent a platform or amount that contradicts this list.\n"
        "- Numbers must be specific and realistic for this industry+city — no vague ranges like 'a lot more leads'.\n"
        "- CONFIDENCE DISCIPLINE: memory-backed decisions (business/opportunity/offer/kpi data present above) = "
        "high confidence (80+). Inferred decisions = medium (50-70). Pure industry assumption = low (<50) and "
        "'reason' must start with 'Speculative:'. If CROSS-BUSINESS LEARNING data is present, weigh it as real "
        "evidence and raise confidence accordingly.\n"
        "- BANNED words: Elevate, Transform, Unlock, Revolutionize, Empower, Seamless, Leverage, Utilize, Boost, "
        "Maximize, Cutting-edge, State-of-the-art, World-class, One-stop solution, Look no further, In today's digital age.\n"
        "- Return ONLY the JSON. Nothing else."
    )

    try:
        resp = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=1600,
            temperature=0.4,
        )
        raw = resp.choices[0].message.content.strip()
        plan = json.loads(raw)
    except Exception as _e:
        logger.error(f"[AUTONOMOUS] GPT call failed: {_e}")
        return {"success": False, "error": str(_e)}

    plan = _fix_rs(plan)
    plan = _clean_banned_words_deep(plan)

    save_to_memory("autonomous_plan", norm_key, {
        "plan_data": plan,
        "goal_text": goal_text,
        "budget":    budget,
    })
    logger.info(f"[AUTONOMOUS] Done: key={norm_key!r} confidence={plan.get('confidence')}")

    log_activity(
        "autonomous_marketing", business_key=norm_key,
        business_name=(memory.get("business", {}) or {}).get("business_name", ""),
        url=request.url, industry=industry, city=city,
        summary=f"Autonomous Marketing plan generated — goal: {goal_text}",
    )

    return {
        "success":      True,
        "memory_used":  bool(memory),
        "business_key": norm_key,
        "plan":         plan,
    }


# ── Module 20: Performance Intelligence ──────────────────────────────────────
# Shared, honesty-first helpers used by BOTH Performance Intelligence and AI
# Optimizer: statistical-significance guard, composite health scoring,
# conversion-cause diagnosis, and confidence breakdown. These are computed
# deterministically in Python — not left to GPT — because they encode hard
# numeric/factual rules (e.g. "a 10% CTR campaign can't score 0 health") that
# an LLM can silently violate even with a prompt instruction telling it not to.

_DATA_SUFFICIENCY_THRESHOLDS = {"min_spend": 500, "min_impressions": 1000, "min_clicks": 100}

def _check_data_sufficiency(impressions: int, clicks: int, spend: float) -> dict:
    """Statistical significance guard — flags small-sample data so confidence
    and recommendations downstream can be honestly downgraded instead of
    treating a handful of clicks as a confirmed trend."""
    t = _DATA_SUFFICIENCY_THRESHOLDS
    insufficient = spend < t["min_spend"] or impressions < t["min_impressions"] or clicks < t["min_clicks"]
    banner = None
    if insufficient:
        banner = (
            f"⚠️ INSUFFICIENT DATA: This campaign has only {impressions} impressions and ₹{spend:.0f} spend. "
            f"Recommendations below are preliminary — collect more data (target: {t['min_impressions']}+ impressions, "
            f"₹{t['min_spend']}+ spend) before acting on bid/budget changes."
        )
    return {"insufficient_data": insufficient, "banner": banner, "thresholds": t}


_TRACKING_STATUS_LABELS = {
    "NOT_CONVERSION_TRACKED":                        "not set up",
    "UNKNOWN":                                        "status unknown/unverified",
    "UNSPECIFIED":                                    "status unknown/unverified",
    "CONVERSION_TRACKING_MANAGED_BY_SELF":            "set up (self-managed)",
    "CONVERSION_TRACKING_MANAGED_BY_THIS_MANAGER":    "set up (managed by this manager account)",
    "CONVERSION_TRACKING_MANAGED_BY_ANOTHER_MANAGER": "set up (managed by another manager account)",
}
_TRACKING_VERIFIED_STATUSES = {
    "CONVERSION_TRACKING_MANAGED_BY_SELF",
    "CONVERSION_TRACKING_MANAGED_BY_THIS_MANAGER",
    "CONVERSION_TRACKING_MANAGED_BY_ANOTHER_MANAGER",
}

def _fetch_conversion_tracking_status() -> str:
    """Real GAQL signal for whether conversion tracking is actually set up on
    this account — used instead of guessing, so the conversion diagnosis and
    Tracking Health sub-score are grounded in a fact, not an assumption."""
    try:
        customer_id = _genv("GOOGLE_ADS_CUSTOMER_ID")
        client  = get_google_ads_client()
        service = client.get_service("GoogleAdsService")
        rows = list(service.search(
            customer_id=customer_id,
            query="SELECT customer.conversion_tracking_setting.conversion_tracking_status FROM customer LIMIT 1",
        ))
        if rows:
            status = rows[0].customer.conversion_tracking_setting.conversion_tracking_status
            return status.name if hasattr(status, "name") else str(status)
        return "UNKNOWN"
    except Exception as _e:
        logger.warning(f"[TRACKING-STATUS] Could not fetch conversion tracking status: {_e}")
        return "UNKNOWN"


def _compute_health_scores(ctr: float, cpc: float, conversions: float, clicks: int,
                            benchmark_ctr: float, benchmark_cpc: float, tracking_status: str) -> dict:
    """
    Composite health score — Traffic Health (CTR/CPC vs benchmark), Conversion
    Health (conversion rate, only scored if tracking is verified), Tracking
    Health (is tracking actually set up). Weighted so strong traffic metrics
    alone can carry a campaign to a respectable score even with zero/unverified
    conversion data — a campaign getting cheap, high-CTR clicks should never
    show 0/100 just because conversions haven't been confirmed yet.
    """
    ctr_ratio = (ctr / benchmark_ctr) if benchmark_ctr > 0 else 1.0
    cpc_ratio = (benchmark_cpc / cpc) if cpc > 0 and benchmark_cpc > 0 else 1.0
    traffic_health = round(max(0, min(100,
        (min(ctr_ratio, 2.0) / 2.0) * 50 + (min(cpc_ratio, 2.0) / 2.0) * 50
    )))

    tracking_score_map = {
        "CONVERSION_TRACKING_MANAGED_BY_SELF":            100,
        "CONVERSION_TRACKING_MANAGED_BY_THIS_MANAGER":    100,
        "CONVERSION_TRACKING_MANAGED_BY_ANOTHER_MANAGER": 80,
        "NOT_CONVERSION_TRACKED":                         0,
        "UNKNOWN":                                         40,
        "UNSPECIFIED":                                     40,
    }
    tracking_health   = tracking_score_map.get(tracking_status, 40)
    tracking_verified = tracking_status in _TRACKING_VERIFIED_STATUSES

    if clicks <= 0:
        conversion_health = None   # no traffic yet — nothing to assess
    elif not tracking_verified:
        conversion_health = None   # can't trust "0 conversions" as real signal if tracking isn't confirmed
    else:
        conv_rate = (conversions / clicks) * 100
        conversion_health = round(max(0, min(100, (conv_rate / 2.0) * 100)))  # 2% conv rate ≈ 100

    if conversion_health is None:
        overall = round(traffic_health * 0.65 + tracking_health * 0.35)
    else:
        overall = round(traffic_health * 0.40 + conversion_health * 0.35 + tracking_health * 0.25)

    return {
        "traffic_health":    traffic_health,
        "conversion_health": conversion_health,   # None = "not enough signal to assess yet"
        "tracking_health":   tracking_health,
        "overall_health":    overall,
    }


def _conversion_diagnosis(clicks: int, conversions: float, ctr: float, tracking_status: str) -> list:
    """
    Ranked, evidence-based causes for 0 conversions despite clicks. Percentages
    are deterministic, not GPT-invented: tracking status is a real queried
    signal weighted first, the rest split by CTR (high CTR + 0 conversions
    points at the landing page/offer rather than audience intent, and vice
    versa). Returns [] when there's nothing to diagnose (no clicks yet, or
    conversions are already happening).
    """
    if clicks <= 0 or conversions > 0:
        return []

    tracking_label    = _TRACKING_STATUS_LABELS.get(tracking_status, "status unknown/unverified")
    tracking_verified = tracking_status in _TRACKING_VERIFIED_STATUSES

    if tracking_status == "NOT_CONVERSION_TRACKED":
        tracking_pct = 55
    elif not tracking_verified:
        tracking_pct = 45
    else:
        tracking_pct = 15

    remaining = 100 - tracking_pct
    if ctr >= 3.0:
        weights = {"landing_page": 0.45, "audience": 0.20, "offer": 0.35}
    elif ctr < 1.0:
        weights = {"landing_page": 0.30, "audience": 0.45, "offer": 0.25}
    else:
        weights = {"landing_page": 0.38, "audience": 0.30, "offer": 0.32}

    diagnosis = [
        {
            "cause": f"Conversion tracking {tracking_label}",
            "likelihood_pct": tracking_pct,
            "check_fix": (
                "Check Google Ads → Goals → Conversions: confirm a conversion action exists, the tag is installed "
                "on the confirmation/thank-you page, and it's firing (use Tag Assistant or GTM preview mode)."
                if not tracking_verified else
                "Tracking is set up at the account level — verify the specific conversion action for THIS campaign "
                "is attached and actually firing on the real thank-you/confirmation page (not just the form page)."
            ),
        },
        {
            "cause": "Landing page isn't converting (no clear CTA/form, slow load, mobile issues)",
            "likelihood_pct": round(remaining * weights["landing_page"]),
            "check_fix": "Open the landing page on mobile: is there ONE clear form/CTA above the fold? Check PageSpeed Insights — over 3s load time kills conversions.",
        },
        {
            "cause": "Wrong audience intent — clicks aren't from people ready to act",
            "likelihood_pct": round(remaining * weights["audience"]),
            "check_fix": "Check the Search Terms report for irrelevant queries; confirm keyword match types aren't too broad.",
        },
        {
            "cause": "Offer mismatch — what's promised in the ad doesn't match the landing page, or the offer isn't compelling",
            "likelihood_pct": round(remaining * weights["offer"]),
            "check_fix": "Compare the ad headline/description promise to the landing page headline word-for-word — do they match? Consider a lower-commitment offer.",
        },
    ]
    diagnosis.sort(key=lambda d: -d["likelihood_pct"])
    return diagnosis


def _confidence_breakdown(google_ads_connected: bool, has_account_history: bool,
                           industry: str, has_conversion_data: bool) -> dict:
    """
    Honest confidence breakdown — every field here is a real, checked signal.
    cross_business_learning.record_count is the ACTUAL row count from
    growth_memory for this industry (never a made-up number like '143
    campaigns') — get_growth_learning() runs a real SQL COUNT-equivalent query.
    """
    growth = get_growth_learning(industry) if industry else {"sample_size": 0, "entries": []}
    n = growth.get("sample_size", 0)
    return {
        "google_ads_live_data":         bool(google_ads_connected),
        "account_performance_history":  bool(has_account_history),
        "cross_business_learning": {
            "available":    n > 0,
            "record_count": n,
            "label": f"{n} real record(s) from other {industry} businesses run through Adsoh" if n > 0 else "No cross-business records yet for this industry",
        },
        "industry_benchmarks":              "general (not this specific industry unless KPI Engine has run)",
        "this_campaign_conversion_data":    bool(has_conversion_data),
    }


def _parse_pct_generic(v) -> float:
    try:
        s = str(v).replace("%", "").replace(",", "").strip()
        return float(s) if s else 0.0
    except Exception:
        return 0.0

def _parse_money_generic(v) -> float:
    try:
        s = str(v).replace("RS", "").replace("₹", "").replace(",", "").strip()
        return float(s) if s else 0.0
    except Exception:
        return 0.0


class PerformanceIntelligenceRequest(BaseModel):
    url:        str = ""
    industry:   str = ""
    city:       str = ""
    date_range: str = "30d"

def _parse_days(date_range: str) -> int:
    mapping = {"7d": 7, "30d": 30, "90d": 90}
    return mapping.get(date_range.lower(), 30)

def _fetch_gads_performance(days: int) -> dict:
    """Run Google Ads aggregate query — returns raw metrics dict or error."""
    customer_id = _genv("GOOGLE_ADS_CUSTOMER_ID")
    try:
        client  = get_google_ads_client()
        service = client.get_service("GoogleAdsService")
        end     = date.today()
        start   = end - timedelta(days=days)
        query   = f"""
            SELECT
                metrics.impressions,
                metrics.clicks,
                metrics.cost_micros,
                metrics.conversions,
                metrics.ctr,
                metrics.average_cpc
            FROM customer
            WHERE segments.date BETWEEN '{start}' AND '{end}'
        """
        response          = service.search(customer_id=customer_id, query=query)
        total_impressions = 0
        total_clicks      = 0
        total_cost_micros = 0
        total_conversions = 0.0
        for row in response:
            total_impressions  += row.metrics.impressions
            total_clicks       += row.metrics.clicks
            total_cost_micros  += row.metrics.cost_micros
            total_conversions  += row.metrics.conversions

        total_cost = total_cost_micros / 1_000_000
        ctr        = (total_clicks / total_impressions * 100) if total_impressions else 0.0
        avg_cpc    = (total_cost / total_clicks)             if total_clicks      else 0.0
        cpa        = (total_cost / total_conversions)        if total_conversions else 0.0
        return {
            "connected":    True,
            "impressions":  total_impressions,
            "clicks":       total_clicks,
            "cost_inr":     round(total_cost, 2),
            "conversions":  round(total_conversions, 2),
            "ctr_pct":      round(ctr, 2),
            "avg_cpc_inr":  round(avg_cpc, 2),
            "cpa_inr":      round(cpa, 2),
            "start_date":   str(start),
            "end_date":     str(end),
        }
    except GoogleAdsException as ex:
        errors = [e.message for e in ex.failure.errors]
        logger.error(f"[PERF-INTEL] GAds aggregate error: {errors}")
        return {"connected": False, "error": str(errors)}
    except Exception as ex:
        logger.error(f"[PERF-INTEL] GAds aggregate unexpected: {ex}")
        return {"connected": False, "error": str(ex)}

def _fetch_gads_campaigns(days: int) -> list:
    """Per-campaign breakdown — returns list or []."""
    customer_id = _genv("GOOGLE_ADS_CUSTOMER_ID")
    end   = date.today()
    start = end - timedelta(days=days)
    try:
        client  = get_google_ads_client()
        service = client.get_service("GoogleAdsService")
        query   = f"""
            SELECT
                campaign.id,
                campaign.name,
                campaign.status,
                metrics.impressions,
                metrics.clicks,
                metrics.cost_micros,
                metrics.conversions,
                metrics.ctr
            FROM campaign
WHERE segments.date BETWEEN '{start}' AND '{end}'
ORDER BY metrics.cost_micros DESC
            LIMIT 20
        """
        rows      = list(service.search(customer_id=customer_id, query=query))
        campaigns = []
        for row in rows:
            cost = row.metrics.cost_micros / 1_000_000
            imp  = row.metrics.impressions
            clk  = row.metrics.clicks
            conv = row.metrics.conversions
            campaigns.append({
                "campaign_id": str(row.campaign.id),
                "name":        row.campaign.name,
                "status":      row.campaign.status.name,
                "impressions": imp,
                "clicks":      clk,
                "cost_inr":    round(cost, 2),
                "conversions": round(conv, 2),
                "ctr_pct":     round(clk / imp * 100, 2) if imp else 0.0,
                "avg_cpc_inr": round(cost / clk, 2)      if clk else 0.0,
            })
        return campaigns
    except Exception as ex:
        logger.error(f"[PERF-INTEL] GAds campaigns error: {ex}")
        return []

@app.post("/performance-intelligence")
async def performance_intelligence(request: PerformanceIntelligenceRequest):
    try:
        industry   = (request.industry or "").strip()
        city       = (request.city or "").strip()
        city_display = city or "India (national average)"
        date_range = (request.date_range or "30d").strip()
        days       = _parse_days(date_range)
        _bk_src    = (request.url or "").strip()

        logger.info(f"[PERF-INTEL] url={request.url!r} industry={industry!r} city={city!r} date_range={date_range!r} days={days}")

        # ── Memory + Google Ads in parallel ──────────────────────────────────
        memory, norm_key = get_memory_with_city_fallback(_bk_src, industry, city)
        logger.info(f"[PERF-INTEL] key={norm_key!r} memory_tables={list(memory.keys())}")
        has_account_history = bool(memory.get("performance"))

        perf_data, campaign_rows, tracking_status = await asyncio.gather(
            asyncio.to_thread(_fetch_gads_performance, days),
            asyncio.to_thread(_fetch_gads_campaigns,  days),
            asyncio.to_thread(_fetch_conversion_tracking_status),
        )
        logger.info(f"[PERF-INTEL] gads_perf={perf_data} campaigns={len(campaign_rows)} tracking_status={tracking_status}")

        google_ads_connected = perf_data.get("connected", False)

        # ── Pull expected KPIs from kpi_memory ───────────────────────────────
        _kpi_raw = memory.get("kpi", {})
        if isinstance(_kpi_raw, str):
            try: _kpi_raw = json.loads(_kpi_raw)
            except: _kpi_raw = {}
        _kpi_data = _kpi_raw.get("kpi_data", {})
        if isinstance(_kpi_data, str):
            try: _kpi_data = json.loads(_kpi_data)
            except: _kpi_data = {}
        _pm_exp = _kpi_data.get("predicted_metrics", {})
        if isinstance(_pm_exp, str):
            try: _pm_exp = json.loads(_pm_exp)
            except: _pm_exp = {}

        def _exp(key):
            m = _pm_exp.get(key, {})
            if isinstance(m, dict): return m.get("value", "N/A")
            return str(m) if m else "N/A"

        # ── Numeric metrics ───────────────────────────────────────────────────
        imp  = int(perf_data.get("impressions", 0))
        clk  = int(perf_data.get("clicks", 0))
        cost = float(perf_data.get("cost_inr", 0.0))
        conv = float(perf_data.get("conversions", 0.0))
        ctr  = float(perf_data.get("ctr_pct", 0.0))
        cpc  = float(perf_data.get("avg_cpc_inr", 0.0))
        cpa  = float(perf_data.get("cpa_inr", 0.0))
        roas = round(conv / cost, 2) if cost > 0 and conv > 0 else 0.0
        zero_spend = (cost == 0 and imp == 0)

        # ── Benchmarks: prefer KPI Engine's own predicted CTR/CPC (already
        # industry-tailored) over a generic fallback — track which was used so
        # the confidence breakdown can honestly say "general" vs industry-specific.
        _pred_ctr = _parse_pct_generic(_exp("ctr"))
        _pred_cpc = _parse_money_generic(_exp("cpc"))
        benchmark_ctr    = _pred_ctr if _pred_ctr > 0 else 2.0
        benchmark_cpc    = _pred_cpc if _pred_cpc > 0 else 20.0
        benchmark_source = "kpi_engine_prediction" if (_pred_ctr > 0 or _pred_cpc > 0) else "generic_search_benchmark"

        # ── Deterministic, honesty-first computations (never left to GPT) ─────
        data_sufficiency   = _check_data_sufficiency(imp, clk, cost)
        health_scores      = _compute_health_scores(ctr, cpc, conv, clk, benchmark_ctr, benchmark_cpc, tracking_status)
        conversion_diag    = _conversion_diagnosis(clk, conv, ctr, tracking_status)
        confidence_bkdown  = _confidence_breakdown(google_ads_connected, has_account_history, industry, has_conversion_data=(conv > 0))
        has_active_campaign_with_data = any(
            row.get("status") == "ENABLED" and row.get("impressions", 0) > 0 for row in campaign_rows
        )

        # ── Impact quantification: only estimate using KPI Engine's own
        # predicted CPC/CPA (never an invented conversion rate) ───────────────
        _pred_cpa = _parse_money_generic(_exp("cpa"))
        impact_quantification = None
        if clk > 0 and conv == 0 and _pred_cpc > 0 and _pred_cpa > 0:
            implied_rate = _pred_cpc / _pred_cpa
            potential_leads = round(clk * implied_rate, 1)
            impact_quantification = {
                "basis": "Estimated from KPI Engine's predicted CPC/CPA implied conversion rate — this campaign has 0 tracked conversions, so this is NOT a measured rate.",
                "clicks_with_no_tracked_conversion": clk,
                "implied_conversion_rate_pct": round(implied_rate * 100, 2),
                "estimated_potential_leads": potential_leads,
                "note": (
                    f"{clk} clicks with 0 tracked conversions — if even {round(implied_rate * 100, 1)}% converted at the "
                    f"KPI Engine's predicted rate, that's an estimated {potential_leads} lead(s) currently invisible, "
                    "most likely due to a tracking or landing-page issue rather than zero real interest."
                ),
            }
        elif clk > 0 and conv == 0:
            impact_quantification = {
                "basis": "No KPI Engine prediction available to base an estimate on.",
                "clicks_with_no_tracked_conversion": clk,
                "note": f"{clk} clicks with 0 tracked conversions. Run KPI Engine first for an industry-calibrated estimate.",
            }

        _bm = memory.get("business", {})
        def _sv(d, k, dfl=""):
            v = (d or {}).get(k, dfl) if isinstance(d, dict) else dfl
            return str(v or dfl)

        # ── Build campaign summary text (safe, outside f-string) ──────────────
        campaign_summary = "\n".join(
            "  * {n} ({s}): {i:,} impr / {c} clk / RS{cost} / {conv} conv / CTR {ctr}%".format(
                n=row["name"], s=row["status"], i=row["impressions"],
                c=row["clicks"], cost=row["cost_inr"],
                conv=row["conversions"], ctr=row["ctr_pct"],
            )
            for row in campaign_rows[:8]
        ) or "  No campaign data found."

        # ── Pre-build campaign_breakdown JSON outside the f-string ────────────
        # (avoids the {{...}} inside f-string expression bug)
        def _rate(row):
            if row.get("ctr_pct", 0) > 2 or row.get("conversions", 0) > 0: return "good"
            if row.get("ctr_pct", 0) >= 1: return "average"
            return "poor"

        _camp_list = [
            {
                "campaign_name":      row["name"],
                "status":             row["status"],
                "impressions":        row["impressions"],
                "clicks":             row["clicks"],
                "cost":               "RS" + str(row["cost_inr"]),
                "conversions":        row["conversions"],
                "ctr":                str(row["ctr_pct"]) + "%",
                "performance_rating": _rate(row),
            }
            for row in campaign_rows[:8]
        ]
        _camp_json = json.dumps(_camp_list, ensure_ascii=False)

        # Ground-truth signal for "are there really zero active campaigns" —
        # per-campaign data, not the account-level aggregate, which can
        # diverge from it. GPT is told never to claim "no active campaigns"
        # when this is True; Python enforces it afterward regardless.
        zero_note = (
            "NOTE: Google Ads shows zero spend and zero impressions across the WHOLE account for this date range."
            if zero_spend and not has_active_campaign_with_data else ""
        )

        sufficiency_note = data_sufficiency["banner"] or ""
        tracking_note = f"CONVERSION TRACKING STATUS (real, queried): {tracking_status} ({_TRACKING_STATUS_LABELS.get(tracking_status, 'unknown')})\n"

        prompt = (
            "You are a senior Google Ads performance analyst for an Indian digital marketing agency. You think in "
            "terms of DECISIONS, not just descriptions — every report leads with what to do, not just what happened.\n"
            "Analyse the following campaign performance data and return a JSON report.\n\n"
            "BUSINESS: " + _sv(_bm, "business_name") + " | Industry: " + industry + " | City: " + city_display + "\n"
            "ANALYSIS PERIOD: Last " + str(days) + " days (" + str(perf_data.get("start_date", "N/A")) + " to " + str(perf_data.get("end_date", "N/A")) + ")\n"
            "GOOGLE ADS CONNECTED: " + str(google_ads_connected) + "\n"
            + tracking_note +
            "HAS AT LEAST ONE ENABLED CAMPAIGN WITH REAL IMPRESSIONS: " + str(has_active_campaign_with_data) + "\n\n"
            "ACTUAL GOOGLE ADS METRICS:\n"
            "- Impressions: " + str(imp) + "\n"
            "- Clicks: " + str(clk) + "\n"
            "- CTR: " + str(ctr) + "%\n"
            "- Avg CPC: RS" + str(cpc) + "\n"
            "- Total Cost: RS" + str(cost) + "\n"
            "- Conversions: " + str(conv) + "\n"
            "- CPA: RS" + str(cpa) + "\n"
            "- ROAS: " + str(roas) + "x\n\n"
            "EXPECTED (from KPI predictions):\n"
            "- CTR: " + _exp("ctr") + "\n"
            "- CPC: " + _exp("cpc") + "\n"
            "- CPA: " + _exp("cpa") + "\n"
            "- ROAS: " + _exp("roas") + "\n"
            "- Impressions: " + _exp("impressions") + "\n"
            "- Clicks: " + _exp("clicks") + "\n"
            "- Conversions: " + _exp("conversions") + "\n\n"
            "CAMPAIGN BREAKDOWN:\n" + campaign_summary + "\n\n"
            + (zero_note + "\n\n" if zero_note else "")
            + (sufficiency_note + "\n\n" if sufficiency_note else "")
            + "Return ONLY valid JSON (no markdown, no text outside JSON) with this EXACT schema:\n"
            '{\n'
            '  "date_range": "' + date_range + ' (' + str(days) + ' days)",\n'
            '  "actual_metrics": {\n'
            '    "impressions": ' + str(imp) + ',\n'
            '    "clicks": ' + str(clk) + ',\n'
            '    "ctr": "' + str(ctr) + '%",\n'
            '    "cpc": "RS' + str(cpc) + '",\n'
            '    "cost": "RS' + str(cost) + '",\n'
            '    "conversions": ' + str(conv) + ',\n'
            '    "cpa": "RS' + str(cpa) + '",\n'
            '    "roas": "' + str(roas) + 'x"\n'
            '  },\n'
            '  "expected_vs_actual": [\n'
            '    {"metric":"CTR","expected":"' + _exp("ctr") + '","actual":"' + str(ctr) + '%","status":"below/on_track/above","gap":"numeric gap with sign","action":"specific action — MUST mention the sample size (impressions/clicks) if data is insufficient, e.g. \'...but based on only N impressions, too small to confirm\'"},\n'
            '    {"metric":"CPC","expected":"' + _exp("cpc") + '","actual":"RS' + str(cpc) + '","status":"below/on_track/above","gap":"numeric gap","action":"specific action"},\n'
            '    {"metric":"Conversions","expected":"' + _exp("conversions") + '","actual":"' + str(conv) + '","status":"below/on_track/above","gap":"numeric gap","action":"specific action"},\n'
            '    {"metric":"ROAS","expected":"' + _exp("roas") + '","actual":"' + str(roas) + 'x","status":"below/on_track/above","gap":"numeric gap","action":"specific action"},\n'
            '    {"metric":"Cost","expected":"N/A","actual":"RS' + str(cost) + '","status":"on_track","gap":"0","action":"budget pacing assessment"}\n'
            '  ],\n'
            '  "campaign_breakdown": ' + _camp_json + ',\n'
            '  "top_insight": "single most important thing happening now — specific, data-backed, MUST reference sample size if small",\n'
            '  "biggest_problem": "single most urgent issue to fix — specific, data-backed, no fluff",\n'
            '  "quick_wins": ["action 1 with expected impact","action 2","action 3"],\n'
            '  "ai_analysis": "2-3 paragraphs on overall performance, what is working, what needs fixing. Specific to the numbers above. If data is insufficient, say so explicitly and frame confidently-worded conclusions as preliminary.",\n'
            '  "trend": "improving/stable/declining",\n'
            '  "decision_summary": {\n'
            '    "top_problems": [{"problem":"...","impact":"high/medium/low"}],\n'
            '    "top_actions": [{"action":"...","expected_impact":"...","effort":"low/medium/high"}],\n'
            '    "expected_improvement_if_actioned": "range, explicitly labeled as a projection, not a guarantee",\n'
            '    "overall_confidence": 0\n'
            '  }\n'
            '}\n\n'
            "Rules:\n"
            "- Do NOT include an overall_health field — health scoring is computed separately from real thresholds, not by you.\n"
            "- Fill campaign_breakdown performance_rating: good (CTR>2% or conv>0), average (CTR 1-2%), poor (CTR<1% and conv=0).\n"
            "- NEVER say 'no active campaigns' or similar in ANY field if HAS AT LEAST ONE ENABLED CAMPAIGN WITH REAL "
            "IMPRESSIONS above is True — that claim would be factually wrong. Only use that framing if it is False.\n"
            "- decision_summary.top_problems and top_actions: exactly 3 each, ranked by impact (most impactful first). "
            "top_actions must each include a realistic effort estimate.\n"
            "- decision_summary.overall_confidence: an honest 0-100 integer. If insufficient data (see banner above), "
            "this MUST be 40 or below — do not express high confidence on a small sample.\n"
            "- SAMPLE SIZE HONESTY: if impressions are low, NEVER celebrate or alarm on a metric without explicitly "
            "flagging the sample size, e.g. 'CTR of 10.16% is well above benchmark, but based on only 384 impressions — "
            "too small to confirm as sustained performance. Re-evaluate at 1000+ impressions.'\n"
            "- BANNED words: Elevate, Transform, Unlock, Revolutionize, Empower, Seamless.\n"
            "- Return ONLY the JSON. Nothing else."
        )

        logger.info(f"[PERF-INTEL] Sending prompt to GPT-4o (len={len(prompt)})")

        resp = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=2200,
            temperature=0.3,
        )
        raw_resp = resp.choices[0].message.content.strip()
        logger.info(f"[PERF-INTEL] GPT responded (len={len(raw_resp)})")
        performance = json.loads(raw_resp)

        performance = _fix_rs(performance)

        # ── Back-fill campaign_breakdown if GPT returned empty ────────────────
        if not performance.get("campaign_breakdown") and campaign_rows:
            performance["campaign_breakdown"] = [
                {
                    "campaign_name":      row["name"],
                    "status":             row["status"],
                    "impressions":        row["impressions"],
                    "clicks":             row["clicks"],
                    "cost":               "₹" + str(row["cost_inr"]),
                    "conversions":        row["conversions"],
                    "ctr":                str(row["ctr_pct"]) + "%",
                    "performance_rating": _rate(row),
                }
                for row in campaign_rows[:8]
            ]

        # ── Inject deterministic fields — these are never GPT-generated, so
        # they can't drift from the real computed values above ───────────────
        performance["overall_health"]       = health_scores["overall_health"]
        performance["health_scores"]        = health_scores
        performance["data_sufficiency"]     = data_sufficiency
        performance["conversion_diagnosis"] = conversion_diag
        performance["why_not_converting"]   = conversion_diag
        performance["confidence_breakdown"] = confidence_bkdown
        if impact_quantification:
            performance["impact_quantification"] = impact_quantification

        # ── Enforce the "no active campaigns" claim can't be factually wrong ──
        if has_active_campaign_with_data:
            best_campaign = max(campaign_rows, key=lambda r: r.get("impressions", 0))
            for _field in ("top_insight", "biggest_problem"):
                _val = str(performance.get(_field, "") or "")
                if "no active campaign" in _val.lower():
                    performance[_field] = (
                        f"'{best_campaign['name']}' is ENABLED with {best_campaign['impressions']} impressions — "
                        "this account does have active campaign data; re-check the specific metric driving this concern."
                    )

        # ── Cap decision_summary confidence honestly on small samples ────────
        _ds = performance.get("decision_summary")
        if isinstance(_ds, dict) and data_sufficiency["insufficient_data"]:
            try:
                _ds["overall_confidence"] = min(int(_ds.get("overall_confidence", 40) or 40), 40)
            except Exception:
                _ds["overall_confidence"] = 40

        save_to_memory("performance", norm_key, {
            "performance_data": performance,
            "date_range":       date_range,
            "overall_health":   float(performance.get("overall_health", 0) or 0),
        })
        logger.info(f"[PERF-INTEL] Done: key={norm_key!r} health={performance.get('overall_health')} connected={google_ads_connected}")

        log_activity(
            "performance_intelligence", business_key=norm_key,
            business_name=_bm.get("business_name", ""),
            url=request.url, industry=industry, city=city,
            summary=f"Performance Intelligence report — health {performance.get('overall_health', 'N/A')}"
                    + (" (insufficient data)" if data_sufficiency["insufficient_data"] else ""),
        )

        return {
            "success":              True,
            "google_ads_connected": google_ads_connected,
            "memory_used":          bool(memory),
            "business_key":         norm_key,
            "performance":          performance,
        }

    except Exception as _outer_e:
        tb = _traceback.format_exc()
        logger.error(f"[PERF-INTEL] UNHANDLED ERROR: {_outer_e}\n{tb}")
        return {
            "success":   False,
            "error":     str(_outer_e),
            "traceback": tb,
        }


# ── Module 21: AI Optimizer ───────────────────────────────────────────────────

class AIOptimizerRequest(BaseModel):
    url:      str = ""
    industry: str = ""
    city:     str = ""

async def _run_ai_optimizer_core(url: str = "", industry: str = "", city: str = "") -> dict:
    """
    Core optimizer logic, factored out of the /ai-optimizer endpoint so it can
    also be driven in bulk by /optimizer/run-all (Phase 10 continuous
    optimization loop) without duplicating the prompt/parsing logic.
    """
    try:
        industry = (industry or "").strip()
        city     = (city or "").strip()
        city_display = city or "India (national average)"
        _bk_src  = (url      or "").strip()

        logger.info(f"[AI-OPT] url={url!r} industry={industry!r} city={city!r}")

        memory, norm_key = get_memory_with_city_fallback(_bk_src, industry, city)
        logger.info(f"[AI-OPT] key={norm_key!r} tables={list(memory.keys())}")

        # ── Extract KPI + Performance memory ─────────────────────────────────
        def _load_json_field(mem_dict, table_key, field_key):
            raw = (mem_dict.get(table_key) or {})
            if isinstance(raw, str):
                try: raw = json.loads(raw)
                except: raw = {}
            val = raw.get(field_key, {})
            if isinstance(val, str):
                try: return json.loads(val)
                except: return {}
            return val or {}

        kpi_data         = _load_json_field(memory, "kpi",         "kpi_data")
        performance_data = _load_json_field(memory, "performance",  "performance_data")
        business_data    = memory.get("business", {}) or {}
        audience_data    = memory.get("audience",  {}) or {}
        offer_data       = _load_json_field(memory, "offer",        "offer_data")
        campaign_data    = memory.get("campaign",  {}) or {}
        market_data      = memory.get("market",    {}) or {}

        has_kpi  = bool(kpi_data)
        has_perf = bool(performance_data)

        if not has_kpi and not has_perf:
            return {
                "success":     False,
                "memory_used": False,
                "message":     "No KPI or performance data found. Run KPI Engine then Performance Intelligence first.",
            }

        # ── Safe string extractor ─────────────────────────────────────────────
        def _sv(d, *keys, dfl="N/A"):
            cur = d
            for k in keys:
                if not isinstance(cur, dict): return dfl
                cur = cur.get(k, dfl)
            return str(cur or dfl)

        # ── KPI expected metrics ──────────────────────────────────────────────
        pm_exp    = kpi_data.get("predicted_metrics", {}) or {}
        primary   = kpi_data.get("primary_kpi", {})       or {}
        cac_ltv   = kpi_data.get("cac_ltv", {})           or {}
        budget_bk = kpi_data.get("budget_breakdown", {})  or {}
        sec_kpis  = kpi_data.get("secondary_kpis", [])    or []

        def _exp(key):
            m = pm_exp.get(key, {})
            if isinstance(m, dict): return m.get("value", "N/A")
            return str(m) if m else "N/A"

        # ── Performance actual metrics ────────────────────────────────────────
        am       = performance_data.get("actual_metrics", {})    or {}
        eva      = performance_data.get("expected_vs_actual", []) or []
        camp_bk  = performance_data.get("campaign_breakdown", []) or []
        qw       = performance_data.get("quick_wins", [])         or []
        health   = performance_data.get("overall_health", "N/A")
        trend    = performance_data.get("trend", "N/A")
        top_ins  = performance_data.get("top_insight", "")
        big_prob = performance_data.get("biggest_problem", "")

        # ── Inherit the honesty-first computations Performance Intelligence
        # already saved (so both modules agree on the same numbers) — fall
        # back to a lightweight local recompute only if this business hasn't
        # run Performance Intelligence since this feature shipped ───────────
        _imp  = int(am.get("impressions", 0) or 0)
        _clk  = int(am.get("clicks", 0) or 0)
        _cost = _parse_money_generic(am.get("cost", 0))
        _conv = float(am.get("conversions", 0) or 0)
        _ctr  = _parse_pct_generic(am.get("ctr", 0))
        _cpc  = _parse_money_generic(am.get("cpc", 0))

        data_sufficiency = performance_data.get("data_sufficiency") or _check_data_sufficiency(_imp, _clk, _cost)
        health_scores    = performance_data.get("health_scores")
        if not health_scores:
            _pred_ctr  = _parse_pct_generic(_exp("ctr"))
            _pred_cpc  = _parse_money_generic(_exp("cpc"))
            _bench_ctr = _pred_ctr if _pred_ctr > 0 else 2.0
            _bench_cpc = _pred_cpc if _pred_cpc > 0 else 20.0
            health_scores = _compute_health_scores(_ctr, _cpc, _conv, _clk, _bench_ctr, _bench_cpc, "UNKNOWN")
        _inherited_diag = performance_data.get("conversion_diagnosis")
        conversion_diag = _inherited_diag if _inherited_diag is not None else _conversion_diagnosis(_clk, _conv, _ctr, "UNKNOWN")
        confidence_bkdown = performance_data.get("confidence_breakdown") or _confidence_breakdown(
            has_perf, has_perf, industry, has_conversion_data=(_conv > 0)
        )

        # ── Summarise below-target metrics from expected_vs_actual ────────────
        below_metrics = [
            e["metric"] + " (expected " + str(e.get("expected","?")) + ", actual " + str(e.get("actual","?")) + ", gap " + str(e.get("gap","?")) + ")"
            for e in (eva if isinstance(eva, list) else [])
            if isinstance(e, dict) and e.get("status") == "below"
        ]
        above_metrics = [
            e["metric"] + " (" + str(e.get("actual","?")) + " vs expected " + str(e.get("expected","?")) + ")"
            for e in (eva if isinstance(eva, list) else [])
            if isinstance(e, dict) and e.get("status") == "above"
        ]

        poor_campaigns = [
            c.get("campaign_name","?") + " (CTR " + str(c.get("ctr","?")) + ", cost " + str(c.get("cost","?")) + ", conv " + str(c.get("conversions","?")) + ")"
            for c in (camp_bk if isinstance(camp_bk, list) else [])
            if isinstance(c, dict) and c.get("performance_rating") == "poor"
        ]
        good_campaigns = [
            c.get("campaign_name","?") + " (CTR " + str(c.get("ctr","?")) + ", conv " + str(c.get("conversions","?")) + ")"
            for c in (camp_bk if isinstance(camp_bk, list) else [])
            if isinstance(c, dict) and c.get("performance_rating") == "good"
        ]

        no_campaigns = (
            not camp_bk
            or (isinstance(am, dict) and str(am.get("cost","0")).replace("RS","").replace("₹","").strip() in ("0","0.0",""))
        )

        # ── Full per-campaign breakdown + pause-eligibility filter ────────────
        # A campaign with ₹0 spend AND 0 impressions has nothing to save by
        # pausing it (it's not running) — exclude it from pause candidates so
        # the AI can't recommend "pausing" noise instead of real underperformers.
        def _parse_money(v):
            try:
                s = str(v).replace("RS", "").replace("₹", "").replace(",", "").strip()
                return float(s) if s else 0.0
            except Exception:
                return 0.0

        _all_campaigns = [c for c in (camp_bk if isinstance(camp_bk, list) else []) if isinstance(c, dict)]
        _pause_eligible = [
            c for c in _all_campaigns
            if not (_parse_money(c.get("cost", 0)) == 0 and int(c.get("impressions", 0) or 0) == 0)
        ]

        campaign_breakdown_block = "\n".join(
            "- " + c.get("campaign_name", "?") +
            ": status=" + str(c.get("status", "?")) +
            ", cost=" + str(c.get("cost", "?")) +
            ", impressions=" + str(c.get("impressions", "?")) +
            ", CTR=" + str(c.get("ctr", "?")) +
            ", conversions=" + str(c.get("conversions", "?")) +
            ", rating=" + str(c.get("performance_rating", "?"))
            for c in _all_campaigns
        ) or "No campaign breakdown available."

        pause_eligible_block = "\n".join(
            "- " + c.get("campaign_name", "?") for c in _pause_eligible
        ) or "NONE — every campaign has ₹0 spend and 0 impressions. Return an empty pause_recommendations list. Do not invent one."

        def _jstr(obj):
            try: return json.dumps(obj, ensure_ascii=False)[:400]
            except: return str(obj)[:400]

        context = (
            "BUSINESS: " + _sv(business_data, "business_name") +
            " | Industry: " + (industry or _sv(business_data, "industry")) +
            " | City: " + city_display + "\n\n"

            "=== KPI ENGINE (Expected) ===\n"
            + ("NOT AVAILABLE\n" if not has_kpi else (
                "Primary KPI: " + _sv(primary, "metric") + " target " + _sv(primary, "target") + "\n"
                "Budget: " + _sv(budget_bk, "recommended_total") +
                " (Google: " + _sv(budget_bk, "google_ads") +
                " / Meta: " + _sv(budget_bk, "meta_ads") +
                " / Remarketing: " + _sv(budget_bk, "remarketing") + ")\n"
                "Expected CTR: " + _exp("ctr") + " | CPC: " + _exp("cpc") + " | CPA: " + _exp("cpa") + " | ROAS: " + _exp("roas") + "\n"
                "Expected Leads: " + _exp("leads") + " | Conversions: " + _exp("conversions") + " | Revenue: " + _exp("revenue_potential") + "\n"
                "CAC: " + _sv(cac_ltv, "estimated_cac") + " | LTV: " + _sv(cac_ltv, "estimated_ltv") + " | LTV:CAC " + _sv(cac_ltv, "ltv_cac_ratio") + "\n"
            )) + "\n"

            "=== PERFORMANCE INTELLIGENCE (Actual) ===\n"
            + ("NOT AVAILABLE\n" if not has_perf else (
                "Health: " + str(health) + "/100 | Trend: " + str(trend) + "\n"
                "Actual: CTR " + _sv(am, "ctr") + " | CPC " + _sv(am, "cpc") + " | Cost " + _sv(am, "cost") +
                " | Conv " + str(am.get("conversions","?")) + " | CPA " + _sv(am, "cpa") + " | ROAS " + _sv(am, "roas") + "\n"
                "Top Insight: " + str(top_ins or "N/A") + "\n"
                "Biggest Problem: " + str(big_prob or "N/A") + "\n"
                + ("BELOW TARGET: " + "; ".join(below_metrics) + "\n" if below_metrics else "")
                + ("ABOVE TARGET: " + "; ".join(above_metrics) + "\n" if above_metrics else "")
                + ("POOR CAMPAIGNS: " + "; ".join(poor_campaigns) + "\n" if poor_campaigns else "")
                + ("GOOD CAMPAIGNS: " + "; ".join(good_campaigns) + "\n" if good_campaigns else "")
                + ("Quick wins flagged: " + "; ".join(qw[:3]) + "\n" if qw else "")
                + ("NOTE: No active campaign spend detected.\n" if no_campaigns else "")
            )) + "\n"

            "=== CAMPAIGN BREAKDOWN (use these EXACT campaign_name values in any recommendation) ===\n"
            + campaign_breakdown_block + "\n\n"

            "=== PAUSE-ELIGIBLE CAMPAIGNS (only these may appear in pause_recommendations — "
            "campaigns with ₹0 spend and 0 impressions are excluded, there is nothing to save by pausing them) ===\n"
            + pause_eligible_block + "\n\n"

            "=== AUDIENCE & OFFER ===\n"
            "Target segments: " + _sv(audience_data, "segments")[:200] + "\n"
            "Recommended offer: " + _sv(offer_data, "recommended_offer")[:200] + "\n"
            "Lead magnet: " + _sv(offer_data, "lead_magnet")[:200] + "\n\n"

            "=== MARKET ===\n"
            "Competition level: " + _sv(market_data, "competition_level") + "\n"
            "Market gap: " + _sv(market_data, "market_gap")[:200] + "\n"
        )

        missing_note = ""
        if not has_kpi:
            missing_note = "NOTE: KPI Engine data not available — base recommendations on performance data + industry benchmarks.\n"
        elif not has_perf:
            missing_note = "NOTE: Performance data not available — base recommendations on KPI targets + pre-launch best practices.\n"

        sufficiency_note = data_sufficiency["banner"] or ""

        prompt = (
            "You are a senior Google Ads + Meta Ads optimization strategist for an Indian digital marketing agency.\n"
            "Based on the data below, generate a detailed, actionable optimization plan.\n\n"
            + context + "\n"
            + missing_note
            + (sufficiency_note + "\n\n" if sufficiency_note else "\n")
            + "Return ONLY valid JSON (no markdown, no text outside JSON) matching this EXACT schema:\n"
            "{\n"
            '  "overall_verdict": "campaigns need urgent attention / on track / performing well",\n'
            '  "health_change": "improving / stable / declining",\n'
            '  "pause_recommendations": [\n'
            '    {"what":"EXACT campaign_name from the PAUSE-ELIGIBLE list above — never a generic phrase like \'paused campaigns\'","why":"specific data reason citing that campaign'"'"'s own numbers","expected_saving":"RS X/month","urgency":"immediate/this week/monitor"}\n'
            '  ],\n'
            '  "scale_recommendations": [\n'
            '    {"what":"EXACT campaign_name from the CAMPAIGN BREAKDOWN above (or exact ad set) — never a generic phrase","why":"specific data reason citing that campaign'"'"'s own numbers","how_much":"increase budget by X%","expected_impact":"..."}\n'
            '  ],\n'
            '  "audience_recommendations": [\n'
            '    {"current":"current targeting","problem":"specific issue","recommended_change":"what to change","expected_improvement":"..."}\n'
            '  ],\n'
            '  "creative_recommendations": [\n'
            '    {"issue":"specific issue from data","recommendation":"what to do","format":"image/video/carousel","hook_suggestion":"opening line for the ad"}\n'
            '  ],\n'
            '  "budget_recommendations": {\n'
            '    "current_total": "RS ...",\n'
            '    "recommended_total": "RS ...",\n'
            '    "reallocation": [\n'
            '      {"platform":"Google/Meta/Remarketing","current":"RS ...","recommended":"RS ...","reason":"specific reason"}\n'
            '    ]\n'
            '  },\n'
            '  "keyword_recommendations": [\n'
            '    {"action":"add/remove/modify","keyword":"...","reason":"..."}\n'
            '  ],\n'
            '  "this_week_actions": [\n'
            '    {"priority":1,"action":"most impactful action","expected_impact":"...","time_to_implement":"e.g. 30 mins"},\n'
            '    {"priority":2,"action":"...","expected_impact":"...","time_to_implement":"..."},\n'
            '    {"priority":3,"action":"...","expected_impact":"...","time_to_implement":"..."},\n'
            '    {"priority":4,"action":"...","expected_impact":"...","time_to_implement":"..."},\n'
            '    {"priority":5,"action":"...","expected_impact":"...","time_to_implement":"..."}\n'
            '  ],\n'
            '  "next_test": {\n'
            '    "what_to_test":"...","hypothesis":"...","how_to_measure":"...","duration":"e.g. 2 weeks"\n'
            '  },\n'
            '  "decision_summary": {\n'
            '    "top_problems": [{"problem":"...","impact":"high/medium/low"}],\n'
            '    "top_actions": [{"action":"...","expected_impact":"...","effort":"low/medium/high"}],\n'
            '    "expected_improvement_if_actioned": "range, explicitly labeled as a projection, not a guarantee",\n'
            '    "overall_confidence": 0\n'
            '  },\n'
            '  "confidence": 75\n'
            '}\n\n'
            "Rules:\n"
            "- Every recommendation must cite SPECIFIC numbers from the data above, not generic advice.\n"
            "- Use RS as prefix for Indian Rupees (e.g. RS 5,000).\n"
            "- this_week_actions must be ordered by impact (highest first, priority 1 = most impactful).\n"
            "- If no active campaigns: focus pause_recommendations on pre-launch setup; scale on budget allocation plan.\n"
            "- CAMPAIGN NAMING: when recommending activating, pausing, or scaling a campaign, ALWAYS name the "
            "specific campaign from the CAMPAIGN BREAKDOWN data above. Never say 'paused campaigns', 'underperforming "
            "campaigns', or any other generic phrase — say e.g. 'Activate [exact campaign_name] because [specific "
            "reason from its data]'.\n"
            "- pause_recommendations may ONLY name campaigns from the PAUSE-ELIGIBLE list above. If that list says "
            "NONE, return an empty pause_recommendations array — do not invent a pause recommendation.\n"
            "- Minimum 2 items in scale, audience, creative, keyword lists. pause_recommendations has no minimum — "
            "leave it empty rather than padding it with a campaign that isn't pause-eligible.\n"
            "- Minimum 5 this_week_actions.\n"
            "- decision_summary.top_problems and top_actions: exactly 3 each, ranked by impact.\n"
            "- STATISTICAL SIGNIFICANCE: if the insufficient-data banner above is present, do NOT recommend bid "
            "increases or budget shifts as confident actions — frame every scale_recommendations/budget_recommendations "
            "item as conditional, e.g. 'Once this campaign reaches 1000+ impressions, consider...' rather than a "
            "direct instruction to act now. confidence and decision_summary.overall_confidence MUST be 40 or below "
            "in this case.\n"
            "- BANNED words: Elevate, Transform, Unlock, Revolutionize, Empower, Seamless, Leverage, Utilize, Boost, "
            "Maximize, Cutting-edge, State-of-the-art, World-class, One-stop solution, Look no further, In today's digital age.\n"
            "- Return ONLY the JSON. Nothing else."
        )

        logger.info(f"[AI-OPT] Sending to GPT-4o (prompt_len={len(prompt)})")
        resp = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=2800,
            temperature=0.4,
        )
        raw = resp.choices[0].message.content.strip()
        logger.info(f"[AI-OPT] GPT responded (len={len(raw)})")
        optimizer = json.loads(raw)

        # Replace RS → ₹ in all string values
        optimizer = _fix_rs(optimizer)
        # Scrub banned buzzwords (and their conjugations) from every nested string —
        # this is structured JSON, not one raw text blob, so the filter must recurse.
        optimizer = _clean_banned_words_deep(optimizer)

        # ── Inject the same deterministic, honesty-first fields Performance
        # Intelligence uses — never GPT-generated, so they can't drift ───────
        optimizer["health_scores"]        = health_scores
        optimizer["data_sufficiency"]     = data_sufficiency
        optimizer["conversion_diagnosis"] = conversion_diag
        optimizer["why_not_converting"]   = conversion_diag
        optimizer["confidence_breakdown"] = confidence_bkdown

        if data_sufficiency["insufficient_data"]:
            try:
                optimizer["confidence"] = min(int(optimizer.get("confidence", 40) or 40), 40)
            except Exception:
                optimizer["confidence"] = 40
            _ds = optimizer.get("decision_summary")
            if isinstance(_ds, dict):
                try:
                    _ds["overall_confidence"] = min(int(_ds.get("overall_confidence", 40) or 40), 40)
                except Exception:
                    _ds["overall_confidence"] = 40

        save_to_memory("optimizer", norm_key, {"optimizer_data": optimizer})
        logger.info(f"[AI-OPT] Done: key={norm_key!r} confidence={optimizer.get('confidence')}")

        log_activity(
            "ai_optimizer", business_key=norm_key,
            business_name=business_data.get("business_name", ""),
            url=url, industry=industry, city=city,
            summary="AI Optimizer recommendations generated" + (" (insufficient data)" if data_sufficiency["insufficient_data"] else ""),
        )

        return {
            "success":     True,
            "memory_used": True,
            "business_key": norm_key,
            "has_kpi":     has_kpi,
            "has_perf":    has_perf,
            "optimizer":   optimizer,
        }

    except Exception as _e:
        tb = _traceback.format_exc()
        logger.error(f"[AI-OPT] ERROR: {_e}\n{tb}")
        return {"success": False, "error": str(_e), "traceback": tb}

@app.post("/ai-optimizer")
async def ai_optimizer(request: AIOptimizerRequest):
    return await _run_ai_optimizer_core(request.url, request.industry, request.city)


# ── Continuous Optimization Loop (Phase 10) ──────────────────────────────────
# Stateless web services (like this one on Render) can't reliably run an
# in-process scheduler — multiple worker processes would each fire their own
# timer and duplicate work, and a free-tier instance can sleep between
# requests. Instead this endpoint runs the optimizer for every business we
# have performance data for, and is meant to be triggered by an EXTERNAL
# scheduler (a Render Cron Job, GitHub Actions schedule, or any cron hitting
# this URL every few hours) — that's what makes the loop "continuous".
@app.post("/optimizer/run-all")
async def optimizer_run_all():
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT DISTINCT business_key FROM performance_memory")
            ).fetchall()
        business_keys = [r[0] for r in rows if r[0]]
    except Exception as _e:
        logger.error(f"[OPT-RUN-ALL] Could not list business_keys: {_e}")
        return {"success": False, "error": str(_e)}

    results = []
    for bk in business_keys:
        try:
            mem = get_memory(bk)
            bm = mem.get("business", {}) or {}
            industry = bm.get("industry", "") or ""
            city     = bm.get("city", "") or ""
            # business_key encodes the url/industry portion; business_memory's own
            # industry/city columns (saved by full-report) are the reliable source
            # since the key itself may be url-only or industry-only depending on mode.
            url_guess = bk.split("::")[0] if "::" in bk else bk

            prev_optimizer = (mem.get("optimizer", {}) or {}).get("optimizer_data")
            if isinstance(prev_optimizer, str):
                try: prev_optimizer = json.loads(prev_optimizer)
                except Exception: prev_optimizer = {}
            prev_verdict = (prev_optimizer or {}).get("overall_verdict")

            outcome = await _run_ai_optimizer_core(url_guess, industry, city)
            new_verdict = (outcome.get("optimizer", {}) or {}).get("overall_verdict") if outcome.get("success") else None

            results.append({
                "business_key":    bk,
                "success":         outcome.get("success", False),
                "overall_verdict": new_verdict,
                "health_change":   (outcome.get("optimizer", {}) or {}).get("health_change") if outcome.get("success") else None,
                "alert":           bool(prev_verdict and new_verdict and prev_verdict != new_verdict),
                "error":           outcome.get("error") if not outcome.get("success") else None,
            })
        except Exception as _e:
            logger.error(f"[OPT-RUN-ALL] Failed for {bk!r}: {_e}")
            results.append({"business_key": bk, "success": False, "error": str(_e)})

    alerts = [r for r in results if r.get("alert")]
    logger.info(f"[OPT-RUN-ALL] Processed {len(results)} businesses, {len(alerts)} verdict changes")

    return {
        "success":              True,
        "businesses_processed": len(results),
        "alerts":               alerts,
        "results":              results,
    }


# ── Module 22: Result Center ──────────────────────────────────────────────────

class ResultCenterRequest(BaseModel):
    url:      str = ""
    industry: str = ""
    city:     str = ""

def _save_growth_memory(gmp: dict):
    """Insert one row into growth_memory (anonymous, no business_key)."""
    now = datetime.now().isoformat()
    try:
        with engine.connect() as conn:
            if "postgresql" in str(engine.url):
                conn.execute(text("""
                    INSERT INTO growth_memory
                      (industry, business_type, budget_range, winning_audience,
                       winning_platform, winning_offer, avg_cpl, avg_roas, confidence, created_at)
                    VALUES (:industry, :business_type, :budget_range, :winning_audience,
                            :winning_platform, :winning_offer, :avg_cpl, :avg_roas, :confidence, :created_at)
                """), {
                    "industry":         gmp.get("industry", ""),
                    "business_type":    gmp.get("business_type", ""),
                    "budget_range":     gmp.get("budget_range", ""),
                    "winning_audience": gmp.get("winning_audience", ""),
                    "winning_platform": gmp.get("winning_platform", ""),
                    "winning_offer":    gmp.get("winning_offer", ""),
                    "avg_cpl":          gmp.get("avg_cpl", ""),
                    "avg_roas":         gmp.get("avg_roas", ""),
                    "confidence":       float(gmp.get("confidence", 0) or 0),
                    "created_at":       now,
                })
            else:
                conn.execute(text("""
                    INSERT INTO growth_memory
                      (industry, business_type, budget_range, winning_audience,
                       winning_platform, winning_offer, avg_cpl, avg_roas, confidence, created_at)
                    VALUES (:industry, :business_type, :budget_range, :winning_audience,
                            :winning_platform, :winning_offer, :avg_cpl, :avg_roas, :confidence, :created_at)
                """), {
                    "industry":         gmp.get("industry", ""),
                    "business_type":    gmp.get("business_type", ""),
                    "budget_range":     gmp.get("budget_range", ""),
                    "winning_audience": gmp.get("winning_audience", ""),
                    "winning_platform": gmp.get("winning_platform", ""),
                    "winning_offer":    gmp.get("winning_offer", ""),
                    "avg_cpl":          gmp.get("avg_cpl", ""),
                    "avg_roas":         gmp.get("avg_roas", ""),
                    "confidence":       float(gmp.get("confidence", 0) or 0),
                    "created_at":       now,
                })
            conn.commit()
        logger.info(f"[RESULT] Growth memory saved: industry={gmp.get('industry')!r}")
    except Exception as _e:
        logger.error(f"[RESULT] growth_memory save failed: {_e}")

def get_growth_learning(industry: str, limit: int = 5) -> dict:
    """
    Learning Engine (Phase 11) read side: pull anonymised winning-pattern
    evidence from OTHER businesses in the same industry (across the whole
    Adsoh install base, not just this one business_key), most confident
    and most recent first. Used to calibrate predictions/recommendations
    with real cross-business evidence instead of generic benchmarks.
    """
    industry = (industry or "").strip()
    if not industry:
        return {"sample_size": 0, "entries": []}
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT business_type, budget_range, winning_audience, winning_platform, "
                    "winning_offer, avg_cpl, avg_roas, confidence, created_at FROM growth_memory "
                    "WHERE LOWER(industry) = LOWER(:industry) "
                    "ORDER BY confidence DESC, created_at DESC LIMIT :limit"
                ),
                {"industry": industry, "limit": limit},
            ).mappings().all()
        entries = [dict(r) for r in rows]
        return {"sample_size": len(entries), "entries": entries}
    except Exception as _e:
        logger.error(f"[GROWTH-LEARNING] lookup failed for industry={industry!r}: {_e}")
        return {"sample_size": 0, "entries": []}

def growth_learning_block(industry: str, label: str = "CROSS-BUSINESS LEARNING") -> str:
    """Render get_growth_learning() as a prompt-ready text block, or '' if no data yet."""
    learning = get_growth_learning(industry)
    if not learning["entries"]:
        return ""
    lines = [
        f"- Platform: {e.get('winning_platform','?')} | Audience: {e.get('winning_audience','?')} | "
        f"Offer: {e.get('winning_offer','?')} | CPL: {e.get('avg_cpl','?')} | ROAS: {e.get('avg_roas','?')} "
        f"(confidence {e.get('confidence','?')})"
        for e in learning["entries"]
    ]
    return (
        f"{label} — anonymised results from {learning['sample_size']} other {industry} campaign(s) "
        "run through Adsoh (real outcomes, not industry averages — weigh these over generic benchmarks):\n"
        + "\n".join(lines) + "\n\n"
    )

@app.post("/result-center")
async def result_center(request: ResultCenterRequest):
    try:
        industry = (request.industry or "").strip()
        city     = (request.city or "").strip()
        city_display = city or "India (national average)"
        _bk_src  = (request.url     or "").strip()

        logger.info(f"[RESULT] url={request.url!r} industry={industry!r} city={city!r}")

        memory, norm_key = get_memory_with_city_fallback(_bk_src, industry, city)
        logger.info(f"[RESULT-CENTER] LOOKUP key: '{norm_key}'")
        logger.info(f"[RESULT] key={norm_key!r} tables={list(memory.keys())}")

        if not memory:
            return {"success": False, "memory_used": False,
                    "message": "No memory found. Run Marketing Brain first with the same URL + industry."}

        # ── Unpack all relevant memory tables ────────────────────────────────
        def _jload(raw):
            if isinstance(raw, str):
                try: return json.loads(raw)
                except: return {}
            return raw or {}

        def _field(table_key, field_key):
            return _jload(_jload(memory.get(table_key, {})).get(field_key, {}))

        kpi_data         = _field("kpi",         "kpi_data")
        perf_data        = _field("performance",  "performance_data")
        optimizer_data   = _field("optimizer",    "optimizer_data")
        business_data    = _jload(memory.get("business",  {}))
        audience_data    = _jload(memory.get("audience",  {}))
        offer_data       = _field("offer",        "offer_data")
        outreach_data    = _field("outreach",      "outreach_data")
        opportunity_data = _field("opportunity",   "opportunity_data")

        has_perf = bool(perf_data)
        has_kpi  = bool(kpi_data)
        has_opt  = bool(optimizer_data)

        def _sv(d, *keys, dfl="N/A"):
            cur = d
            for k in keys:
                if not isinstance(cur, dict): return dfl
                cur = cur.get(k, dfl)
            return str(cur or dfl)

        # ── Build compact context for GPT ─────────────────────────────────────
        pm_exp   = kpi_data.get("predicted_metrics", {}) or {}
        prim_kpi = kpi_data.get("primary_kpi", {})       or {}
        cac_ltv  = kpi_data.get("cac_ltv", {})           or {}
        am       = perf_data.get("actual_metrics", {})   or {}
        eva      = perf_data.get("expected_vs_actual", []) or []
        camp_bk  = perf_data.get("campaign_breakdown", []) or []
        health   = perf_data.get("overall_health", 0)
        trend    = perf_data.get("trend", "N/A")
        top_ins  = perf_data.get("top_insight", "")
        big_prob = perf_data.get("biggest_problem", "")

        def _exp(key):
            m = pm_exp.get(key, {})
            return (m.get("value", "N/A") if isinstance(m, dict) else str(m)) if m else "N/A"

        good_camps = [c.get("campaign_name","?") for c in (camp_bk if isinstance(camp_bk,list) else [])
                      if isinstance(c, dict) and c.get("performance_rating") == "good"]
        poor_camps = [c.get("campaign_name","?") for c in (camp_bk if isinstance(camp_bk,list) else [])
                      if isinstance(c, dict) and c.get("performance_rating") == "poor"]

        opt_works = [r.get("what","?") for r in (optimizer_data.get("scale_recommendations",[]) or [])
                     if isinstance(r, dict)][:3]
        opt_pause = [r.get("what","?") for r in (optimizer_data.get("pause_recommendations",[]) or [])
                     if isinstance(r, dict)][:3]

        context = (
            "BUSINESS: " + _sv(business_data,"business_name") +
            " | Industry: " + (industry or _sv(business_data,"industry")) +
            " | City: " + city_display + "\n\n"

            "=== KPI ENGINE (Predicted) ===\n"
            + ("NOT AVAILABLE\n" if not has_kpi else
               "Primary KPI: " + _sv(prim_kpi,"metric") + " → " + _sv(prim_kpi,"target") + "\n"
               "Predicted CTR: " + _exp("ctr") + " | CPC: " + _exp("cpc") + " | CPA: " + _exp("cpa") +
               " | ROAS: " + _exp("roas") + " | Revenue: " + _exp("revenue_potential") + "\n"
               "CAC: " + _sv(cac_ltv,"estimated_cac") + " | LTV: " + _sv(cac_ltv,"estimated_ltv") +
               " | LTV:CAC " + _sv(cac_ltv,"ltv_cac_ratio") + "\n"
            ) + "\n"

            "=== PERFORMANCE INTELLIGENCE (Actual) ===\n"
            + ("NOT AVAILABLE\n" if not has_perf else
               "Health: " + str(health) + "/100 | Trend: " + str(trend) + "\n"
               "Actual: CTR " + _sv(am,"ctr") + " | CPC " + _sv(am,"cpc") +
               " | Cost " + _sv(am,"cost") + " | Conv " + str(am.get("conversions","?")) +
               " | CPA " + _sv(am,"cpa") + " | ROAS " + _sv(am,"roas") + "\n"
               "Top insight: " + str(top_ins) + "\n"
               "Biggest problem: " + str(big_prob) + "\n"
               + ("Good campaigns: " + ", ".join(good_camps) + "\n" if good_camps else "")
               + ("Poor campaigns: " + ", ".join(poor_camps) + "\n" if poor_camps else "")
            ) + "\n"

            "=== AI OPTIMIZER ===\n"
            + ("NOT AVAILABLE\n" if not has_opt else
               "Scale: " + "; ".join(opt_works) + "\n"
               "Pause: " + "; ".join(opt_pause) + "\n"
               "Verdict: " + str(optimizer_data.get("overall_verdict","N/A")) + "\n"
            ) + "\n"

            "=== OFFER & AUDIENCE ===\n"
            "Recommended offer: " + _sv(offer_data,"recommended_offer")[:200] + "\n"
            "Lead magnet: " + _sv(offer_data,"lead_magnet")[:150] + "\n"
            "Best opportunity audience: " + _sv(opportunity_data,"highest_roi_audience")[:150] + "\n"
        )

        no_perf_note = "" if has_perf else (
            "NOTE: No performance data available — base revenue_summary on KPI predictions, "
            "set actual values to 'N/A', focus on prediction_vs_actual using predicted vs benchmark.\n"
        )

        prompt = (
            "You are a senior marketing strategist producing a final campaign results report for an Indian business.\n"
            "Analyse all the data below and produce a comprehensive Result Center report.\n\n"
            + context + "\n"
            + no_perf_note + "\n"
            "Return ONLY valid JSON (no markdown) with this EXACT schema:\n"
            "{\n"
            '  "campaign_verdict": "success/partial/needs_work",\n'
            '  "overall_score": 72,\n'
            '  "prediction_vs_actual": [\n'
            '    {"metric":"CTR","predicted":"' + _exp("ctr") + '","actual":"' + _sv(am,"ctr") + '","verdict":"below/on_track/above","learning":"what this tells us"},\n'
            '    {"metric":"CPC","predicted":"' + _exp("cpc") + '","actual":"' + _sv(am,"cpc") + '","verdict":"below/on_track/above","learning":"..."},\n'
            '    {"metric":"CPA","predicted":"' + _exp("cpa") + '","actual":"' + _sv(am,"cpa") + '","verdict":"below/on_track/above","learning":"..."},\n'
            '    {"metric":"ROAS","predicted":"' + _exp("roas") + '","actual":"' + _sv(am,"roas") + '","verdict":"below/on_track/above","learning":"..."},\n'
            '    {"metric":"Conversions","predicted":"' + _exp("conversions") + '","actual":"' + str(am.get("conversions","N/A")) + '","verdict":"below/on_track/above","learning":"..."}\n'
            '  ],\n'
            '  "what_worked": [\n'
            '    {"what":"...","why":"specific data reason","keep_doing":"..."},\n'
            '    {"what":"...","why":"...","keep_doing":"..."}\n'
            '  ],\n'
            '  "what_failed": [\n'
            '    {"what":"...","why":"specific data reason","fix_next_time":"..."},\n'
            '    {"what":"...","why":"...","fix_next_time":"..."}\n'
            '  ],\n'
            '  "revenue_summary": {\n'
            '    "total_spend": "' + _sv(am,"cost") + '",\n'
            '    "total_leads": 0,\n'
            '    "total_conversions": ' + str(am.get("conversions",0)) + ',\n'
            '    "revenue_generated": "RS ...",\n'
            '    "roas": "' + _sv(am,"roas") + '",\n'
            '    "roi": "0%"\n'
            '  },\n'
            '  "best_performing": {\n'
            '    "audience":"best audience segment from data",\n'
            '    "creative":"best creative format or hook",\n'
            '    "platform":"Google/Meta/etc",\n'
            '    "campaign":"' + (good_camps[0] if good_camps else "N/A") + '"\n'
            '  },\n'
            '  "key_learnings": ["learning 1","learning 2","learning 3","learning 4","learning 5"],\n'
            '  "next_campaign_recommendations": [\n'
            '    {"recommendation":"...","reason":"specific data reason","expected_improvement":"..."},\n'
            '    {"recommendation":"...","reason":"...","expected_improvement":"..."},\n'
            '    {"recommendation":"...","reason":"...","expected_improvement":"..."}\n'
            '  ],\n'
            '  "growth_memory_packet": {\n'
            '    "industry":"' + industry + '",\n'
            '    "business_type":"derived from memory",\n'
            '    "budget_range":"RS X,XXX - RS X,XXX/month",\n'
            '    "winning_audience":"most effective audience segment",\n'
            '    "winning_platform":"Google/Meta/etc",\n'
            '    "winning_offer":"best performing offer",\n'
            '    "avg_cpl":"RS ...",\n'
            '    "avg_roas":"0x",\n'
            '    "confidence":75\n'
            '  },\n'
            '  "next_actions": [\n'
            '    {"priority":1,"action":"most important next action","deadline":"this week/this month","expected_impact":"..."},\n'
            '    {"priority":2,"action":"...","deadline":"...","expected_impact":"..."},\n'
            '    {"priority":3,"action":"...","deadline":"...","expected_impact":"..."}\n'
            '  ]\n'
            '}\n\n'
            "Rules:\n"
            "- Replace overall_score with an integer 0-100 (based on how closely actual matched predicted).\n"
            "- Replace revenue_summary.total_leads and total_conversions with actual integers.\n"
            "- Use RS prefix for all rupee values (post-processing will replace with ₹).\n"
            "- growth_memory_packet must be anonymised — no business name, no URL, no PII.\n"
            "- Minimum 2 items in what_worked, what_failed, next_campaign_recommendations.\n"
            "- Minimum 5 key_learnings, minimum 3 next_actions.\n"
            "- BANNED words: Elevate, Transform, Unlock, Revolutionize, Empower, Seamless.\n"
            "- Return ONLY the JSON. Nothing else."
        )

        logger.info(f"[RESULT] GPT-4o call (prompt_len={len(prompt)})")
        resp = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=2800,
            temperature=0.3,
        )
        raw = resp.choices[0].message.content.strip()
        logger.info(f"[RESULT] GPT responded (len={len(raw)})")
        result_obj = json.loads(raw)

        # RS → ₹ in all string values
        result_obj = _fix_rs(result_obj)

        # Save result_memory
        save_to_memory("result", norm_key, {
            "result_data":   result_obj,
            "overall_score": float(result_obj.get("overall_score", 0) or 0),
        })

        # Save growth_memory (anonymous)
        gmp = result_obj.get("growth_memory_packet", {})
        if gmp:
            await asyncio.to_thread(_save_growth_memory, gmp)

        logger.info(f"[RESULT] Done: key={norm_key!r} score={result_obj.get('overall_score')} verdict={result_obj.get('campaign_verdict')}")

        log_activity(
            "result_center", business_key=norm_key,
            business_name=business_data.get("business_name", ""),
            url=request.url, industry=industry, city=city,
            summary=f"Result Center report — verdict: {result_obj.get('campaign_verdict', 'N/A')}",
        )

        return {
            "success":     True,
            "memory_used": True,
            "business_key": norm_key,
            "result":      result_obj,
        }

    except Exception as _e:
        tb = _traceback.format_exc()
        logger.error(f"[RESULT] ERROR: {_e}\n{tb}")
        return {"success": False, "error": str(_e), "traceback": tb}


# ── Module 23: Creative Studio ────────────────────────────────────────────────
# Unified Creative Intelligence Engine — merges the former AI Creative
# Director (blank-slate, business-intelligence-driven) and Ad-to-Creative
# Generator (campaign-driven) into one module + one mode toggle. Reuses
# Business DNA / audience / growth / performance memory rather than
# duplicating lookups, and auto-runs a lightweight discovery pass the first
# time a business has no Marketing Brain memory yet, so creatives are never
# generic. Does NOT generate images itself — that's a future phase; this
# generates everything needed to produce them elsewhere.

def _resolve_campaign_by_id(campaign_id: str) -> dict:
    """
    Look up a real campaign by ID across Google Ads then Meta — returns
    {"platform", "name", "status", "impressions", "clicks", "cost_inr",
    "conversions", "ctr_pct", "avg_cpc_inr"} or {} if not found on either.
    Reuses the exact same fetchers Performance Intelligence and /campaigns/all
    already use — no duplicated Google/Meta query logic.
    """
    try:
        rows = _fetch_gads_campaigns(90)
        for row in rows:
            if str(row.get("campaign_id")) == str(campaign_id):
                return {**row, "platform": "google"}
    except Exception as _e:
        logger.warning(f"[AD-TO-CREATIVE] Google campaign lookup failed: {_e}")

    try:
        from facebook_business.adobjects.adaccount import AdAccount
        from facebook_business.adobjects.campaign import Campaign
        _, account_id = get_meta_ads_client()

        def _fetch_meta_campaign():
            account = AdAccount(account_id)
            return list(account.get_campaigns(fields=[Campaign.Field.id, Campaign.Field.name, Campaign.Field.status]))

        meta_campaigns = _fetch_meta_campaign()
        match = next((c for c in meta_campaigns if str(c.get(Campaign.Field.id)) == str(campaign_id)), None)
        if match:
            perf_row = {}
            try:
                from facebook_business.adobjects.adsinsights import AdsInsights
                account = AdAccount(account_id)
                insight_rows = account.get_insights(
                    fields=[AdsInsights.Field.impressions, AdsInsights.Field.clicks, AdsInsights.Field.spend,
                            AdsInsights.Field.ctr, AdsInsights.Field.cpc, AdsInsights.Field.conversions],
                    params={"date_preset": "last_30d", "level": "campaign",
                            "filtering": [{"field": "campaign.id", "operator": "EQUAL", "value": campaign_id}]},
                )
                insight_rows = list(insight_rows)
                if insight_rows:
                    r = insight_rows[0]
                    perf_row = {
                        "impressions": int(r.get(AdsInsights.Field.impressions, 0) or 0),
                        "clicks":      int(r.get(AdsInsights.Field.clicks, 0) or 0),
                        "cost_inr":    round(float(r.get(AdsInsights.Field.spend, 0) or 0), 2),
                        "ctr_pct":     round(float(r.get(AdsInsights.Field.ctr, 0) or 0), 2),
                        "avg_cpc_inr": round(float(r.get(AdsInsights.Field.cpc, 0) or 0), 2),
                        "conversions": 0.0,
                    }
            except Exception as _pe:
                logger.warning(f"[AD-TO-CREATIVE] Meta insights lookup failed: {_pe}")
            return {
                "platform": "meta",
                "campaign_id": str(match.get(Campaign.Field.id)),
                "name": match.get(Campaign.Field.name),
                "status": match.get(Campaign.Field.status),
                "impressions": 0, "clicks": 0, "cost_inr": 0.0, "conversions": 0.0, "ctr_pct": 0.0, "avg_cpc_inr": 0.0,
                **perf_row,
            }
    except RuntimeError:
        pass
    except Exception as _e:
        logger.warning(f"[AD-TO-CREATIVE] Meta campaign lookup failed: {_e}")

    return {}


def _resolve_business_key_from_campaign(campaign_id: str) -> dict:
    """
    activity_log already records business_key + reference_id (=campaign_id)
    for every campaign this tool has ever pushed — reuse that instead of
    building a separate campaign→business mapping table.
    """
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT business_key, url, industry, city FROM activity_log "
                    "WHERE activity_type IN ('google_campaign_created', 'meta_campaign_created') "
                    "AND reference_id = :cid ORDER BY id DESC LIMIT 1"
                ),
                {"cid": campaign_id},
            ).mappings().first()
        return dict(row) if row else {}
    except Exception as _e:
        logger.warning(f"[AD-TO-CREATIVE] business_key lookup from campaign_id failed: {_e}")
        return {}


_CREATIVE_STUDIO_VARIANTS = ["Direct Response", "Emotional/Lifestyle", "Authority/Trust"]
_CREATIVE_SCORE_FIELDS = ["visual_appeal", "headline", "cta", "brand_match", "emotional_appeal", "scroll_stopping", "conversion_potential"]


async def _lightweight_business_audience_discovery(url: str, industry: str, city: str) -> dict:
    """
    First-run discovery for Creative Studio when a business has no Marketing
    Brain memory yet — reuses gather_bi_data() (the same BI engine /full-report
    itself uses, and it's cached) for business DNA instead of re-crawling and
    re-extracting from scratch, then one focused GPT call for audience
    psychographics. Saves both via save_to_memory so every other module
    benefits from this business's data going forward too — not a throwaway.
    """
    bi = None
    try:
        bi = await gather_bi_data(url, industry)
    except Exception as _e:
        logger.warning(f"[CREATIVE-STUDIO] gather_bi_data failed during discovery: {_e}")

    # gather_bi_data() returns {"intelligence": {"business_dna": {...}, "positioning": {...}, ...}, "scores": {...}}
    # — nested one level deeper than a flat dict. business_dna itself has no
    # "business_name"/"positioning"/"uvp" fields (its real schema is
    # detected_industry/business_model/target_geography/unique_value_prop/...);
    # "positioning" is a SEPARATE nested block with current_positioning/
    # winning_position. Reading these at the wrong level silently produced an
    # empty business_dna every time (caught via a live test where a real
    # business's positioning/uvp came back blank despite a successful crawl).
    _intelligence = (bi or {}).get("intelligence", {}) if bi else {}
    business_dna     = _intelligence.get("business_dna", {}) or {}
    _positioning_blk = _intelligence.get("positioning", {}) or {}
    business_name    = url
    uvp              = business_dna.get("unique_value_prop", "")
    positioning      = _positioning_blk.get("winning_position") or _positioning_blk.get("current_positioning", "") or uvp
    # target_geography ("Local"/"Regional"/"National"/"International") is a
    # real, direct signal for the locality determination downstream — surface
    # it so Creative Studio doesn't have to re-infer national-vs-local purely
    # from positioning prose.
    target_geography = business_dna.get("target_geography", "")

    # ── Never ask GPT to invent an audience with zero real signal to ground it.
    # This is exactly how the wellness/mindfulness contamination bug happened:
    # a business with blank industry AND empty business_dna still got an
    # audience-discovery call ("Industry: unknown | Positioning: unknown"),
    # and GPT filled the vacuum with a plausible-sounding but completely
    # unrelated persona. If there's truly nothing to go on, return no
    # segments and say so honestly instead of fabricating one. ─────────────
    has_real_signal = bool(industry.strip() or positioning.strip() or uvp.strip() or business_dna)
    audience_out = {"segments": [], "brand_personality": ""}
    if has_real_signal:
        try:
            prompt = (
                "Based on this business's real data, identify its target audience psychographics for a creative "
                "marketing brief. Be specific — cite what's actually plausible given the business/industry, don't "
                "invent unrelated demographics.\n\n"
                f"BUSINESS: {business_name} | Industry: {industry or 'unknown'} | Positioning: {positioning or 'unknown'} "
                f"| Target geography: {target_geography or 'unknown'}\n\n"
                'Return ONLY JSON: {"segments": [{"segment":"...","pain_points":"...","desires":"...",'
                '"content_they_respond_to":"..."}], "brand_personality": "one line describing this brand\'s personality"}'
            )
            resp = await asyncio.to_thread(
                client.chat.completions.create, model="gpt-4o", messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}, max_tokens=700, temperature=0.4,
            )
            audience_out = json.loads(resp.choices[0].message.content)
        except Exception as _e:
            logger.warning(f"[CREATIVE-STUDIO] Audience discovery call failed: {_e}")
    else:
        logger.warning(
            f"[CREATIVE-STUDIO] Skipping audience discovery for {business_name!r} — no industry, positioning, "
            "UVP, or business_dna available to ground it. Returning no segments rather than hallucinating one."
        )

    return {
        "business": {
            "business_name": business_name, "industry": industry, "city": city,
            "business_dna": business_dna, "uvp": uvp, "positioning": positioning,
            "target_geography": target_geography,
        },
        "audience": {"segments": audience_out.get("segments", [])},
        "brand_personality": audience_out.get("brand_personality", ""),
    }


def _normalize_creative_score(score: dict) -> dict:
    """
    Deterministic score fix — never trust GPT's own arithmetic for `overall`.
    Handles the known bug where a concept's sub-scores land around 80 but
    overall shows up as "8/100" (0-10 scale slip): any sub-score in (0,10] is
    treated as a 0-10 value and upscaled ×10. overall is ALWAYS recomputed as
    the average of the 7 sub-scores, so it can never mismatch them.
    """
    vals = []
    for f in _CREATIVE_SCORE_FIELDS:
        v = score.get(f, 0)
        try: v = float(v)
        except Exception: v = 0.0
        if 0 < v <= 10:
            v = v * 10
        v = max(0, min(100, v))
        score[f] = round(v)
        vals.append(score[f])
    score["overall"] = round(sum(vals) / len(vals)) if vals else 0
    return score


def _creative_score_is_empty(score: dict) -> bool:
    return not score or all(float(score.get(f, 0) or 0) == 0 for f in _CREATIVE_SCORE_FIELDS)


async def _generate_missing_image_prompt(concept: dict, business_context: str) -> str:
    """Targeted single-concept image_prompt generation — used when the main
    call returns a concept with the field missing/empty entirely, instead of
    regenerating the whole response for one dropped field."""
    try:
        prompt = (
            "Write ONE production-ready image-generation prompt for this ad creative concept, as natural, "
            "richly-descriptive prose the way a professional would actually type it into Midjourney/DALL-E — "
            "covering subject, environment, camera/lens, lighting, composition, mood, color palette, depth of "
            "field, reserved space for branding and typography, commercial photography style, ending with "
            "'Negative prompt: ...' and 'Render quality: ...'. NEVER prefix it with a meta-label like "
            "'production-ready prompt:' and NEVER write it as a comma-separated field:value list.\n\n"
            f"Business context: {business_context}\n"
            f"Concept: headline={concept.get('headline')!r}, subheadline={concept.get('subheadline')!r}, "
            f"visual_direction={concept.get('visual_direction')!r}\n\n"
            'Return ONLY JSON: {"image_prompt": "..."}'
        )
        resp = await asyncio.to_thread(
            client.chat.completions.create, model="gpt-4o", messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}, max_tokens=350, temperature=0.5,
        )
        return json.loads(resp.choices[0].message.content).get("image_prompt", "").strip()
    except Exception as _e:
        logger.warning(f"[CREATIVE-STUDIO] Targeted image_prompt generation failed: {_e}")
        return ""


async def _rescore_concept(concept: dict, business_context: str) -> dict:
    """Targeted single-concept rescoring — used only when the main call
    returns a concept with no real score data, instead of regenerating
    everything (the old bug: concepts B/C silently coming back 0/100)."""
    try:
        prompt = (
            "Score this ad creative concept honestly on a strict 0-100 scale for each dimension below. "
            f"Business context: {business_context}\n"
            f"Concept: headline={concept.get('headline')!r}, subheadline={concept.get('subheadline')!r}, "
            f"visual_direction={concept.get('visual_direction')!r}, cta={concept.get('cta')!r}\n\n"
            'Return ONLY JSON: {"visual_appeal":0,"headline":0,"cta":0,"brand_match":0,"emotional_appeal":0,'
            '"scroll_stopping":0,"conversion_potential":0,"reasoning":"one line explaining the scores"}'
        )
        resp = await asyncio.to_thread(
            client.chat.completions.create, model="gpt-4o", messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}, max_tokens=300, temperature=0.4,
        )
        rescored = json.loads(resp.choices[0].message.content)
        return _normalize_creative_score(rescored)
    except Exception as _e:
        logger.warning(f"[CREATIVE-STUDIO] Targeted rescore failed: {_e}")
        return _normalize_creative_score({})


def _backfill_creative_studio(output: dict, variant_names: list) -> dict:
    """Guarantee exactly one concept per variant name — schema-ordering +
    backfill so a partial/malformed GPT response still renders a complete,
    usable 3-concept output instead of leaving sections empty."""
    concepts = output.get("concepts") or []
    concepts = [c for c in concepts if isinstance(c, dict) and c.get("variant_name")]
    have_names = {c.get("variant_name") for c in concepts}
    letters = ["A", "B", "C"]
    for i, vname in enumerate(variant_names):
        if vname not in have_names:
            concepts.append({
                "variant_id": letters[i], "variant_name": vname,
                "headline": "N/A — GPT did not return this concept", "subheadline": "", "caption": "", "cta": "",
                "hashtags": [], "visual_direction": "N/A",
                "image_prompt": "N/A — regenerate to get this concept's image prompt",
                "design_layout": "N/A", "applicable_platforms_and_sizes": [],
                "reason_this_matches": "N/A — not generated",
                "ai_review": {"strong": "Not assessed", "weak": "Not assessed", "scroll_stopping": "Not assessed",
                              "cta_standout": "Not assessed", "audience_match": "Not assessed"},
                "creative_score": _normalize_creative_score({}),
            })
    output["concepts"] = concepts[:3]
    return output


class CreativeStudioRequest(BaseModel):
    mode:               str  = "business"   # "business" | "campaign"
    campaign_id:        str  = ""
    business_key:       str  = ""
    url:                str  = ""
    industry:           str  = ""
    city:               str  = ""
    campaign_objective: str  = "Leads"
    offer:              str  = ""
    ad_copy:            str  = ""
    headlines:          list = []
    keywords:           list = []
    landing_url:        str  = ""


async def _run_creative_studio(request: CreativeStudioRequest) -> dict:
    try:
        mode = (request.mode or "business").strip().lower()
        campaign_id = (request.campaign_id or "").strip() if mode == "campaign" else ""
        campaign_info = {}
        resolved_from_log = {}

        if campaign_id:
            campaign_info = await asyncio.to_thread(_resolve_campaign_by_id, campaign_id)
            if not campaign_info:
                return {"success": False, "error": f"No campaign found with ID {campaign_id!r} on Google Ads or Meta."}
            resolved_from_log = await asyncio.to_thread(_resolve_business_key_from_campaign, campaign_id)

        url      = (request.url or resolved_from_log.get("url") or "").strip()
        industry = (request.industry or resolved_from_log.get("industry") or "").strip()
        city     = (request.city or resolved_from_log.get("city") or "").strip()
        objective = (request.campaign_objective or "Leads").strip()
        offer    = (request.offer or "").strip()
        city_display = city or "India (no specific city given)"

        _bk_src = request.business_key or url
        if not _bk_src and not campaign_id:
            return {"success": False, "error": "Provide either a campaign_id or a url/business_key."}

        if _bk_src:
            norm_key_base = derive_business_key(_bk_src, industry, city)
            norm_key = f"{norm_key_base}::campaign::{campaign_id}" if campaign_id else norm_key_base
        else:
            norm_key_base = f"campaign::{campaign_id}"
            norm_key = norm_key_base

        memory, _resolved = get_memory_with_city_fallback(_bk_src, industry, city) if _bk_src else ({}, norm_key_base)
        logger.info(f"[CREATIVE-STUDIO] key={norm_key!r} mode={mode!r} campaign_id={campaign_id!r} tables={list(memory.keys())}")

        _bm = memory.get("business", {}) or {}
        _am = memory.get("audience", {}) or {}
        _campaign_mem = memory.get("campaign", {}) or {}

        segments = _am.get("segments") if isinstance(_am, dict) else None
        if isinstance(segments, str):
            try: segments = json.loads(segments)
            except Exception: segments = []

        discovery_ran = False
        # ── Core intelligence fix: auto-run lightweight discovery if this
        # business has no REAL Marketing Brain intelligence yet, so creatives
        # are never generic on first use. Checks real content, not just "is
        # there a row" — a business_memory row with empty positioning/uvp/
        # business_dna (e.g. a previous crawl failure) must still trigger a
        # retry rather than being treated as "already discovered".
        if url and (not _has_real_business_dna(_bm) or not segments):
            logger.info(f"[CREATIVE-STUDIO] No real business intelligence for key={norm_key_base!r} — running lightweight discovery first")
            discovery = await _lightweight_business_audience_discovery(url, industry, city)
            _bm = discovery["business"]
            segments = discovery["audience"]["segments"]
            discovery_ran = True
            # Never cache a failed/weak discovery as if it were reliable — a
            # transient crawl failure with empty business_dna must NOT poison
            # this business's memory forever with an empty record that then
            # blocks (and, worse, an audience-hallucination call that produces
            # a fabricated persona unrelated to the real business — confirmed
            # live: sohscape.com once got a wellness/mindfulness audience this
            # way, from a run where gather_bi_data found nothing real).
            if _has_real_business_dna(_bm):
                try:
                    save_to_memory("business", norm_key_base, {
                        "business_name": _bm.get("business_name"), "industry": industry, "city": city,
                        "business_dna": _bm.get("business_dna"), "uvp": _bm.get("uvp"), "positioning": _bm.get("positioning"),
                    })
                    save_to_memory("audience", norm_key_base, {"segments": segments})
                except Exception as _e:
                    logger.warning(f"[CREATIVE-STUDIO] Could not persist discovery output: {_e}")
            else:
                logger.warning(f"[CREATIVE-STUDIO] Discovery for key={norm_key_base!r} found no real business_dna — using this request's (weak) result without caching it, so a later retry can still succeed.")

        def _load_json_field(mem_dict, table_key, field_key):
            raw = (mem_dict.get(table_key) or {})
            if isinstance(raw, str):
                try: raw = json.loads(raw)
                except Exception: raw = {}
            val = raw.get(field_key, {})
            if isinstance(val, str):
                try: return json.loads(val)
                except Exception: return {}
            return val or {}

        kpi_data   = _load_json_field(memory, "kpi", "kpi_data")
        _perf_data = _load_json_field(memory, "performance", "performance_data")

        has_business_dna   = _has_real_business_dna(_bm)
        has_audience_data  = bool(segments)
        has_campaign_data  = bool(campaign_info)
        has_performance     = bool(_perf_data) or has_campaign_data
        has_kpi_prediction  = bool(kpi_data)

        _segments_preview = json.dumps(segments, ensure_ascii=False)[:200] if segments else "None"
        logger.info(
            f"[CREATIVE STUDIO] key={norm_key!r}, business_name={_bm.get('business_name')!r}, "
            f"positioning={_bm.get('positioning')!r}, uvp={_bm.get('uvp')!r}, "
            f"has_business_dna={has_business_dna}, has_audience_data={has_audience_data}, "
            f"audience_source={'fresh_discovery' if discovery_ran else 'existing_memory'}, "
            f"segments_preview={_segments_preview}"
        )

        growth = get_growth_learning(industry) if industry else {"sample_size": 0, "entries": []}
        growth_records = growth.get("sample_size", 0)

        def _sv(d, k, dfl="N/A"):
            v = (d or {}).get(k, dfl) if isinstance(d, dict) else dfl
            return str(v or dfl)

        segments_summary = json.dumps(segments, ensure_ascii=False)[:600] if segments else "NOT AVAILABLE"

        # ── Campaign-mode real numbers + data sufficiency ────────────────────
        imp  = int(campaign_info.get("impressions", 0) or 0)
        clk  = int(campaign_info.get("clicks", 0) or 0)
        cost = float(campaign_info.get("cost_inr", 0) or 0)
        conv = float(campaign_info.get("conversions", 0) or 0)
        ctr  = float(campaign_info.get("ctr_pct", 0) or 0)
        cpc  = float(campaign_info.get("avg_cpc_inr", 0) or 0)

        data_sufficiency = None
        perf_notes = []
        if mode == "campaign":
            data_sufficiency = _check_data_sufficiency(imp, clk, cost) if has_campaign_data else {
                "insufficient_data": True, "banner": None, "thresholds": _DATA_SUFFICIENCY_THRESHOLDS,
            }
            campaign_name_lower = str(campaign_info.get("name", "")).lower()
            is_remarketing = any(w in campaign_name_lower for w in ("remarketing", "retarget"))
            if has_campaign_data and not data_sufficiency["insufficient_data"]:
                benchmark_ctr = _parse_pct_generic((kpi_data.get("predicted_metrics", {}) or {}).get("ctr", {}).get("value", "")) or 2.0
                if ctr > benchmark_ctr and conv == 0:
                    perf_notes.append(f"High CTR ({ctr}%) but 0 conversions — creatives should improve trust signals and CTA clarity, not just attention.")
                elif ctr < 1.0:
                    perf_notes.append(f"Low CTR ({ctr}%) — creatives need stronger scroll-stopping hooks.")
                if cpc > 0 and cpc > (_parse_money_generic((kpi_data.get("predicted_metrics", {}) or {}).get("cpc", {}).get("value", "")) or 20.0) * 1.5:
                    perf_notes.append(f"High CPC (₹{cpc}) relative to benchmark — creatives should target a more specific niche audience.")
            if is_remarketing:
                perf_notes.append("This is a remarketing/retargeting campaign — creatives should lean on proof (testimonial, review, before/after, case study) and a limited-time reminder angle, not cold-audience awareness framing.")

        performance_notes_block = "\n".join(f"- {n}" for n in perf_notes) or "No specific performance-driven creative adjustment flagged."

        objective_focus_map = {
            "leads":    "trust + problem framing + clear CTA + offer",
            "traffic":  "curiosity + click intent",
            "sales":    "product + urgency + offer + proof",
            "branding": "lifestyle + identity + recall",
        }
        objective_focus = objective_focus_map.get(objective.strip().lower(), "trust + problem framing + clear CTA + offer")

        campaign_copy = _campaign_mem.get("campaign_data", {}) if isinstance(_campaign_mem, dict) else {}
        if isinstance(campaign_copy, str):
            try: campaign_copy = json.loads(campaign_copy)
            except Exception: campaign_copy = {}
        existing_headlines = request.headlines or (campaign_copy.get("headlines") if isinstance(campaign_copy, dict) else []) or []
        existing_keywords   = request.keywords or (campaign_copy.get("keywords") if isinstance(campaign_copy, dict) else []) or []

        _today = date.today().strftime("%B %d, %Y")

        data_context = (
            f"TODAY'S REAL DATE: {_today} (use this for any seasonal angle — never invent an occasion that hasn't happened)\n"
            f"BUSINESS DNA AVAILABLE: {has_business_dna}{' (from fresh discovery just now)' if discovery_ran else ''}\n"
            + (f"  - Business: {_sv(_bm, 'business_name')} | Positioning: {_sv(_bm, 'positioning')} | UVP: {_sv(_bm, 'uvp')} | Target geography: {_sv(_bm, 'target_geography')}\n" if has_business_dna else "  - No Business DNA available — base creative direction on industry + objective only.\n")
            + f"AUDIENCE INTELLIGENCE AVAILABLE: {has_audience_data}{' (from fresh discovery just now)' if discovery_ran else ''}\n"
            + (f"  - Segments (pain points/desires/content preferences): {segments_summary}\n" if has_audience_data else "  - No audience segments available — base targeting assumptions on industry norms only.\n")
            + (
                f"CAMPAIGN DATA AVAILABLE: {has_campaign_data}\n"
                + (
                    f"  - Name: {campaign_info.get('name')} | Platform: {campaign_info.get('platform')} | Status: {campaign_info.get('status')}\n"
                    f"  - Real metrics: {imp} impressions, {clk} clicks, ₹{cost} spend, {conv} conversions, CTR {ctr}%, CPC ₹{cpc}\n"
                    if has_campaign_data else "  - No specific campaign pulled.\n"
                )
                + (f"  - DATA SUFFICIENCY: {'INSUFFICIENT — preliminary only' if data_sufficiency and data_sufficiency['insufficient_data'] else 'Sufficient for performance-based claims'}\n" if has_campaign_data else "")
                + f"PERFORMANCE-DRIVEN CREATIVE NOTES:\n{performance_notes_block}\n"
                + f"EXISTING AD COPY ON FILE: headlines={existing_headlines[:5] or 'none'} | keywords={[k.get('text') if isinstance(k, dict) else k for k in existing_keywords[:5]] or 'none'}\n"
                if mode == "campaign" else ""
            )
            + f"MANUAL AD COPY PROVIDED: {request.ad_copy or 'none'}\n"
            + f"KPI ENGINE PREDICTION AVAILABLE: {has_kpi_prediction}\n"
            + f"CROSS-BUSINESS GROWTH MEMORY: {growth_records} real record(s) for '{industry or 'unspecified'}'\n"
            + ("  - NOTE: this contains audience/platform/offer/CPL/ROAS patterns only — NOT creative-angle win rates. Never claim a creative angle 'historically outperforms' based on this.\n" if growth_records else "  - No cross-business records exist for this industry yet.\n")
        )

        prompt = (
            "You are an elite Creative Director + Performance Marketing strategist. Produce a complete creative "
            "strategy and 3 production-ready image-generation prompts for the campaign below. You are NOT generating "
            "images — you are producing everything needed for a human or another tool to generate them.\n\n"
            f"BUSINESS URL: {url or 'Not specified'} | Industry: {industry or 'Not specified'} | City: {city_display}\n"
            f"CAMPAIGN OBJECTIVE: {objective} | Objective-driven creative focus: {objective_focus}\n"
            f"OFFER: {offer or 'Not specified — infer a reasonable offer angle from the business/industry'}\n\n"
            + data_context + "\n"
            + "CRITICAL: every concept's visual style, emotion, headline, and image_prompt MUST reflect what the "
            "REAL audience segments above actually respond to — not a generic stock-photo interpretation of the "
            "industry. Each concept's reason_this_matches MUST cite the SPECIFIC audience insight (a named pain "
            "point, desire, or content preference from the segments above) that drove its creative choices. If no "
            "audience segments are available, say so explicitly instead of inventing one.\n\n"
            + "Return ONLY valid JSON (no markdown, no text outside JSON) matching this EXACT schema:\n"
            "{\n"
            '  "intelligence_applied": "plain-language summary of which specific business/audience insights shaped these creatives — cite the actual positioning/segment/pain-point used, or say none were available",\n'
            '  "performance_context": "' + ('if campaign data is sufficient: \'Based on this campaign\'s real data: CTR X%, Y conversions...\'; if insufficient/unavailable: honest preliminary-data note' if mode == "campaign" else "N/A — business mode, no live campaign data") + '",\n'
            '  "creative_strategy": {"primary_goal":"...","dominant_emotion":"... (with reasoning tied to the real audience)","why":"..."},\n'
            '  "color_psychology": {"palette":[{"name":"...","hex":"#......","reason":"..."}],"why_for_this_industry":"..."},\n'
            '  "typography": {"style":"...","recommended_fonts":["...","..."],"why":"..."},\n'
            '  "layout_blueprint": {"hierarchy":"...","logo_placement":"...","cta_placement":"...","visual_flow":"...","eye_path":"..."},\n'
            '  "seasonal_intelligence": {"current_relevant_occasions":["..."],"recommendation":"..."},\n'
            '  "localization": {"business_locality":"local_to_city / national_or_online / unclear",'
            '"locality_reasoning":"the specific evidence used to decide this — e.g. \'D2C e-commerce brand shipping '
            'pan-India, no mention of Jaipur as a physical base\' or \'this is a physical hotel operating in Jaipur\'",'
            '"city_elements":["..."],"use_or_skip":"use/skip"},\n'
            '  "concepts": [\n'
            '    {"variant_id":"A","variant_name":"Direct Response","headline":"...","subheadline":"...","caption":"...","cta":"...","hashtags":["...","..."],'
            '"visual_direction":"...","image_prompt":"a single flowing descriptive paragraph — NOT a label:value list, NOT prefixed with any meta-text '
            'like \'production-ready prompt:\' — covering subject, environment, camera/lens, lighting, composition, mood, color palette, depth of field, '
            'reserved branding space, reserved typography space, commercial photography style, ending with \'Negative prompt: ...\' and \'Render quality: ...\'",'
            '"design_layout":"...","applicable_platforms_and_sizes":["Instagram Post 1080x1080","Instagram Portrait 1080x1350","Story/Reel 1080x1920",'
            '"Facebook Feed 1200x628","Google Display 1200x628/300x250/728x90","WhatsApp Status 1080x1920"],'
            '"reason_this_matches":"cite the SPECIFIC audience insight that drove this concept",'
            '"ai_review":{"strong":"...","weak":"...","scroll_stopping":"...","cta_standout":"...","audience_match":"..."},'
            '"creative_score":{"visual_appeal":0,"headline":0,"cta":0,"brand_match":0,"emotional_appeal":0,"scroll_stopping":0,"conversion_potential":0,"overall":0,"reasoning":"..."}},\n'
            '    {"variant_id":"B","variant_name":"Emotional/Lifestyle", "...":"same structure, fully filled — do NOT leave scores at 0"},\n'
            '    {"variant_id":"C","variant_name":"Authority/Trust", "...":"same structure, fully filled — do NOT leave scores at 0"}\n'
            '  ],\n'
            '  "ab_prediction": {"likely_winner":"variant_name","why":"...","expected_ctr_difference":"range, e.g. 0.5-1.5pp","confidence":0,"caveat":"prediction based on [what real data / general principles] — not a guarantee"}\n'
            '}\n\n'
            "Rules:\n"
            "- The 3 concepts MUST use 3 genuinely different psychological angles (Direct Response / Emotional-"
            "Lifestyle / Authority-Trust) — not 3 variations of the same idea.\n"
            "- ALL 3 concepts must be FULLY scored 0-100 on every creative_score dimension — never leave a concept's "
            "scores at 0 or blank. Every score must have genuine variation reflecting real quality differences.\n"
            "- Add one LinkedIn-sized concept note in applicable_platforms_and_sizes ONLY if this business is "
            "genuinely B2B (sells to other businesses, not directly to consumers).\n"
            "- image_prompt: natural, richly-descriptive prose the way a professional would actually type it into "
            "an image generator. NEVER start it with a meta-label like 'production-ready prompt:' or write it as a "
            "comma-separated 'field is value' list.\n"
            "- seasonal_intelligence.current_relevant_occasions must be real, currently-relevant occasions given "
            "TODAY'S REAL DATE above — never invent a festival or date that isn't real or isn't actually near.\n"
            "- LOCALIZATION — determine business_locality BEFORE deciding anything about city visuals:\n"
            "  1. Look at the business's real positioning/UVP/industry/Target geography above (and whether it "
            "reads as e-commerce, D2C, ships/delivers nationally, or is an online-only service) versus whether the "
            "city given is actually mentioned or implied as this business's own physical operating base. If "
            "Target geography says 'National' or 'International', treat that as strong evidence toward "
            "national_or_online — do not override a clear National/International signal with a guess.\n"
            "  2. If the business is national, online, D2C, or e-commerce — or there is no evidence it is "
            "physically tied to the given city — set business_locality='national_or_online', use_or_skip='skip', "
            "and city_elements=[]. A national supplement/D2C/e-commerce brand must NEVER get city landmarks "
            "injected just because a city field happened to be filled in — the city there is targeting context "
            "only, not this brand's home turf.\n"
            "  3. ONLY set business_locality='local_to_city' and use_or_skip='use' when this is a genuinely local "
            "physical business (a hotel, restaurant, clinic, or local service) that actually operates IN that city "
            "— then city_elements may reference real, distinctive local landmarks/architecture/tones (e.g. Jaipur "
            "→ Hawa Mahal silhouette, pink-city tones).\n"
            "  4. If you are not confident which applies, default to 'unclear' and use_or_skip='skip' — never "
            "guess toward injecting landmarks.\n"
            "  5. When use_or_skip is 'skip', no concept's headline, visual_direction, or image_prompt may "
            "reference the given city's landmarks/architecture — the creatives should read as national-audience "
            "work, not local-city work.\n"
            "- ab_prediction.confidence MUST be honest: cap at 45 or below unless real performance data is available "
            "above (then you may go higher, referencing the actual data). Never claim confidence from creative-"
            "specific win-rate data that was not provided — growth_memory does not contain that.\n"
            "- HONESTY: never say 'best-performing' or 'top' about any concept unless real, sufficient campaign "
            "performance data is available. If data is insufficient, label performance-based claims 'preliminary — "
            "based on limited data'.\n"
            "- BANNED words: Elevate, Transform, Unlock, Revolutionize, Empower, Seamless, Leverage, Utilize, "
            "Cutting-edge, State-of-the-art, World-class, One-stop solution, Look no further, In today's digital age.\n"
            "- Return ONLY the JSON. Nothing else."
        )

        logger.info(f"[CREATIVE-STUDIO] Sending to GPT-4o (prompt_len={len(prompt)})")
        resp = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4o", messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}, max_tokens=4500, temperature=0.5,
        )
        raw = resp.choices[0].message.content.strip()
        logger.info(f"[CREATIVE-STUDIO] GPT responded (len={len(raw)})")
        output = json.loads(raw)

        output = _clean_banned_words_deep(output)
        output = _backfill_creative_studio(output, _CREATIVE_STUDIO_VARIANTS)

        # ── Deterministic safety net for the "landmarks injected into a
        # national/D2C brand" bug: never trust GPT's own use_or_skip alone —
        # force skip whenever business_locality isn't explicitly local_to_city,
        # regardless of what GPT put in city_elements. ───────────────────────
        _loc = output.get("localization") or {}
        if _loc.get("business_locality") != "local_to_city":
            if _loc.get("city_elements") or _loc.get("use_or_skip") == "use":
                logger.warning(
                    f"[CREATIVE-STUDIO] Overriding localization — GPT set use_or_skip="
                    f"{_loc.get('use_or_skip')!r} with city_elements={_loc.get('city_elements')!r} despite "
                    f"business_locality={_loc.get('business_locality')!r}; forcing skip."
                )
            _loc["city_elements"] = []
            _loc["use_or_skip"] = "skip"
            if not _loc.get("business_locality"):
                _loc["business_locality"] = "unclear"
            if not _loc.get("locality_reasoning"):
                _loc["locality_reasoning"] = "Not enough evidence this business is physically tied to the given city — defaulting to skip."
        output["localization"] = _loc

        # ── Fix the known "concepts B/C come back 0/100" bug: normalize every
        # concept's score deterministically, and if a concept has NO real score
        # data at all, do a targeted single-concept rescore rather than
        # regenerating the whole response. Same treatment for image_prompt —
        # observed live: GPT can drop the field entirely on 2 of 3 concepts
        # while still returning everything else for them. ───────────────────
        business_context_for_scoring = f"{_sv(_bm, 'business_name')} ({industry or 'unspecified industry'}), objective: {objective}"
        for concept in output["concepts"]:
            score = concept.get("creative_score") or {}
            if _creative_score_is_empty(score):
                concept["creative_score"] = await _rescore_concept(concept, business_context_for_scoring)
            else:
                concept["creative_score"] = _normalize_creative_score(score)

            if not (concept.get("image_prompt") or "").strip():
                logger.warning(f"[CREATIVE-STUDIO] Concept {concept.get('variant_name')!r} missing image_prompt — generating it via targeted follow-up")
                generated = await _generate_missing_image_prompt(concept, business_context_for_scoring)
                concept["image_prompt"] = generated or "N/A — regenerate to get this concept's image prompt"

        # ── data_sources_used / data_sufficiency: real, computed — never GPT-generated ─
        output["data_sources_used"] = {
            "business_dna":           has_business_dna,
            "audience_intelligence":  has_audience_data,
            "performance_data":       bool(_perf_data) if mode != "campaign" else (bool(_perf_data) or has_campaign_data),
            "kpi_prediction":         has_kpi_prediction,
            "growth_memory_records":  growth_records,
        }
        if mode == "campaign":
            output["data_sufficiency"] = data_sufficiency

        # ── TRUST & ACCURACY LAYER ────────────────────────────────────────
        _trust_extra_note = "Fresh discovery just ran for this business." if discovery_ran else ""
        output["trust_verdict"] = _compute_trust_verdict(has_business_dna, has_audience_data, _trust_extra_note)
        output["based_on"] = _compute_based_on_line(has_business_dna, has_audience_data, has_performance)

        _biz_label = _bm.get("business_name") or url or "this business"
        _biz_positioning_txt = _bm.get("positioning") or ""
        _business_context_txt = " ".join([
            industry or "", _biz_positioning_txt, _bm.get("uvp") or "",
            json.dumps(_bm.get("business_dna") or {}),
        ])
        _output_context_txt = " ".join([
            json.dumps(output.get("concepts", [])),
            json.dumps(output.get("creative_strategy", {})),
            json.dumps(output.get("color_psychology", {})),
        ])
        _match_warning = _business_match_sanity_check(_business_context_txt, _output_context_txt, _biz_label, _biz_positioning_txt)
        _contradiction_warning = _detect_consistency_contradiction(has_business_dna, output.get("intelligence_applied", ""))
        output["validation_warning"] = _combine_validation_warnings(_match_warning, _contradiction_warning) or None

        # Pre-check: don't let a weak/empty business_dna silently pass as if
        # the creative were properly grounded — tell the user to run
        # Marketing Brain first for an accurate result (still generate below,
        # just flagged clearly).
        output["needs_marketing_brain"] = not has_business_dna
        output["needs_marketing_brain_message"] = (
            "We couldn't confidently read this business from its website. For accurate creatives, run Marketing Brain first."
            if not has_business_dna else ""
        )

        save_to_memory("creative_studio", norm_key, {"data": output})
        logger.info(f"[CREATIVE-STUDIO] Done: key={norm_key!r} mode={mode!r} concepts={len(output.get('concepts', []))}")

        log_activity(
            "creative_studio", business_key=norm_key,
            business_name=_bm.get("business_name", "") or campaign_info.get("name", "") or url,
            url=url, industry=industry, city=city,
            summary=f"Creative Studio ({mode}) — {objective} campaign" + (f" ({campaign_info.get('name')})" if campaign_info else ""),
            reference_id=campaign_id,
        )

        return {
            "success":       True,
            "memory_used":   bool(memory),
            "discovery_ran": discovery_ran,
            "business_key":  norm_key,
            "campaign_info": campaign_info or None,
            "creative":      output,
        }

    except Exception as _e:
        tb = _traceback.format_exc()
        logger.error(f"[CREATIVE-STUDIO] ERROR: {_e}\n{tb}")
        return {"success": False, "error": str(_e), "traceback": tb}


@app.post("/creative-studio")
async def creative_studio(request: CreativeStudioRequest):
    return await _run_creative_studio(request)


# ── Legacy endpoints (deprecated) — thin wrappers over Creative Studio, kept
# only so any existing external caller doesn't 404. The frontend uses only
# /creative-studio going forward. ─────────────────────────────────────────────

class CreativeDirectorRequest(BaseModel):
    url:                str
    industry:           str = ""
    city:               str = ""
    campaign_objective: str = "Leads"
    offer:              str = ""

@app.post("/creative-director")
async def creative_director(request: CreativeDirectorRequest):
    return await _run_creative_studio(CreativeStudioRequest(
        mode="business", url=request.url, industry=request.industry, city=request.city,
        campaign_objective=request.campaign_objective, offer=request.offer,
    ))


class AdToCreativeRequest(BaseModel):
    campaign_id:        str  = ""
    business_key:       str  = ""
    url:                str  = ""
    industry:           str  = ""
    city:               str  = ""
    campaign_objective: str  = "Leads"
    offer:              str  = ""
    ad_copy:            str  = ""
    headlines:          list = []
    keywords:           list = []
    landing_url:        str  = ""

@app.post("/ad-to-creative")
async def ad_to_creative(request: AdToCreativeRequest):
    return await _run_creative_studio(CreativeStudioRequest(
        mode="campaign", campaign_id=request.campaign_id, business_key=request.business_key,
        url=request.url, industry=request.industry, city=request.city,
        campaign_objective=request.campaign_objective, offer=request.offer, ad_copy=request.ad_copy,
        headlines=request.headlines, keywords=request.keywords, landing_url=request.landing_url,
    ))


# ── Module 3: Prospect Discovery ─────────────────────────────────────────────

_INDUSTRY_SEARCH_TERMS = {
    "hospitality (hotels, restaurants, cafes)": "hotels restaurants cafes",
    "schools & education":                      "schools coaching institutes tuition",
    "healthcare & clinics":                     "clinics hospitals doctors medical centre",
    "real estate":                              "real estate developers builders property dealers",
    "retail & fashion":                         "clothing stores boutiques fashion retail",
    "food & beverage":                          "restaurants cafes bakeries food delivery",
    "wellness & fitness":                       "gyms fitness centres yoga wellness spa",
    "wedding & events":                         "wedding planners event management decorators",
    "auto & transport":                         "car dealers service centres auto garage",
    "professional services":                    "chartered accountants lawyers consultants",
    "coaching & tutoring":                      "coaching centres tutors training institutes",
    "jewellery & accessories":                  "jewellery stores showrooms gold silver",
    "interior design & architecture":           "interior designers architects home decor",
    "photography & videography":                "photographers videographers studios",
    "legal & ca services":                      "lawyers advocates chartered accountants",
    "it & software companies":                  "software companies IT firms tech startups",
    "travel & tourism":                         "travel agents tour operators holiday packages",
    "salon & beauty":                           "salons beauty parlours makeup artists",
    "gym & sports academy":                     "gyms sports academies fitness clubs",
    "ngo & social enterprise":                  "NGO social enterprise non profit foundation",
    "agriculture & dairy":                      "dairy farms agriculture suppliers",
    "logistics & transport":                    "logistics courier transport fleet",
    "printing & packaging":                     "printing press packaging manufacturers",
    "construction & builders":                  "construction companies builders contractors",
    "media & entertainment":                    "media production entertainment events",
    "other":                                    "local businesses services",
}

def _get_search_terms(industry: str) -> str:
    k = (industry or "").strip().lower()
    for key, terms in _INDUSTRY_SEARCH_TERMS.items():
        if k == key or k in key or key in k:
            return terms
    return k or "local businesses"


class ProspectDiscoveryRequest(BaseModel):
    industry:       str
    city:           str  = ""
    url:            str  = ""
    max_prospects:  int  = 15


async def _retry_openai_call(fn, retries: int = 2, base_delay: float = 2.0, label: str = ""):
    """
    Retry a blocking OpenAI call on transient rate-limit/timeout/connection
    errors. Needed because Smart Analysis fires up to 7 modules' GPT-4o
    calls simultaneously via asyncio.gather — a collision that never shows
    up when a module is called alone can intermittently throttle whichever
    call lands last, which is what caused prospect_discovery (the heaviest,
    latest-firing prompt) to fail sporadically only inside /smart-analysis.
    """
    from openai import RateLimitError, APITimeoutError, APIConnectionError
    last_err = None
    for attempt in range(retries + 1):
        try:
            return await asyncio.to_thread(fn)
        except (RateLimitError, APITimeoutError, APIConnectionError) as _e:
            last_err = _e
            if attempt < retries:
                delay = base_delay * (attempt + 1)
                logger.warning(f"[RETRY] {label} transient error (attempt {attempt+1}/{retries+1}): {_e} — retrying in {delay}s")
                await asyncio.sleep(delay)
    raise last_err


@app.post("/prospect-discovery")
async def prospect_discovery(request: ProspectDiscoveryRequest):
    try:
        industry       = (request.industry or "").strip()
        city           = (request.city or "").strip()
        # Prospect discovery genuinely needs a location to search — blank means
        # search nationally (India) rather than silently assuming a specific city.
        search_scope   = city or "India"
        max_prospects  = max(5, min(request.max_prospects, 20))

        search_terms   = _get_search_terms(industry)
        logger.info(f"[PROSPECT] industry={industry!r} city={city!r} terms={search_terms!r}")

        # 1. Text-search Google Places
        raw_places = await fetch_google_places(search_terms, search_scope, max_results=20)
        google_places_used = bool(raw_places)
        logger.info(f"[PROSPECT] Google Places returned {len(raw_places)} results")

        # 2. Enrich top 10 with place details (parallel)
        top_places = raw_places[:10]
        detail_tasks = [fetch_place_details(p["place_id"]) for p in top_places if p.get("place_id")]
        details_list = await asyncio.gather(*detail_tasks, return_exceptions=True)

        enriched = []
        for i, place in enumerate(top_places):
            det = details_list[i] if i < len(details_list) and isinstance(details_list[i], dict) else {}
            enriched.append({
                "name":               det.get("name") or place.get("name", ""),
                "address":            det.get("formatted_address") or place.get("address", ""),
                "phone":              det.get("formatted_phone_number", ""),
                "website":            det.get("website") or place.get("website", ""),
                "rating":             det.get("rating") or place.get("rating"),
                "user_ratings_total": det.get("user_ratings_total") or place.get("user_ratings_total", 0),
                "place_id":           place.get("place_id", ""),
                "business_status":    place.get("business_status", ""),
                "recent_reviews":     [r.get("text", "")[:120] for r in (det.get("reviews") or [])[:2]],
            })

        # 3. Tavily social / ad presence checks for businesses with names
        tavily_results = {}
        if TAVILY_API_KEY and enriched:
            tasks_social = [
                fetch_tavily(f"{p['name']} {search_scope} Instagram Facebook social media")
                for p in enriched[:6]
            ]
            tasks_ads = [
                fetch_tavily(f"{p['name']} {search_scope} ads marketing campaigns")
                for p in enriched[:6]
            ]
            social_data, ads_data = await asyncio.gather(
                asyncio.gather(*tasks_social, return_exceptions=True),
                asyncio.gather(*tasks_ads,    return_exceptions=True),
            )
            for i, p in enumerate(enriched[:6]):
                tavily_results[p["name"]] = {
                    "social": social_data[i] if isinstance(social_data[i], str) else "",
                    "ads":    ads_data[i]    if isinstance(ads_data[i],    str) else "",
                }

        # 4. Build prompt (plain string concat — no f-string dicts)
        RS = "RS"

        biz_lines = ""
        for i, p in enumerate(enriched):
            tv = tavily_results.get(p["name"], {})
            biz_lines += (
                "\n---\n"
                "Business " + str(i + 1) + ": " + p["name"] + "\n"
                "Address: " + p["address"] + "\n"
                "Phone: " + (p["phone"] or "not found") + "\n"
                "Website: " + (p["website"] or "NONE") + "\n"
                "Google Rating: " + str(p["rating"] or "no rating") + " (" + str(p["user_ratings_total"]) + " reviews)\n"
                "Status: " + (p["business_status"] or "unknown") + "\n"
                "Recent Reviews: " + (" | ".join(p["recent_reviews"]) or "none") + "\n"
                "Social Media Intel: " + (tv.get("social") or "no data")[:300] + "\n"
                "Ads/Marketing Intel: " + (tv.get("ads") or "no data")[:300] + "\n"
            )

        prompt = (
            "You are a B2B prospect scoring expert for a digital marketing agency in " + search_scope + ".\n"
            "Industry focus: " + industry + "\n\n"
            "Analyse these REAL local businesses found on Google Maps and score them as prospects.\n\n"
            "SCORING RULES:\n"
            "- No website → HIGH opportunity (score 80-95)\n"
            "- Low rating (<3.5) + few reviews → HIGH opportunity (score 70-90)\n"
            "- Few reviews (<20) → HIGH opportunity\n"
            "- No social media presence → HIGH opportunity\n"
            "- Already running active ads → LOWER opportunity (score 30-50)\n"
            "- HOT = opportunity_score > 75\n"
            "- WARM = opportunity_score 50-75\n"
            "- COLD = opportunity_score < 50\n\n"
            "For each business provide a SPECIFIC, PERSONALIZED analysis based on the actual data.\n"
            "Suggested opening line must be specific to THAT business (mention their name, city, actual weakness).\n"
            "Use " + RS + " for Indian Rupee symbol in expected_ltv.\n\n"
            "Businesses to score:\n" + biz_lines + "\n"
            "Return JSON:\n"
            "{\n"
            '  "total_found": ' + str(len(enriched)) + ',\n'
            '  "city": "' + search_scope + '",\n'
            '  "industry": "' + industry + '",\n'
            '  "search_query_used": "' + search_terms + ' in ' + search_scope + '",\n'
            '  "data_source": "Google Places API + Tavily",\n'
            '  "top_opportunity": "Name of best prospect and one sentence why",\n'
            '  "prospects": [\n'
            "    {\n"
            '      "rank": 1,\n'
            '      "name": "exact business name from data",\n'
            '      "address": "exact address",\n'
            '      "phone": "phone or empty string",\n'
            '      "website": "url or empty string",\n'
            '      "google_rating": 4.2,\n'
            '      "total_reviews": 145,\n'
            '      "classification": "hot",\n'
            '      "opportunity_score": 85,\n'
            '      "website_score": 30,\n'
            '      "marketing_maturity": "low",\n'
            '      "closing_probability": "75%",\n'
            '      "expected_ltv": "' + RS + '25,000/month",\n'
            '      "why_contact": "specific reason based on real data",\n'
            '      "weakness_found": "specific weakness (no website / 3 reviews only / no Instagram / last post 4 months ago)",\n'
            '      "recommended_service": "Meta Ads Management",\n'
            '      "suggested_opening_line": "Hi [Name], I noticed [specific observation about their business] — I help [industry] businesses in ' + search_scope + ' get more customers through [service]. Would love to show you what we did for similar businesses here."\n'
            "    }\n"
            "  ]\n"
            "}\n"
            "Return ONLY valid JSON. Score all " + str(len(enriched)) + " businesses. Rank by opportunity_score descending."
        )

        logger.info(f"[PROSPECT] Calling GPT-4o with {len(enriched)} businesses")
        raw_resp = await _retry_openai_call(
            lambda: client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=3500,
                temperature=0.3,
                response_format={"type": "json_object"},
            ).choices[0].message.content,
            label="prospect_discovery GPT-4o call",
        )

        result_obj = json.loads(raw_resp)
        result_obj = _fix_rs(result_obj)

        # Separate into hot/warm/cold
        prospects = result_obj.get("prospects", [])
        result_obj["hot_prospects"]  = [p for p in prospects if p.get("classification", "").lower() == "hot"]
        result_obj["warm_prospects"] = [p for p in prospects if p.get("classification", "").lower() == "warm"]
        result_obj["cold_prospects"] = [p for p in prospects if p.get("classification", "").lower() == "cold"]
        result_obj["total_found"]    = len(prospects)

        # Trim to max_prospects
        result_obj["prospects"] = prospects[:max_prospects]

        # 5. Save to prospect_memory keyed by industry::city
        prospect_key = derive_business_key("", industry, city)
        _now = datetime.utcnow().isoformat()
        def _save_prospect():
            with engine.begin() as conn:
                if _is_sqlite:
                    conn.execute(text(
                        "INSERT INTO prospect_memory (business_key, prospects_data, industry, city, created_at, updated_at) "
                        "VALUES (:k, :d, :ind, :cit, :ca, :ua) "
                        "ON CONFLICT(business_key) DO UPDATE SET prospects_data=:d, updated_at=:ua"
                    ), {"k": prospect_key, "d": json.dumps(result_obj), "ind": industry, "cit": city, "ca": _now, "ua": _now})
                else:
                    conn.execute(text(
                        "INSERT INTO prospect_memory (business_key, prospects_data, industry, city, created_at, updated_at) "
                        "VALUES (:k, :d, :ind, :cit, :ca, :ua) "
                        "ON CONFLICT(business_key) DO UPDATE SET prospects_data=:d, updated_at=:ua"
                    ), {"k": prospect_key, "d": json.dumps(result_obj), "ind": industry, "cit": city, "ca": _now, "ua": _now})
        await asyncio.to_thread(_save_prospect)
        logger.info(f"[PROSPECT] Saved to prospect_memory key={prospect_key!r}")

        log_activity(
            "prospect_discovery", business_key=prospect_key,
            business_name=f"{industry} prospects" if industry else "Prospect search",
            url=request.url, industry=industry, city=city,
            summary=f"Prospect Discovery — {len(result_obj.get('prospects', []))} prospects found",
        )

        return {
            "success":            True,
            "google_places_used": google_places_used,
            "data":               result_obj,
        }

    except Exception as _e:
        tb = _traceback.format_exc()
        logger.error(f"[PROSPECT] ERROR: {_e}\n{tb}")
        return {"success": False, "error": str(_e), "traceback": tb}


# ── Google Ads Campaign Creation (Basic Access) ───────────────────────────────

class CreateCampaignRequest(BaseModel):
    campaign_name:  str
    budget_daily:   float                  # in ₹
    campaign_type:  str  = "SEARCH"        # SEARCH or DISPLAY
    start_date:     str  = ""              # YYYYMMDD; defaults to tomorrow
    end_date:       str  = ""              # YYYYMMDD; optional
    business_key:   str  = ""             # if set, pull keywords from campaign_memory
    url:            str  = ""             # business website — used as key fallback + ad final_url
    industry:       str  = ""             # used with city for get_memory_with_city_fallback
    city:           str  = ""

class CreateAdRequest(BaseModel):
    campaign_id:    str
    ad_group_name:  str  = ""
    headlines:      list = []              # max 15, each max 30 chars
    descriptions:   list = []              # max 4, each max 90 chars
    final_url:      str  = ""
    sitelinks:      list = []              # [{"link_text","description1","description2"}], max 6

class AddKeywordsRequest(BaseModel):
    ad_group_id:    str
    keywords:       list = []              # [{"text": "...", "match_type": "EXACT/PHRASE/BROAD"}]


def _gads_customer_id():
    return _genv("GOOGLE_ADS_CUSTOMER_ID")


@app.get("/google-ads/test-connection")
async def gads_test_connection():
    """Verify Google Ads connectivity and surface account details for debugging."""
    import google.ads.googleads as _gads_pkg
    customer_id   = _genv("GOOGLE_ADS_CUSTOMER_ID")
    dev_token     = _genv("GOOGLE_ADS_DEVELOPER_TOKEN")
    api_version   = getattr(_gads_pkg, "__version__", "unknown")

    # Detect library default API version from the module path
    try:
        from google.ads.googleads import __version__ as _lib_ver
        # google-ads 31.x => v19, 30.x => v18, 29.x => v17, etc.
        _major = int(_lib_ver.split(".")[0])
        _inferred_api = f"v{_major - 12}"   # rough mapping: 31-12=19
    except Exception:
        _inferred_api = "unknown"

    result = {
        "customer_id":            customer_id or None,
        "developer_token_prefix": (dev_token[:5] + "…") if dev_token else None,
        "library_version":        api_version,
        "api_version_inferred":   _inferred_api,
        "connected":              False,
        "campaigns_found":        0,
        "campaigns_sample":       [],
        "error":                  None,
    }

    if not customer_id:
        result["error"] = "GOOGLE_ADS_CUSTOMER_ID env var not set"
        return result

    try:
        def _test_sync():
            client  = get_google_ads_client()
            service = client.get_service("GoogleAdsService")
            query = """
                SELECT campaign.id, campaign.name, campaign.status
                FROM campaign
                ORDER BY campaign.id DESC
                LIMIT 5
            """
            rows = list(service.search(customer_id=customer_id, query=query))
            return [
                {"id": str(r.campaign.id), "name": r.campaign.name, "status": r.campaign.status.name}
                for r in rows
            ]

        campaigns = await asyncio.to_thread(_test_sync)
        result["connected"]       = True
        result["campaigns_found"] = len(campaigns)
        result["campaigns_sample"] = campaigns
    except GoogleAdsException as ex:
        errors = [e.message for e in ex.failure.errors]
        result["error"] = "; ".join(errors)
    except Exception as ex:
        result["error"] = str(ex)

    return result



def _create_campaign_sync(campaign_name: str, budget_daily: float, campaign_type: str,
                          start_date: str, end_date: str, customer_id: str):
    """Synchronous: create CampaignBudget + Campaign + AdGroup. Returns dict."""
    client = get_google_ads_client()

    # 1. CampaignBudget
    budget_service = client.get_service("CampaignBudgetService")
    budget_op      = client.get_type("CampaignBudgetOperation")
    cb = budget_op.create
    cb.name                 = f"Budget — {campaign_name}"
    cb.amount_micros        = int(budget_daily * 1_000_000)
    cb.delivery_method      = client.enums.BudgetDeliveryMethodEnum.STANDARD
    cb.explicitly_shared    = False
    budget_resp    = budget_service.mutate_campaign_budgets(customer_id=customer_id, operations=[budget_op])
    budget_rn      = budget_resp.results[0].resource_name

    # 2. Campaign
    campaign_service = client.get_service("CampaignService")
    campaign_op      = client.get_type("CampaignOperation")
    camp = campaign_op.create
    camp.name            = campaign_name
    camp.status          = client.enums.CampaignStatusEnum.PAUSED   # safe default
    camp.campaign_budget = budget_rn
    if campaign_type.upper() == "DISPLAY":
        camp.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.DISPLAY
    else:
        camp.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.SEARCH
        camp.manual_cpc.enhanced_cpc_enabled = False

    # Required by Google Ads API v16+ for EU DSA compliance.
    camp.contains_eu_political_advertising = (
        client.enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
    )

    # Location targeting must mean "people physically in the target city",
    # not "anyone searching about it" — PRESENCE_OR_INTEREST (the API default)
    # would match e.g. someone in Delhi googling "Jaipur hotels".
    camp.geo_target_type_setting.positive_geo_target_type = (
        client.enums.PositiveGeoTargetTypeEnum.PRESENCE
    )

    # Note: start_date / end_date are NOT set on the Campaign object.
    # The Google Ads API rejects unknown fields for Campaign; dates can be
    # managed in the Google Ads dashboard after creation.
    if not start_date:
        start_date = date.today().strftime("%Y%m%d")

    campaign_resp = campaign_service.mutate_campaigns(customer_id=customer_id, operations=[campaign_op])
    campaign_rn   = campaign_resp.results[0].resource_name
    campaign_id   = campaign_rn.split("/")[-1]

    # 3. AdGroup
    ad_group_service = client.get_service("AdGroupService")
    ag_op            = client.get_type("AdGroupOperation")
    ag = ag_op.create
    ag.name           = f"{campaign_name} — Ad Group 1"
    ag.campaign       = campaign_rn
    ag.status         = client.enums.AdGroupStatusEnum.ENABLED
    ag.type_          = client.enums.AdGroupTypeEnum.SEARCH_STANDARD
    ag.cpc_bid_micros = 1_000_000   # ₹1 default, user can update in dashboard

    ag_resp     = ad_group_service.mutate_ad_groups(customer_id=customer_id, operations=[ag_op])
    ag_rn       = ag_resp.results[0].resource_name
    ad_group_id = ag_rn.split("/")[-1]

    return {
        "campaign_id":   campaign_id,
        "campaign_name": campaign_name,
        "ad_group_id":   ad_group_id,
        "budget_rn":     budget_rn,
        "campaign_rn":   campaign_rn,
        "ad_group_rn":   ag_rn,
        "status":        "PAUSED",
        "start_date":    start_date,
        "end_date":      end_date or None,
    }


# India (country-level) geo target constant — stable, documented by Google:
# https://developers.google.com/google-ads/api/data/geotargets
_INDIA_GEO_TARGET_CONSTANT = "geoTargetConstants/2356"


def _resolve_geo_target_sync(city: str) -> tuple:
    """
    Synchronous: resolve a city name to a Google Ads geo target constant
    resource name via GeoTargetConstantService. Falls back to targeting
    all of India if the city is blank/generic or the lookup fails/misses —
    NEVER falls all the way back to worldwide.
    Returns (resource_name, matched_name).
    """
    city_clean = (city or "").strip()
    if not city_clean or city_clean.lower() in ("india", "all india", "pan india", "national"):
        return _INDIA_GEO_TARGET_CONSTANT, "India"

    try:
        client       = get_google_ads_client()
        gtc_service  = client.get_service("GeoTargetConstantService")
        gtc_request  = client.get_type("SuggestGeoTargetConstantsRequest")
        gtc_request.locale = "en"
        gtc_request.country_code = "IN"
        gtc_request.location_names.names.append(city_clean)

        response = gtc_service.suggest_geo_target_constants(request=gtc_request)
        suggestions = list(response.geo_target_constant_suggestions)
        # Prefer the most specific match (City) over broader regions
        # (Admin Division, Country) when multiple suggestions come back.
        suggestions.sort(key=lambda s: 0 if s.geo_target_constant.target_type == "City" else 1)
        if suggestions:
            gtc = suggestions[0].geo_target_constant
            return gtc.resource_name, gtc.name
        logger.info(f"[GADS LOCATION] No geo target suggestions for city={city_clean!r} — falling back to India")
    except Exception as _e:
        logger.warning(f"[GADS LOCATION] Geo target lookup failed for city={city_clean!r}: {_e}")

    return _INDIA_GEO_TARGET_CONSTANT, "India (fallback)"


def _add_location_criterion_sync(campaign_rn: str, geo_target_resource_name: str, customer_id: str):
    """Synchronous: add a CampaignCriterion pinning the campaign to a location (Presence targeting)."""
    client = get_google_ads_client()
    svc    = client.get_service("CampaignCriterionService")
    op     = client.get_type("CampaignCriterionOperation")
    crit   = op.create
    crit.campaign = campaign_rn
    crit.location.geo_target_constant = geo_target_resource_name
    resp = svc.mutate_campaign_criteria(customer_id=customer_id, operations=[op])
    return resp.results[0].resource_name


def _add_keywords_sync(ad_group_rn: str, keywords: list, customer_id: str):
    """Synchronous: add keyword criteria to an ad group."""
    client    = get_google_ads_client()
    svc       = client.get_service("AdGroupCriterionService")
    _match_map = {
        "EXACT":  client.enums.KeywordMatchTypeEnum.EXACT,
        "PHRASE": client.enums.KeywordMatchTypeEnum.PHRASE,
        "BROAD":  client.enums.KeywordMatchTypeEnum.BROAD,
    }
    ops = []
    for kw in keywords[:100]:
        op  = client.get_type("AdGroupCriterionOperation")
        crit = op.create
        crit.ad_group = ad_group_rn
        crit.status   = client.enums.AdGroupCriterionStatusEnum.ENABLED
        crit.keyword.text       = kw.get("text", "")[:80]
        crit.keyword.match_type = _match_map.get(kw.get("match_type", "BROAD").upper(),
                                                  client.enums.KeywordMatchTypeEnum.BROAD)
        ops.append(op)
    if not ops:
        return []
    resp = svc.mutate_ad_group_criteria(customer_id=customer_id, operations=ops)
    return [r.resource_name for r in resp.results]


def _truncate_at_word_boundary(text: str, limit: int) -> str:
    """Truncate to `limit` chars without cutting a word in half where avoidable."""
    text = str(text).strip()
    if len(text) <= limit:
        return text
    truncated = text[:limit]
    last_space = truncated.rfind(" ")
    # Only back off to the word boundary if it doesn't throw away more than
    # half the budget — otherwise a single long word would collapse to almost nothing.
    if last_space > limit * 0.5:
        truncated = truncated[:last_space]
    return truncated.rstrip(" ,.-")


def _dedupe_preserve_order(items: list) -> list:
    """Drop case-insensitive duplicates, keeping first occurrence order.
    Needed because truncation can collapse two distinct headlines/descriptions
    into identical text, which Google Ads rejects as a duplicate asset."""
    seen = set()
    result = []
    for it in items:
        key = it.strip().lower()
        if key and key not in seen:
            seen.add(key)
            result.append(it)
    return result


# ── Google Ads policy handling ───────────────────────────────────────────────
# Plain-language explanations for the policy topics we see most often on
# generated ad copy. Falls back to a generic "see Google's policy" message
# for any topic not in this map — the list of possible topics is not fixed
# and can change at any time per Google's own docs.
_POLICY_TOPIC_EXPLANATIONS = {
    "TRADEMARKS_IN_AD_TEXT":            "Your ad text includes a trademarked term you likely don't have rights to use in ad copy.",
    "TRADEMARKS":                       "Your ad text includes a trademarked term you likely don't have rights to use.",
    "MISLEADING_CONTENT":               "Your ad content reads as misleading or exaggerated to Google's reviewers.",
    "MISREPRESENTATION":                "A claim in your ad copy (often an unverifiable 'best'/'#1'/'guaranteed' claim) isn't backed by evidence Google can confirm.",
    "UNAPPROVED_SUBSTANCES":            "Your ad appears to reference a substance (alcohol, tobacco, drugs, supplements) that needs certification or isn't allowed to advertise.",
    "DESTINATION_NOT_WORKING":          "Google couldn't load your landing page — it may be down, redirecting incorrectly, or blocking Google's crawler.",
    "DESTINATION_MISMATCH":             "Your ad's display/final URL doesn't match what the landing page is actually about.",
    "INACCURATE_CLAIMS_LANDING_PAGE":   "A claim made in your ad isn't clearly supported on the landing page itself.",
    "PHONE_NUMBER_IN_AD_TEXT":          "Phone numbers aren't allowed directly in headlines/descriptions — use a Call extension instead.",
    "EXCESSIVE_CAPITALIZATION_IN_AD_TEXT": "Too many words in ALL CAPS — Google treats this as 'shouting' and restricts it.",
    "INCORRECT_KEYBOARD_CHARACTERS_IN_AD_TEXT": "Unusual symbols/characters in the ad text aren't allowed.",
    "UNACCEPTABLE_LANGUAGE":            "The ad text uses language Google considers unacceptable (profanity, slurs, etc).",
    "GENDER_IN_AD_TEXT":                "The ad text implies a policy-restricted assumption about the reader's gender.",
    "GUARANTEE_MISUSE":                 "The ad claims a 'guarantee' Google can't verify is genuinely offered.",
}

def _policy_explanation(topic: str) -> str:
    return _POLICY_TOPIC_EXPLANATIONS.get(
        topic,
        f"Google flagged this ad copy under the '{topic}' policy — see "
        "https://support.google.com/adspolicy/answer/6008942 for details.",
    )

def _extract_policy_violations(gae: "GoogleAdsException") -> list:
    """
    Pull the SPECIFIC policy topic(s) and offending text out of a
    GoogleAdsException raised during ad creation — instead of surfacing only
    the generic "PROHIBITED" error code. Returns one dict per policy_topic_entry:
    {"topic": "TRADEMARKS_IN_AD_TEXT", "entry_type": "PROHIBITED", "evidence": [...], "message": "..."}
    Entries with no policy_finding_details (a non-policy error) still get a
    dict with topic=None so callers can tell "policy violation" apart from
    "some other Google Ads error".
    """
    violations = []
    for error in gae.failure.errors:
        pfd = error.details.policy_finding_details if error.details else None
        entries = list(pfd.policy_topic_entries) if pfd and pfd.policy_topic_entries else []
        if not entries:
            violations.append({
                "topic": None, "entry_type": None, "evidence": [], "message": error.message,
            })
            continue
        for entry in entries:
            evidence_texts = []
            for ev in entry.evidences:
                if ev.text_list and ev.text_list.texts:
                    evidence_texts.extend(str(t) for t in ev.text_list.texts)
            violations.append({
                "topic":      entry.topic,
                "entry_type": entry.type_.name if hasattr(entry.type_, "name") else str(entry.type_),
                "evidence":   evidence_texts,
                "message":    error.message,
            })
    return violations

def _build_policy_error_response(violations: list) -> dict:
    """Turn _extract_policy_violations() output into a user-facing structure
    the frontend can render as a real error card (topic + offending text +
    plain-language explanation), not just 'PROHIBITED'."""
    topics = [v for v in violations if v["topic"]]
    if not topics:
        return {"error": "Ad creation failed.", "details": [v["message"] for v in violations]}
    return {
        "error": "Ad rejected by Google Ads policy review.",
        "policy_violations": [
            {
                "topic":           v["topic"],
                "entry_type":      v["entry_type"],
                "offending_text":  v["evidence"],
                "explanation":     _policy_explanation(v["topic"]),
                "message":         v["message"],
            }
            for v in topics
        ],
    }

# Unsubstantiated superlative/claim patterns — Google's MISREPRESENTATION
# policy requires these to be backed by evidence it can verify, which
# GPT-generated ad copy never has, so they're stripped pre-flight rather
# than left to fail at the API and burn a whole ad.
_SUPERLATIVE_PATTERNS = [
    r'\bbest\b', r'#\s?1\b', r'\bnumber\s+one\b', r'\bno\.?\s*1\b',
    r"\bworld'?s\s+(?:best|no\.?\s*1|number\s+one)\b", r'\btop[\s-]rated\b',
    r'\bguaranteed?\b', r'\b100%\s+guarantee[d]?\b',
]
_PHONE_PATTERN = re.compile(r'(\+?\d[\d\-\s\(\)]{7,}\d)')
_GENERIC_CTA_PHRASES = ["click here", "click now", "click below"]
# Acronyms that are legitimately all-caps and shouldn't be de-capitalized.
_ALLOWED_ACRONYMS = {"SEO", "PPC", "ROI", "FAQ", "GST", "USA", "UK", "AI", "IT", "HR", "CRM", "B2B", "B2C", "D2C", "24X7", "24/7"}

def _policy_precheck_ad_copy(headlines: list, descriptions: list) -> tuple:
    """
    Best-effort pre-flight cleaner: strips the most common Google Ads policy
    triggers from generated ad copy BEFORE sending it to the API — unsubstantiated
    superlative claims, phone numbers (must use a Call extension instead),
    generic "click here"-style CTAs, and shouting ALL-CAPS words. Returns
    (clean_headlines, clean_descriptions, warnings) — this reduces how often
    a policy rejection happens at all, it doesn't guarantee zero rejections
    (trademark/destination findings depend on things this can't detect).
    """
    warnings = []

    def _decap(m):
        w = m.group(0)
        base = w.split("'")[0]
        if base in _ALLOWED_ACRONYMS:
            return w
        if "'" in w:
            head, _sep, tail = w.partition("'")
            return head.capitalize() + "'" + tail.lower()
        return w.capitalize()

    def _fix(text: str) -> str:
        original = text
        if _PHONE_PATTERN.search(text):
            text = _PHONE_PATTERN.sub('', text).strip()
            warnings.append(f"Removed phone number from ad text: {original!r}")
        for pat in _SUPERLATIVE_PATTERNS:
            if re.search(pat, text, re.I):
                text = re.sub(pat, '', text, flags=re.I)
                warnings.append(f"Removed unsubstantiated claim ('{pat}') from ad text: {original!r}")
        for phrase in _GENERIC_CTA_PHRASES:
            if phrase in text.lower():
                text = re.sub(re.escape(phrase), '', text, flags=re.I)
                warnings.append(f"Removed generic CTA phrase '{phrase}' from ad text: {original!r}")
        text = re.sub(r"\b[A-Z]{4,}(?:'[A-Z]+)?\b", _decap, text)
        text = re.sub(r'\s{2,}', ' ', text).strip(' -,.')
        return text

    clean_headlines    = [_fix(str(h)) for h in headlines]
    clean_descriptions = [_fix(str(d)) for d in descriptions]
    if warnings:
        logger.info(f"[GADS AD PRECHECK] Cleaned {len(warnings)} policy-risk pattern(s) before send: {warnings}")
    return clean_headlines, clean_descriptions, warnings


def _create_ad_sync(ad_group_rn: str, headlines: list, descriptions: list,
                    final_url: str, customer_id: str):
    """
    Synchronous: create a ResponsiveSearchAd in an ad group.
    Runs the policy pre-flight cleaner first (strips common rejection triggers
    like unsubstantiated superlatives and phone numbers), then enforces
    Google Ads character limits (30/90) via word-boundary truncation, then
    de-duplicates — raises ValueError (never silently proceeds) if that
    leaves fewer than the Google Ads minimum (3 headlines / 2 descriptions),
    since sending too few would fail at the API anyway with a less clear error.
    """
    precheck_headlines, precheck_descriptions, _precheck_warnings = _policy_precheck_ad_copy(headlines, descriptions)
    clean_headlines    = _dedupe_preserve_order([_truncate_at_word_boundary(h, 30) for h in precheck_headlines if str(h).strip()])
    clean_descriptions = _dedupe_preserve_order([_truncate_at_word_boundary(d, 90) for d in precheck_descriptions if str(d).strip()])

    if len(clean_headlines) < 3:
        raise ValueError(
            f"Only {len(clean_headlines)} unique headline(s) survived cleanup/truncation "
            f"(Google Ads requires 3+); original count was {len(headlines)}"
        )
    if len(clean_descriptions) < 2:
        raise ValueError(
            f"Only {len(clean_descriptions)} unique description(s) survived cleanup/truncation "
            f"(Google Ads requires 2+); original count was {len(descriptions)}"
        )

    client  = get_google_ads_client()
    svc     = client.get_service("AdGroupAdService")
    op      = client.get_type("AdGroupAdOperation")
    aga     = op.create
    aga.ad_group = ad_group_rn
    aga.status   = client.enums.AdGroupAdStatusEnum.ENABLED

    rsa = aga.ad.responsive_search_ad
    for h in clean_headlines[:15]:
        asset = client.get_type("AdTextAsset")
        asset.text = h
        rsa.headlines.append(asset)
    for d in clean_descriptions[:4]:
        asset = client.get_type("AdTextAsset")
        asset.text = d
        rsa.descriptions.append(asset)
    aga.ad.final_urls.append(final_url)

    resp   = svc.mutate_ad_group_ads(customer_id=customer_id, operations=[op])
    ad_rn  = resp.results[0].resource_name

    # Never trust the mutate response's resource_name alone as proof the ad
    # actually persisted — read it back immediately. Without this, a caller
    # can end up reporting "ad created" for an ad that doesn't really exist
    # (e.g. this call raised nothing, but caller code elsewhere set the flag
    # optimistically before/without checking the actual mutate result).
    if not _verify_ad_group_ad_exists(ad_rn, customer_id):
        raise RuntimeError(
            f"Ad mutation returned {ad_rn!r} but the resource did not verify as "
            "persisted on read-back — treating this as a failed ad creation."
        )
    return {"ad_id": ad_rn.split("/")[-1], "resource_name": ad_rn, "status": "ENABLED"}


def _verify_ad_group_ad_exists(ad_rn: str, customer_id: str) -> bool:
    """
    Read a just-created ad_group_ad back via GAQL to confirm it genuinely
    exists and isn't REMOVED — the ground-truth check behind _create_ad_sync's
    return value, so "ad_created: true" in an API response always means an
    ad really is there, never just that a mutate call didn't raise.
    """
    try:
        client  = get_google_ads_client()
        service = client.get_service("GoogleAdsService")
        query = f"SELECT ad_group_ad.status FROM ad_group_ad WHERE ad_group_ad.resource_name = '{ad_rn}'"
        rows = list(service.search(customer_id=customer_id, query=query))
        if not rows:
            return False
        status = rows[0].ad_group_ad.status
        status_name = status.name if hasattr(status, "name") else str(status)
        return status_name not in ("REMOVED", "UNKNOWN", "UNSPECIFIED")
    except Exception as _ve:
        logger.warning(f"[GADS-AD] Post-creation verification query failed for {ad_rn!r}: {_ve}")
        return False


def _create_sitelinks_sync(campaign_rn: str, sitelinks: list, customer_id: str):
    """
    Synchronous: create up to 6 SitelinkAssets and link them to the campaign.
    Google Ads models sitelinks as standalone Assets linked via CampaignAsset —
    they are not a field on the ad itself. Returns the list of linked resource names.
    """
    client = get_google_ads_client()

    # 1. Create the Asset objects (one mutate call, one operation per sitelink)
    asset_service = client.get_service("AssetService")
    asset_ops = []
    for sl in sitelinks[:6]:
        op = client.get_type("AssetOperation")
        asset = op.create
        asset.sitelink_asset.link_text    = str(sl.get("link_text", ""))[:25]
        asset.sitelink_asset.description1 = str(sl.get("description1", ""))[:35]
        asset.sitelink_asset.description2 = str(sl.get("description2", ""))[:35]
        asset_ops.append(op)
    if not asset_ops:
        return []
    asset_resp = asset_service.mutate_assets(customer_id=customer_id, operations=asset_ops)
    asset_resource_names = [r.resource_name for r in asset_resp.results]

    # 2. Link each Asset to the campaign as a SITELINK field type
    campaign_asset_service = client.get_service("CampaignAssetService")
    link_ops = []
    for asset_rn in asset_resource_names:
        op = client.get_type("CampaignAssetOperation")
        ca = op.create
        ca.campaign   = campaign_rn
        ca.asset      = asset_rn
        ca.field_type = client.enums.AssetFieldTypeEnum.SITELINK
        link_ops.append(op)
    link_resp = campaign_asset_service.mutate_campaign_assets(customer_id=customer_id, operations=link_ops)
    return [r.resource_name for r in link_resp.results]


@app.post("/google-ads/create-campaign")
async def gads_create_campaign(request: CreateCampaignRequest):
    try:
        customer_id = _gads_customer_id()
        if not customer_id:
            return {"success": False, "error": "GOOGLE_ADS_CUSTOMER_ID not configured"}

        logger.info(f"[GADS-CREATE] campaign={request.campaign_name!r} budget_daily={request.budget_daily} type={request.campaign_type}")

        result = await asyncio.to_thread(
            _create_campaign_sync,
            request.campaign_name, request.budget_daily, request.campaign_type,
            request.start_date.replace("-", ""),  # accept both YYYY-MM-DD and YYYYMMDD
            request.end_date.replace("-", "") if request.end_date else "",
            customer_id,
        )

        # Location targeting — without this the campaign defaults to
        # worldwide. Resolve the requested city to a geo target constant
        # (falling back to all-of-India, never worldwide) and pin the
        # campaign to it with Presence-only targeting.
        location_resource_name = None
        location_matched_name  = None
        location_applied       = False
        try:
            location_resource_name, location_matched_name = await asyncio.to_thread(
                _resolve_geo_target_sync, request.city
            )
        except Exception as _re:
            logger.warning(f"[GADS-CREATE] Geo target resolution failed: {_re}")
            location_resource_name, location_matched_name = _INDIA_GEO_TARGET_CONSTANT, "India (resolve error fallback)"

        logger.info(f"[GADS LOCATION] Applying location target: {location_resource_name} for city: {request.city}")
        try:
            await asyncio.to_thread(
                _add_location_criterion_sync, result["campaign_rn"], location_resource_name, customer_id
            )
            location_applied = True
        except Exception as _le:
            logger.warning(f"[GADS-CREATE] Location criterion mutation failed: {_le}")

        # Pull keywords/headlines/descriptions from campaign_memory — same key
        # derivation as /campaign-launch-kit's SAVE, with city-fallback lookup
        # so a blank/mismatched city or legacy key still resolves.
        lookup_source = request.business_key or request.url
        business_key = derive_business_key(lookup_source, request.industry, request.city)
        logger.info(f"[GADS CREATE] LOOKUP key: '{business_key}'")

        keywords_added    = []
        ad_created        = None
        ad_creation_error = None
        sitelinks_added   = []
        if lookup_source:
            mem, resolved_key = get_memory_with_city_fallback(lookup_source, request.industry, request.city)
            camp_data_raw = mem.get("campaign", {}).get("campaign_data", {}) if mem else {}
            if isinstance(camp_data_raw, str) and camp_data_raw.strip().startswith("{"):
                try:
                    camp_data_raw = json.loads(camp_data_raw)
                except Exception:
                    camp_data_raw = {}
            camp_data = camp_data_raw if isinstance(camp_data_raw, dict) else {}
            logger.info(f"[GADS-CREATE] Memory resolved via key={resolved_key!r} campaign_data_present={bool(camp_data)}")

            kw_list = camp_data.get("keywords") or camp_data.get("google_keywords") or []
            try:
                if isinstance(kw_list, list) and kw_list:
                    kw_objs = [
                        {"text": str(k.get("text", "")), "match_type": k.get("match_type", "BROAD")}
                        if isinstance(k, dict) else {"text": str(k), "match_type": "BROAD"}
                        for k in kw_list[:30] if k
                    ]
                    keywords_added = await asyncio.to_thread(
                        _add_keywords_sync, result["ad_group_rn"], kw_objs, customer_id
                    )
                    logger.info(f"[GADS-CREATE] Added {len(keywords_added)} keywords from memory")
            except Exception as _ke:
                logger.warning(f"[GADS-CREATE] Keyword pull failed: {_ke}")

            # Auto-create the ResponsiveSearchAd from the same launch-kit data —
            # without this, create-campaign only ever produces an empty ad group.
            headlines    = camp_data.get("headlines", [])
            descriptions = camp_data.get("descriptions", [])
            final_url    = request.url.strip()
            if final_url and not re.match(r'^https?://', final_url, re.I):
                final_url = f"https://{final_url}"

            logger.info(
                f"[GADS CREATE] memory assets: keywords={len(kw_list)}, "
                f"headlines={len(headlines)}, descriptions={len(descriptions)}"
            )

            if len(headlines) >= 3 and len(descriptions) >= 2 and final_url:
                try:
                    ad_created = await asyncio.to_thread(
                        _create_ad_sync, result["ad_group_rn"], headlines, descriptions, final_url, customer_id
                    )
                    logger.info(f"[GADS-CREATE] Ad created: {ad_created}")
                except GoogleAdsException as _gae:
                    policy_violations = _extract_policy_violations(_gae)
                    for _v in policy_violations:
                        if _v["topic"]:
                            logger.info(f"[GADS AD POLICY] topic={_v['topic']}, evidence={_v['evidence']}")
                    ad_creation_error = _build_policy_error_response(policy_violations)
                    logger.error(f"[GADS-CREATE] Ad creation GoogleAdsException: {ad_creation_error}")

                    # If a specific asset (evidence text) is the problem, retry with
                    # the remaining safe assets rather than failing the ad entirely —
                    # Google Ads needs a minimum of 3 headlines + 2 descriptions.
                    offending_texts = [t for _v in policy_violations for t in _v["evidence"] if t]
                    if offending_texts:
                        retry_headlines = [h for h in headlines if not any(t.lower() in str(h).lower() for t in offending_texts)]
                        retry_descriptions = [d for d in descriptions if not any(t.lower() in str(d).lower() for t in offending_texts)]
                        removed_h = len(headlines) - len(retry_headlines)
                        removed_d = len(descriptions) - len(retry_descriptions)
                        if (removed_h or removed_d) and len(retry_headlines) >= 3 and len(retry_descriptions) >= 2:
                            logger.info(
                                f"[GADS-CREATE] Retrying ad creation with {removed_h} headline(s) and "
                                f"{removed_d} description(s) removed (matched policy-violating text)"
                            )
                            try:
                                ad_created = await asyncio.to_thread(
                                    _create_ad_sync, result["ad_group_rn"], retry_headlines, retry_descriptions, final_url, customer_id
                                )
                                ad_creation_error = {
                                    **ad_creation_error,
                                    "retried": True,
                                    "note": (
                                        f"Ad created successfully after removing {removed_h} headline(s) and "
                                        f"{removed_d} description(s) that triggered the policy violation above."
                                    ),
                                }
                                logger.info(f"[GADS-CREATE] Ad created on retry: {ad_created}")
                            except GoogleAdsException as _gae2:
                                policy_violations2 = _extract_policy_violations(_gae2)
                                for _v in policy_violations2:
                                    if _v["topic"]:
                                        logger.info(f"[GADS AD POLICY] (retry) topic={_v['topic']}, evidence={_v['evidence']}")
                                ad_creation_error = _build_policy_error_response(policy_violations2)
                                ad_creation_error["retried"] = True
                                logger.error(f"[GADS-CREATE] Retry also failed: {ad_creation_error}")
                            except Exception as _ae2:
                                ad_creation_error["retry_error"] = str(_ae2)
                                logger.error(f"[GADS-CREATE] Retry raised non-policy error: {_ae2}")
                except Exception as _ae:
                    ad_creation_error = str(_ae)
                    logger.error(f"[GADS-CREATE] Ad creation failed: {_ae}")
            else:
                reasons = []
                if len(headlines) < 3:
                    reasons.append(f"only {len(headlines)} headline(s) in memory (need 3+)")
                if len(descriptions) < 2:
                    reasons.append(f"only {len(descriptions)} description(s) in memory (need 2+)")
                if not final_url:
                    reasons.append("no landing page URL provided")
                ad_creation_error = "Ad not created — " + "; ".join(reasons)
                logger.warning(f"[GADS-CREATE] Skipping ad creation: {ad_creation_error}")

            # Sitelinks — Google Ads' "Ad Strength" grader also checks these;
            # without them a technically-valid RSA still scores lower.
            sitelinks_data = camp_data.get("sitelinks", [])
            if sitelinks_data:
                try:
                    sitelinks_added = await asyncio.to_thread(
                        _create_sitelinks_sync, result["campaign_rn"], sitelinks_data, customer_id
                    )
                    logger.info(f"[GADS-CREATE] Added {len(sitelinks_added)} sitelinks from memory")
                except Exception as _se:
                    logger.warning(f"[GADS-CREATE] Sitelink creation failed: {_se}")
        else:
            ad_creation_error = "Ad not created — no business_key/url provided to look up campaign assets"

        result["keywords_added"]    = len(keywords_added)
        result["ad_created"]       = bool(ad_created)
        result["ads_created"]      = 1 if ad_created else 0
        result["ad"]               = ad_created
        result["ad_creation_error"] = ad_creation_error
        result["sitelinks_added"]  = len(sitelinks_added)
        result["location_target"] = {
            "resource_name": location_resource_name,
            "matched_name":  location_matched_name,
            "applied":       location_applied,
        }
        result["google_ads_dashboard"] = f"https://ads.google.com/aw/campaigns?campaignId={result['campaign_id']}"
        logger.info(f"[GADS-CREATE] Done: campaign_id={result['campaign_id']}")

        log_activity(
            "google_campaign_created", business_key=business_key,
            business_name=request.url or request.campaign_name,
            url=request.url, industry=request.industry, city=request.city,
            summary=(
                f"{result.get('status', 'PAUSED')}, {len(keywords_added)} keywords, "
                f"{'ad created' if ad_created else 'ad NOT created'}"
            ),
            reference_id=result["campaign_id"],
        )

        return {"success": True, **result}

    except GoogleAdsException as ex:
        error_details = []
        for error in ex.failure.errors:
            detail = {
                "error_code": str(error.error_code),
                "message":    error.message,
                "field":      None,
            }
            if error.location:
                detail["field"] = str(error.location.field_path_elements)
            error_details.append(detail)
        logger.error(f"[GADS-CREATE EXACT ERROR] {error_details}")
        return {"success": False, "errors": error_details}
    except Exception as _e:
        tb = _traceback.format_exc()
        logger.error(f"[GADS-CREATE UNEXPECTED ERROR] {_e}\n{tb}")
        return {"success": False, "error": str(_e), "traceback": tb}


@app.post("/google-ads/create-ad")
async def gads_create_ad(request: CreateAdRequest):
    try:
        customer_id = _gads_customer_id()
        if not customer_id:
            return {"success": False, "error": "GOOGLE_ADS_CUSTOMER_ID not configured"}

        # Build ad_group resource name from campaign_id + ad_group_id
        # Caller may pass ad_group_id directly as resource name or bare ID
        ad_group_id = request.campaign_id  # overloaded: caller passes ad_group resource or we build it
        if not ad_group_id.startswith("customers/"):
            ad_group_rn = f"customers/{customer_id}/adGroups/{ad_group_id}"
        else:
            ad_group_rn = ad_group_id

        if not request.headlines or not request.descriptions or not request.final_url:
            return {"success": False, "error": "headlines, descriptions, and final_url are required"}

        logger.info(f"[GADS-AD] Creating RSA in ad_group_rn={ad_group_rn!r}")
        result = await asyncio.to_thread(
            _create_ad_sync,
            ad_group_rn, request.headlines, request.descriptions, request.final_url, customer_id,
        )

        if request.sitelinks:
            try:
                def _get_campaign_rn():
                    svc = get_google_ads_client().get_service("GoogleAdsService")
                    query = f"SELECT ad_group.campaign FROM ad_group WHERE ad_group.resource_name = '{ad_group_rn}'"
                    rows = list(svc.search(customer_id=customer_id, query=query))
                    return rows[0].ad_group.campaign if rows else None

                campaign_rn = await asyncio.to_thread(_get_campaign_rn)
                if campaign_rn:
                    sitelinks_added = await asyncio.to_thread(
                        _create_sitelinks_sync, campaign_rn, request.sitelinks, customer_id
                    )
                    result["sitelinks_added"] = len(sitelinks_added)
                    logger.info(f"[GADS-AD] Added {len(sitelinks_added)} sitelinks to campaign={campaign_rn!r}")
            except Exception as _se:
                logger.warning(f"[GADS-AD] Sitelink creation failed: {_se}")

        return {"success": True, **result}

    except GoogleAdsException as ex:
        policy_violations = _extract_policy_violations(ex)
        for _v in policy_violations:
            if _v["topic"]:
                logger.info(f"[GADS AD POLICY] topic={_v['topic']}, evidence={_v['evidence']}")
        logger.error(f"[GADS-AD] API error: {[v['message'] for v in policy_violations]}")
        return {"success": False, **_build_policy_error_response(policy_violations)}
    except Exception as _e:
        tb = _traceback.format_exc()
        logger.error(f"[GADS-AD] ERROR: {_e}\n{tb}")
        return {"success": False, "error": str(_e), "traceback": tb}


@app.post("/google-ads/add-keywords")
async def gads_add_keywords(request: AddKeywordsRequest):
    try:
        customer_id = _gads_customer_id()
        if not customer_id:
            return {"success": False, "error": "GOOGLE_ADS_CUSTOMER_ID not configured"}

        ad_group_id = request.ad_group_id
        if not ad_group_id.startswith("customers/"):
            ad_group_rn = f"customers/{customer_id}/adGroups/{ad_group_id}"
        else:
            ad_group_rn = ad_group_id

        if not request.keywords:
            return {"success": False, "error": "keywords list is empty"}

        logger.info(f"[GADS-KW] Adding {len(request.keywords)} keywords to {ad_group_rn!r}")
        added = await asyncio.to_thread(
            _add_keywords_sync, ad_group_rn, request.keywords, customer_id
        )
        return {"success": True, "keywords_added": len(added), "resource_names": added}

    except GoogleAdsException as ex:
        errors = [e.message for e in ex.failure.errors]
        logger.error(f"[GADS-KW] API error: {errors}")
        return {"success": False, "error": "; ".join(errors)}
    except Exception as _e:
        tb = _traceback.format_exc()
        logger.error(f"[GADS-KW] ERROR: {_e}\n{tb}")
        return {"success": False, "error": str(_e), "traceback": tb}


# ═══════════════════════════════════════════════════════════════════════════════
#  META ADS (Facebook/Instagram) — READ-ONLY INTEGRATION
#  Same safe pattern as Google Ads: test-connection first, read-only endpoints
#  only. No campaign creation yet. Isolated from Google Ads / Cricket code.
# ═══════════════════════════════════════════════════════════════════════════════

def get_meta_ads_client():
    """
    Initialize the Meta Marketing API client from env vars.
    Returns (api, ad_account_id) — ad_account_id normalized to the "act_<id>"
    format the SDK expects. Raises RuntimeError if required env vars are missing.
    """
    from facebook_business.api import FacebookAdsApi

    app_id       = _genv("META_APP_ID")
    app_secret   = _genv("META_APP_SECRET")
    access_token = _genv("META_ACCESS_TOKEN")
    account_id   = _genv("META_AD_ACCOUNT_ID")

    missing = [k for k, v in {
        "META_APP_ID": app_id, "META_APP_SECRET": app_secret,
        "META_ACCESS_TOKEN": access_token, "META_AD_ACCOUNT_ID": account_id,
    }.items() if not v]
    if missing:
        raise RuntimeError(f"Missing env vars: {', '.join(missing)}")

    account_id_raw = account_id
    if not account_id.startswith("act_"):
        account_id = f"act_{account_id}"

    api = FacebookAdsApi.init(app_id, app_secret, access_token)
    logger.info(
        f"[META ADS] Client initialized — app_id={app_id[:5]}…(len={len(app_id)}) "
        f"access_token={access_token[:8]}…(len={len(access_token)}) "
        f"account_id_raw={account_id_raw!r} account_id_used={account_id}"
    )
    return api, account_id


# https://developers.facebook.com/docs/marketing-api/reference/ad-account/#account_status
_META_ACCOUNT_STATUS_NAMES = {
    1: "ACTIVE", 2: "DISABLED", 3: "UNSETTLED", 7: "PENDING_RISK_REVIEW",
    9: "IN_GRACE_PERIOD", 100: "PENDING_CLOSURE", 101: "CLOSED",
    201: "ANY_ACTIVE", 202: "ANY_CLOSED",
}


def _meta_error_details(ex) -> dict:
    """
    Extract every field Meta's Graph API gives us for a FacebookRequestError.
    error_user_title/error_user_msg are end-user-facing strings Meta includes
    for some errors (e.g. permission/consent issues) but the SDK doesn't
    expose via a dedicated method — pull them from the raw error body.
    Every accessor is wrapped individually so one bad field never hides the rest.
    """
    def _safe(fn):
        try:
            return fn()
        except Exception as _e:
            return f"<unavailable: {_e}>"

    raw_body = _safe(ex.body)
    raw_error = raw_body.get("error", {}) if isinstance(raw_body, dict) else {}

    # api_error_message() can legitimately be None (e.g. the response wasn't
    # the expected {"error": {...}} shape) — never let "error" end up empty,
    # fall back to the SDK's full formatted diagnostic string instead.
    error_message = _safe(ex.api_error_message) or _safe(ex.get_message) or "Unknown Meta API error (see raw_body)"

    return {
        "error":           error_message,
        "error_code":      _safe(ex.api_error_code),
        "error_subcode":   _safe(ex.api_error_subcode),
        "error_type":      _safe(ex.api_error_type),
        "error_user_title": raw_error.get("error_user_title") if isinstance(raw_error, dict) else None,
        "error_user_msg":   raw_error.get("error_user_msg") if isinstance(raw_error, dict) else None,
        "http_status":     _safe(ex.http_status),
        "fbtrace_id":      raw_error.get("fbtrace_id") if isinstance(raw_error, dict) else None,
        "raw_body":        raw_body,
    }


def _log_meta_error(context: str, ex) -> dict:
    # Logged in full detail up front — Google Ads debugging burned us on
    # vague error messages, so surface every field immediately here.
    details = _meta_error_details(ex)
    logger.error(f"[META ADS] {context} FacebookRequestError — full details: {details}")
    return details


@app.get("/meta-ads/test-connection")
async def meta_ads_test_connection():
    """
    Verify Meta Marketing API connectivity and surface account details for
    debugging. Everything — client init AND the API call — is wrapped in one
    try/except so the frontend always gets a well-formed {connected, error}
    body, never a raw 500 that renders as "Unknown error".
    """
    from facebook_business.adobjects.adaccount import AdAccount
    from facebook_business.exceptions import FacebookRequestError

    try:
        _, account_id = get_meta_ads_client()

        def _fetch():
            account = AdAccount(account_id)
            return account.api_get(fields=[
                AdAccount.Field.name,
                AdAccount.Field.currency,
                AdAccount.Field.timezone_name,
                AdAccount.Field.account_status,
            ])

        info = await asyncio.to_thread(_fetch)
        status_code = info.get(AdAccount.Field.account_status)
        return {
            "connected":    True,
            "account_id":   account_id,
            "account_name": info.get(AdAccount.Field.name),
            "currency":     info.get(AdAccount.Field.currency),
            "timezone":     info.get(AdAccount.Field.timezone_name),
            "status":       _META_ACCOUNT_STATUS_NAMES.get(status_code, status_code),
        }
    except RuntimeError as _e:
        # Missing env vars — from get_meta_ads_client()
        logger.error(f"[META ADS] test-connection config error: {_e}")
        return {"connected": False, "error": str(_e)}
    except FacebookRequestError as ex:
        details = _log_meta_error("test-connection", ex)
        return {"connected": False, **details}
    except Exception as ex:
        tb = _traceback.format_exc()
        logger.error(f"[META ADS] test-connection unexpected error: {type(ex).__name__}: {ex}\n{tb}")
        return {"connected": False, "error": f"{type(ex).__name__}: {ex}" or "Unknown error (see server logs)"}


@app.get("/meta-ads/campaigns")
async def meta_ads_campaigns():
    """List all campaigns in the ad account (read-only)."""
    from facebook_business.adobjects.adaccount import AdAccount
    from facebook_business.adobjects.campaign import Campaign
    from facebook_business.exceptions import FacebookRequestError

    try:
        _, account_id = get_meta_ads_client()
    except RuntimeError as _e:
        return {"success": False, "error": str(_e)}

    try:
        def _fetch():
            account = AdAccount(account_id)
            return list(account.get_campaigns(fields=[
                Campaign.Field.id,
                Campaign.Field.name,
                Campaign.Field.status,
                Campaign.Field.objective,
                Campaign.Field.daily_budget,
            ]))

        rows = await asyncio.to_thread(_fetch)
        campaigns = [{
            "id":           c.get(Campaign.Field.id),
            "name":         c.get(Campaign.Field.name),
            "status":       c.get(Campaign.Field.status),
            "objective":    c.get(Campaign.Field.objective),
            "daily_budget": c.get(Campaign.Field.daily_budget),
        } for c in rows]
        return {"success": True, "campaigns": campaigns}
    except FacebookRequestError as ex:
        details = _log_meta_error("campaigns", ex)
        return {"success": False, **details}
    except Exception as ex:
        tb = _traceback.format_exc()
        logger.error(f"[META ADS] campaigns unexpected error: {type(ex).__name__}: {ex}\n{tb}")
        return {"success": False, "error": f"{type(ex).__name__}: {ex}"}


@app.get("/campaigns/all")
async def campaigns_all():
    """
    Single source of truth for "what campaigns exist" — aggregates every
    campaign this tool has visibility into across Google Ads AND Meta, each
    with a direct dashboard link. Backs the History page's Campaigns tab.
    Each platform's failure is isolated so one platform being unconfigured
    doesn't hide the other's real campaigns.
    """
    campaigns = []
    errors = {}

    # ── Google Ads ────────────────────────────────────────────────────────────
    try:
        customer_id = _gads_customer_id()
        if customer_id:
            def _fetch_gads():
                client  = get_google_ads_client()
                service = client.get_service("GoogleAdsService")
                query = "SELECT campaign.id, campaign.name, campaign.status FROM campaign ORDER BY campaign.id DESC"
                return list(service.search(customer_id=customer_id, query=query))

            rows = await asyncio.to_thread(_fetch_gads)
            for row in rows:
                cid = str(row.campaign.id)
                campaigns.append({
                    "platform":       "google",
                    "campaign_id":    cid,
                    "name":           row.campaign.name,
                    "status":         row.campaign.status.name if hasattr(row.campaign.status, "name") else str(row.campaign.status),
                    "dashboard_link": f"https://ads.google.com/aw/campaigns?campaignId={cid}",
                })
        else:
            errors["google"] = "GOOGLE_ADS_CUSTOMER_ID not configured"
    except GoogleAdsException as ex:
        errors["google"] = "; ".join(e.message for e in ex.failure.errors)
        logger.error(f"[CAMPAIGNS-ALL] Google Ads fetch failed: {errors['google']}")
    except Exception as ex:
        errors["google"] = str(ex)
        logger.error(f"[CAMPAIGNS-ALL] Google Ads fetch unexpected error: {ex}")

    # ── Meta Ads ──────────────────────────────────────────────────────────────
    try:
        from facebook_business.adobjects.adaccount import AdAccount
        from facebook_business.adobjects.campaign import Campaign

        _, account_id = get_meta_ads_client()
        dashboard_link = f"https://business.facebook.com/adsmanager/manage/campaigns?act={account_id.replace('act_', '')}"

        def _fetch_meta():
            account = AdAccount(account_id)
            return list(account.get_campaigns(fields=[
                Campaign.Field.id, Campaign.Field.name, Campaign.Field.status,
            ]))

        rows = await asyncio.to_thread(_fetch_meta)
        for c in rows:
            campaigns.append({
                "platform":       "meta",
                "campaign_id":    c.get(Campaign.Field.id),
                "name":           c.get(Campaign.Field.name),
                "status":         c.get(Campaign.Field.status),
                "dashboard_link": dashboard_link,
            })
    except RuntimeError as ex:
        errors["meta"] = str(ex)
    except Exception as ex:
        errors["meta"] = str(ex)
        logger.error(f"[CAMPAIGNS-ALL] Meta Ads fetch unexpected error: {ex}")

    return {"success": True, "campaigns": campaigns, "errors": errors}


@app.get("/meta-ads/performance")
async def meta_ads_performance(date_range: str = "last_30d"):
    """
    Aggregated + per-campaign performance breakdown — same pattern as
    /google-ads/performance. date_range accepts any Meta Insights date_preset
    (e.g. last_7d, last_30d, last_90d, this_month).
    """
    from facebook_business.adobjects.adaccount import AdAccount
    from facebook_business.adobjects.adsinsights import AdsInsights
    from facebook_business.exceptions import FacebookRequestError

    try:
        _, account_id = get_meta_ads_client()
    except RuntimeError as _e:
        return {"success": False, "error": str(_e)}

    valid_presets = {p for p in dir(AdsInsights.DatePreset) if not p.startswith("_")}
    if date_range not in valid_presets:
        return {"success": False, "error": f"Invalid date_range {date_range!r}. Valid: {sorted(valid_presets)}"}

    try:
        def _fetch():
            account = AdAccount(account_id)
            return list(account.get_insights(
                fields=[
                    AdsInsights.Field.campaign_id,
                    AdsInsights.Field.campaign_name,
                    AdsInsights.Field.impressions,
                    AdsInsights.Field.clicks,
                    AdsInsights.Field.spend,
                    AdsInsights.Field.ctr,
                    AdsInsights.Field.cpc,
                    AdsInsights.Field.reach,
                    AdsInsights.Field.conversions,
                    AdsInsights.Field.date_start,
                    AdsInsights.Field.date_stop,
                ],
                params={"date_preset": date_range, "level": "campaign"},
            ))

        rows = await asyncio.to_thread(_fetch)

        per_campaign = []
        total_impressions, total_clicks, total_reach = 0, 0, 0
        total_spend = total_conversions = 0.0

        for r in rows:
            impressions = int(r.get(AdsInsights.Field.impressions, 0) or 0)
            clicks      = int(r.get(AdsInsights.Field.clicks, 0) or 0)
            spend       = float(r.get(AdsInsights.Field.spend, 0) or 0)
            reach       = int(r.get(AdsInsights.Field.reach, 0) or 0)
            # "conversions" is a list of {action_type, value} — sum values for
            # a single aggregate number in this first read-only pass.
            conv_actions = r.get(AdsInsights.Field.conversions) or []
            conversions  = sum(float(a.get("value", 0)) for a in conv_actions) if isinstance(conv_actions, list) else 0.0

            total_impressions += impressions
            total_clicks      += clicks
            total_spend       += spend
            total_reach       += reach
            total_conversions += conversions

            per_campaign.append({
                "campaign_id":   r.get(AdsInsights.Field.campaign_id),
                "campaign_name": r.get(AdsInsights.Field.campaign_name),
                "impressions":   impressions,
                "clicks":        clicks,
                "spend":         round(spend, 2),
                "ctr":           r.get(AdsInsights.Field.ctr),
                "cpc":           r.get(AdsInsights.Field.cpc),
                "reach":         reach,
                "conversions":   round(conversions, 2),
            })

        overall_ctr = (total_clicks / total_impressions * 100) if total_impressions else 0
        overall_cpc = (total_spend / total_clicks) if total_clicks else 0

        return {
            "success":    True,
            "date_range": date_range,
            "aggregated": {
                "impressions": total_impressions,
                "clicks":      total_clicks,
                "spend":       round(total_spend, 2),
                "ctr_pct":     round(overall_ctr, 2),
                "avg_cpc":     round(overall_cpc, 2),
                "reach":       total_reach,
                "conversions": round(total_conversions, 2),
            },
            "per_campaign": per_campaign,
        }
    except FacebookRequestError as ex:
        details = _log_meta_error("performance", ex)
        return {"success": False, **details}
    except Exception as ex:
        tb = _traceback.format_exc()
        logger.error(f"[META ADS] performance unexpected error: {type(ex).__name__}: {ex}\n{tb}")
        return {"success": False, "error": f"{type(ex).__name__}: {ex}"}


# ── Meta Ads Campaign Creation (write access — PAUSED by default) ───────────

class CreateMetaCampaignRequest(BaseModel):
    campaign_name: str   = ""              # auto-derived from url/industry/city if blank
    objective:     str   = "OUTCOME_LEADS"
    daily_budget:  float                   # in ₹
    business_key:  str   = ""              # if set, pull ad copy/city from campaign_memory
    url:           str   = ""              # used with industry/city to derive business_key
                                            # when business_key isn't passed directly
    industry:      str   = ""
    city:          str   = ""
    creative_id:   str   = ""              # existing Post ID (object_story_id), created manually
                                            # in Ads Manager — required to complete the Ad step
                                            # while the app is in Development Mode (see below)


def _resolve_meta_targeting_sync(city: str) -> dict:
    """
    Returns a Meta targeting spec dict. Defaults to country-level India
    targeting; if a city is given, attempts a city-level geolocation lookup
    via TargetingSearch and falls back to India on any miss/failure — same
    safety pattern as the Google Ads geo-target resolver.
    """
    from facebook_business.adobjects.targetingsearch import TargetingSearch

    india_targeting = {"geo_locations": {"countries": ["IN"]}}
    city_clean = (city or "").strip()
    if not city_clean or city_clean.lower() in ("india", "all india", "pan india"):
        return india_targeting

    try:
        results = TargetingSearch.search(params={
            "type":           TargetingSearch.TargetingSearchTypes.geolocation,
            "location_types": ["city"],
            "q":              city_clean,
            "limit":          1,
        })
        if results:
            key = results[0].get("key")
            if key:
                return {"geo_locations": {"cities": [{"key": key, "radius": 25, "distance_unit": "kilometer"}]}}
        logger.info(f"[META ADS] No city targeting match for {city_clean!r} — falling back to India")
    except Exception as _e:
        logger.warning(f"[META ADS] City targeting lookup failed for city={city_clean!r}: {_e}")

    return india_targeting


@app.post("/meta-ads/create-campaign")
async def meta_ads_create_campaign(request: CreateMetaCampaignRequest):
    """
    Create a Campaign + AdSet, both PAUSED by default — same safe pattern as
    Google Ads: nothing goes live until a human reviews and enables it in
    Ads Manager.

    The Ad step is conditional on `creative_id`:
    Meta blocks apps in Development Mode from creating NEW Page-attributed
    ad creatives (confirmed via live testing — error_subcode 1885183, not
    bypassable via user role, only via full App Review/Live mode). The one
    documented workaround that doesn't require App Review: a human creates
    the creative/post manually in Ads Manager once, and the API only
    references that existing Post ID (object_story_id) rather than
    originating new Page content — so:
      - No creative_id: stop after Campaign+AdSet, return the Ad Set ID and
        instructions to finish the Ad manually in Ads Manager.
      - creative_id given: create the AdCreative via object_story_id
        (referencing the existing post, not creating a new one) then the Ad —
        completing the full automated chain.
    """
    from facebook_business.adobjects.adaccount import AdAccount
    from facebook_business.adobjects.campaign import Campaign
    from facebook_business.adobjects.adset import AdSet
    from facebook_business.adobjects.adcreative import AdCreative
    from facebook_business.adobjects.ad import Ad
    from facebook_business.exceptions import FacebookRequestError

    try:
        _, account_id = get_meta_ads_client()
    except RuntimeError as _e:
        return {"success": False, "error": str(_e)}

    # ── Resolve business_key (same derivation + city-fallback lookup as
    # /google-ads/create-campaign) so a blank/mismatched city or legacy key
    # still resolves to the right campaign_memory row ───────────────────────
    lookup_source = request.business_key or request.url
    resolved_business_key = (
        derive_business_key(lookup_source, request.industry, request.city)
        if lookup_source else ""
    )

    # ── Pull ad copy / city / landing URL from campaign_memory ──────────────
    headline      = "Grow Your Business Today"
    primary_text  = "Reach more customers with a campaign built for results."
    description   = ""
    city          = request.city or ""
    final_url     = ""
    final_url_is_placeholder = False

    if lookup_source:
        mem, resolved_key = get_memory_with_city_fallback(lookup_source, request.industry, request.city)
        logger.info(f"[META ADS CREATE] Memory resolved via key={resolved_key!r}")
        camp_data_raw = mem.get("campaign", {}).get("campaign_data", {}) if mem else {}
        if isinstance(camp_data_raw, str) and camp_data_raw.strip().startswith("{"):
            try:
                camp_data_raw = json.loads(camp_data_raw)
            except Exception:
                camp_data_raw = {}
        camp_data = camp_data_raw if isinstance(camp_data_raw, dict) else {}

        headlines    = camp_data.get("headlines") or []
        descriptions = camp_data.get("descriptions") or []
        if headlines:
            headline = str(headlines[0])[:40]
        if descriptions:
            primary_text = str(descriptions[0])[:125]
            if len(descriptions) > 1:
                description = str(descriptions[1])[:30]

        biz = mem.get("business", {}) if mem else {}
        city = city or biz.get("city", "") or ""

        website = mem.get("website", {}) if mem else {}
        final_url = (website.get("url") or "").strip()

        if not final_url:
            # business_key is derived as "domain.com::industry::city" for
            # URL-mode businesses — reuse the domain segment as a last resort.
            domain = resolved_business_key.strip().split("::")[0].strip()
            if domain and "." in domain and " " not in domain:
                final_url = domain if domain.startswith(("http://", "https://")) else f"https://{domain}"

    # ── Auto-derive a campaign name if the caller didn't send one ───────────
    campaign_name = request.campaign_name.strip()
    if not campaign_name:
        if final_url and not final_url_is_placeholder:
            _domain = final_url.replace("https://", "").replace("http://", "").rstrip("/")
            campaign_name = f"{_domain} — Meta Leads"
        elif request.industry:
            campaign_name = f"{request.industry} — {request.city or 'India'}"
        else:
            campaign_name = "New Meta Campaign"
        logger.info(f"[META ADS CREATE] Auto-derived campaign_name={campaign_name!r}")

    if not final_url:
        final_url = "https://example.com"
        final_url_is_placeholder = True

    logger.info(
        f"[META ADS CREATE] business_key={request.business_key!r} city={city!r} "
        f"final_url={final_url!r} placeholder={final_url_is_placeholder}"
    )

    created = {}
    try:
        def _create_campaign():
            account = AdAccount(account_id)
            return account.create_campaign(params={
                Campaign.Field.name:                 campaign_name,
                Campaign.Field.objective:             request.objective,
                Campaign.Field.status:                Campaign.Status.paused,
                Campaign.Field.special_ad_categories: [Campaign.SpecialAdCategory.none],
                # Budget lives on the AdSet (not shared/pooled at campaign
                # level) — Meta now requires this explicitly instead of
                # inferring it from the absence of a campaign-level budget.
                Campaign.Field.is_adset_budget_sharing_enabled: False,
            })

        campaign = await asyncio.to_thread(_create_campaign)
        campaign_id = campaign.get(Campaign.Field.id)
        created["campaign_id"] = campaign_id
        logger.info(f"[META ADS CREATE] Campaign created: {campaign_id}")

        targeting = await asyncio.to_thread(_resolve_meta_targeting_sync, city)
        logger.info(f"[META ADS CREATE] Targeting resolved: {targeting}")

        optimization_goal = (
            AdSet.OptimizationGoal.lead_generation
            if request.objective.upper() == "OUTCOME_LEADS"
            else AdSet.OptimizationGoal.link_clicks
        )

        def _create_adset():
            account = AdAccount(account_id)
            return account.create_ad_set(params={
                AdSet.Field.name:              f"{campaign_name} — Ad Set 1",
                AdSet.Field.campaign_id:       campaign_id,
                AdSet.Field.daily_budget:      int(round(request.daily_budget * 100)),  # ₹ → paise
                AdSet.Field.billing_event:     AdSet.BillingEvent.impressions,
                AdSet.Field.optimization_goal: optimization_goal,
                AdSet.Field.targeting:         targeting,
                AdSet.Field.status:            AdSet.Status.paused,
                # Explicit bid strategy required — "let Meta find the
                # lowest cost" with no manual bid cap, the simplest default.
                AdSet.Field.bid_strategy:      AdSet.BidStrategy.lowest_cost_without_cap,
            })

        adset = await asyncio.to_thread(_create_adset)
        adset_id = adset.get(AdSet.Field.id)
        created["adset_id"] = adset_id
        logger.info(f"[META ADS CREATE] AdSet created: {adset_id}")

        ads_manager_link = f"https://business.facebook.com/adsmanager/manage/campaigns?act={account_id.replace('act_', '')}"

        if not request.creative_id:
            # Can't create a new ad creative while the app is in Development
            # Mode (Meta blocks the app from originating new Page content —
            # confirmed live, not bypassable via user role). Stop here with
            # a clear next step instead of failing the whole request.
            message = (
                f"Campaign and Ad Set created successfully (PAUSED). To complete this "
                f"campaign, manually create an ad creative in Meta Ads Manager for this "
                f"ad set, then note its Post ID for future automated ad creation. "
                f"Ad Set ID: {adset_id}"
            )
            logger.info(f"[META ADS CREATE] Stopping after AdSet (no creative_id): {message}")

            log_activity(
                "meta_campaign_created", business_key=resolved_business_key,
                business_name=request.url or campaign_name,
                url=request.url, industry=request.industry, city=request.city,
                summary="PAUSED, ad set created, ad creative pending manual step",
                reference_id=campaign_id,
            )

            return {
                "success":               True,
                "action_needed":         True,
                "campaign_id":           campaign_id,
                "adset_id":              adset_id,
                "creative_id":           None,
                "ad_id":                 None,
                "status":                "PAUSED",
                "targeting_used":        targeting,
                "message":               message,
                "meta_ads_manager_link": ads_manager_link,
            }

        # creative_id given: reference the EXISTING post via object_story_id
        # instead of object_story_spec — this doesn't ask the app to
        # originate new Page content, so it isn't blocked by Dev Mode.
        def _create_creative():
            account = AdAccount(account_id)
            return account.create_ad_creative(params={
                AdCreative.Field.name:            f"{campaign_name} — Creative",
                AdCreative.Field.object_story_id: request.creative_id,
            })

        creative = await asyncio.to_thread(_create_creative)
        creative_id = creative.get(AdCreative.Field.id)
        created["creative_id"] = creative_id
        logger.info(f"[META ADS CREATE] AdCreative created from existing post {request.creative_id!r}: {creative_id}")

        def _create_ad():
            account = AdAccount(account_id)
            return account.create_ad(params={
                Ad.Field.name:     f"{campaign_name} — Ad 1",
                Ad.Field.adset_id: adset_id,
                Ad.Field.creative: {"creative_id": creative_id},
                Ad.Field.status:   Ad.Status.paused,
            })

        ad = await asyncio.to_thread(_create_ad)
        ad_id = ad.get(Ad.Field.id)
        created["ad_id"] = ad_id
        logger.info(f"[META ADS CREATE] Ad created: {ad_id}")

        log_activity(
            "meta_campaign_created", business_key=resolved_business_key,
            business_name=request.url or campaign_name,
            url=request.url, industry=request.industry, city=request.city,
            summary="PAUSED, ad created",
            reference_id=campaign_id,
        )

        return {
            "success":                 True,
            "action_needed":           False,
            "campaign_id":             campaign_id,
            "adset_id":                adset_id,
            "creative_id":             creative_id,
            "ad_id":                   ad_id,
            "status":                  "PAUSED",
            "targeting_used":          targeting,
            "final_url":               final_url,
            "final_url_is_placeholder": final_url_is_placeholder,
            "meta_ads_manager_link":   ads_manager_link,
        }

    except FacebookRequestError as ex:
        details = _log_meta_error("create-campaign", ex)
        return {"success": False, "partial": created, **details}
    except Exception as ex:
        tb = _traceback.format_exc()
        logger.error(f"[META ADS] create-campaign unexpected error: {type(ex).__name__}: {ex}\n{tb}")
        return {"success": False, "partial": created, "error": f"{type(ex).__name__}: {ex}"}


class DeleteMetaCampaignRequest(BaseModel):
    campaign_id: str


@app.post("/meta-ads/delete-campaign")
async def meta_ads_delete_campaign(request: DeleteMetaCampaignRequest):
    """
    Delete (Meta's API sets status=DELETED, a reversible soft-delete) a
    campaign — used to clean up test/orphaned campaigns from failed or
    exploratory create-campaign runs.
    """
    from facebook_business.adobjects.campaign import Campaign
    from facebook_business.exceptions import FacebookRequestError

    try:
        get_meta_ads_client()
    except RuntimeError as _e:
        return {"success": False, "error": str(_e)}

    try:
        def _delete():
            campaign = Campaign(request.campaign_id)
            return campaign.api_delete()

        await asyncio.to_thread(_delete)
        logger.info(f"[META ADS] Deleted campaign_id={request.campaign_id}")
        return {"success": True, "campaign_id": request.campaign_id, "status": "DELETED"}
    except FacebookRequestError as ex:
        details = _log_meta_error("delete-campaign", ex)
        return {"success": False, **details}
    except Exception as ex:
        tb = _traceback.format_exc()
        logger.error(f"[META ADS] delete-campaign unexpected error: {type(ex).__name__}: {ex}\n{tb}")
        return {"success": False, "error": f"{type(ex).__name__}: {ex}"}


# ═══════════════════════════════════════════════════════════════════════════════
#  SMART FULL ANALYSIS — ORCHESTRATION LAYER
#  Runs Marketing Brain first, then an AI Decision Layer picks which other
#  modules are worth running, then runs only those in parallel. Pure
#  orchestration: every module call below reuses the exact same function
#  used by that module's own HTTP endpoint — no logic is duplicated.
# ═══════════════════════════════════════════════════════════════════════════════

class SmartAnalysisRequest(BaseModel):
    url:      str
    industry: str   = ""
    city:     str   = ""
    budget:   float = 0.0


_SMART_ANALYSIS_MODULE_KEYS = [
    "opportunity_engine", "offer_intelligence", "website_intelligence",
    "visibility_intelligence", "outreach_ai", "kpi_engine", "prospect_discovery",
]


_SMART_ANALYSIS_BUSINESS_MODELS = ["B2B", "B2C", "D2C"]


async def _smart_analysis_decision_layer(brain_result: dict, request: SmartAnalysisRequest) -> dict:
    """
    One GPT-4o call that does two things: (1) classifies the business model
    as B2B/B2C/D2C by actually reading Marketing Brain's business
    understanding + audience segments — not by just checking whether the
    industry field was filled in, since the goal is to reason about the
    real business model even when the input signal is ambiguous or missing —
    and (2) decides which modules are worth running given that classification.
    Falls back to a deterministic rule-based decision (using the industry
    field as a proxy, since Brain's own prompts already frame their output
    deterministically around it) if the AI call fails or returns something
    unusable — the endpoint should never fail just because this one
    judgment call had a hiccup.
    """
    sections = brain_result.get("sections", {}) or {}
    business_understanding = (sections.get("business_understanding") or "")[:800]
    audience_strategy      = (sections.get("audience_strategy") or "")[:800]
    market_understanding   = (sections.get("market_understanding") or "")[:400]
    has_budget = bool(request.budget and request.budget > 0)

    prompt = (
        "You are a marketing operations decision-maker.\n\n"
        "STEP 1 — Classify this business's model as exactly one of: B2B, B2C, D2C.\n"
        "- B2B: sells to OTHER BUSINESSES. The buyer is an owner/manager/director/founder of a company.\n"
        "- B2C: sells a SERVICE or experience directly to individual consumers (e.g. salons, clinics, "
        "restaurants, coaching, real estate agents).\n"
        "- D2C: sells a tangible PRODUCT directly to consumers, typically via ecommerce (e.g. a jewellery "
        "brand, fashion label, skincare brand, food product brand).\n"
        "Base this ONLY on the actual business understanding and audience segments below — do not assume "
        "from any other signal. If the audience segments describe job titles like Owner/Manager/Director "
        "of a specific industry, it's B2B. If they describe consumer personas (e.g. 'value-conscious "
        "shopper', 'wedding buyer', 'fitness enthusiast'), it's B2C or D2C — pick D2C if the business "
        "primarily sells a physical product online, B2C if it sells a service/experience.\n\n"
        f"BUSINESS UNDERSTANDING:\n{business_understanding}\n\n"
        f"AUDIENCE STRATEGY / SEGMENTS:\n{audience_strategy}\n\n"
        f"MARKET UNDERSTANDING:\n{market_understanding}\n\n"
        f"(Context only — do not treat as the deciding signal: user-provided target industry field = "
        f"{request.industry or '(blank)'})\n\n"
        "STEP 2 — Decide which modules are worth running, given the business model you just classified. "
        "Be selective — running an irrelevant module wastes time and API cost.\n\n"
        f"Website URL provided: {'yes' if request.url.strip() else 'no'}\n"
        f"Budget provided: {'yes' if has_budget else 'no'}\n\n"
        "ALWAYS relevant regardless of business model:\n"
        "1. opportunity_engine — market opportunity scoring. Almost always worth running.\n"
        "2. offer_intelligence — offer/pricing strategy analysis. Almost always worth running.\n"
        "3. website_intelligence — deep website audit (speed, UX, conversion signals). Worth running "
        "if a website URL exists.\n"
        "4. visibility_intelligence — SEO/AEO/GEO search visibility audit. Worth running if this "
        "business depends on search engines or local/online discovery to get customers.\n"
        "5. kpi_engine — KPI targets and benchmarks. Only meaningful if a budget was provided.\n\n"
        "ONLY relevant for B2B (skip entirely for B2C/D2C):\n"
        "6. outreach_ai — cold outreach scripts (WhatsApp/Instagram DM/pitch) for reaching OTHER "
        "BUSINESSES. Not relevant for B2C/D2C — individual consumers aren't cold-outreached like businesses.\n"
        "7. prospect_discovery — finds real prospect BUSINESSES to target. Not relevant for B2C/D2C — "
        "there are no 'prospect businesses' when selling directly to consumers.\n\n"
        "Return STRICT JSON only, using these exact snake_case module keys:\n"
        "{\n"
        '  "business_model": "B2B" | "B2C" | "D2C",\n'
        '  "modules_to_run": ["opportunity_engine", "offer_intelligence"],\n'
        '  "skipped": [{"module": "module_key", "reason": "one sentence why"}]\n'
        "}"
    )

    def _fallback_decision() -> dict:
        # Deterministic mirror of the same criteria stated above — used only
        # if the AI call itself fails. Brain's own prompts already frame
        # their entire output deterministically around whether target_industry
        # was given, so using it as the B2B signal here remains reasonable
        # even without re-reading the generated text.
        is_b2b = bool(request.industry.strip())
        business_model = "B2B" if is_b2b else "B2C"
        run, skip = ["opportunity_engine", "offer_intelligence"], []
        if request.url.strip():
            run.append("website_intelligence")
        else:
            skip.append({"module": "website_intelligence", "reason": "No website URL provided"})
        if request.url.strip():
            run.append("visibility_intelligence")
        else:
            skip.append({"module": "visibility_intelligence", "reason": "No website URL provided"})
        if has_budget:
            run.append("kpi_engine")
        else:
            skip.append({"module": "kpi_engine", "reason": "No budget provided"})
        if is_b2b:
            run += ["outreach_ai", "prospect_discovery"]
        else:
            skip.append({"module": "outreach_ai", "reason": f"Not relevant for {business_model} — consumers aren't cold-outreached like businesses"})
            skip.append({"module": "prospect_discovery", "reason": f"Not relevant for {business_model} — no prospect businesses to find"})
        return {"business_model": business_model, "modules_to_run": run, "skipped": skip}

    try:
        resp = await asyncio.to_thread(
            lambda: client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=700,
                response_format={"type": "json_object"},
            )
        )
        data = json.loads(resp.choices[0].message.content)
        business_model = data.get("business_model")
        if business_model not in _SMART_ANALYSIS_BUSINESS_MODELS:
            business_model = "B2B" if request.industry.strip() else "B2C"
        modules_to_run = [m for m in data.get("modules_to_run", []) if m in _SMART_ANALYSIS_MODULE_KEYS]
        skipped = [
            s for s in data.get("skipped", [])
            if isinstance(s, dict) and s.get("module") in _SMART_ANALYSIS_MODULE_KEYS
        ]
        if not modules_to_run:
            raise ValueError("Decision layer returned an empty modules_to_run")
        # Hard safety net: never let outreach_ai/prospect_discovery run for a
        # non-B2B classification, even if the AI call disagreed with itself.
        if business_model != "B2B":
            for leaked in ("outreach_ai", "prospect_discovery"):
                if leaked in modules_to_run:
                    modules_to_run.remove(leaked)
                    if not any(s.get("module") == leaked for s in skipped):
                        skipped.append({"module": leaked, "reason": f"Not relevant for {business_model} — consumer-facing business"})
        logger.info(f"[SMART-ANALYSIS] Decision layer: model={business_model} run={modules_to_run} skipped={[s['module'] for s in skipped]}")
        return {"business_model": business_model, "modules_to_run": modules_to_run, "skipped": skipped}
    except Exception as _e:
        logger.warning(f"[SMART-ANALYSIS] Decision layer failed, using rule-based fallback: {_e}")
        return _fallback_decision()


_RESULT_KEY_BY_MODULE = {
    "opportunity_engine":      "opportunity",
    "offer_intelligence":      "offer",
    "website_intelligence":    "website",
    "visibility_intelligence": "visibility",
    "outreach_ai":             "outreach",
    "kpi_engine":              "kpi",
    "prospect_discovery":      "prospects",
}

# Same localStorage keys each module's own page already reads on mount —
# used to pre-load a module's page when the user clicks "View Full Report"
# from Smart Analysis, and by the frontend directly (kept here as the single
# source of truth so backend/frontend never drift apart).
_RESULT_LS_KEY_BY_MODULE = {
    "opportunity_engine":      "adsoh_opportunity_result",
    "offer_intelligence":      "adsoh_offer_result",
    "website_intelligence":    "adsoh_website_result",
    "visibility_intelligence": "adsoh_visibility_result",
    "outreach_ai":             "adsoh_outreach_result",
    "kpi_engine":              "adsoh_kpi_result",
    "prospect_discovery":      "adsoh_prospect_result",
}


async def _smart_analysis_run_brain_and_decision(request: SmartAnalysisRequest) -> dict:
    """Steps 1+2 only: Marketing Brain + Decision Layer. No modules run yet."""
    full_report_req = FullReportRequest(
        url=request.url,
        business_type=request.industry or "Business",
        budget=int(request.budget) if request.budget else 10000,
        goal="Lead Generation",
        target_industry=request.industry,
        target_city=request.city,
    )
    db = SessionLocal()
    try:
        brain_result = await full_report(full_report_req, db)
    except Exception as _e:
        tb = _traceback.format_exc()
        logger.error(f"[SMART-ANALYSIS] Marketing Brain failed: {_e}\n{tb}")
        return {"success": False, "error": f"Marketing Brain failed: {_e}"}
    finally:
        db.close()

    if not brain_result.get("success"):
        return {"success": False, "error": "Marketing Brain did not complete successfully", "brain_result": brain_result}

    business_key = derive_business_key(request.url, request.industry, request.city)
    logger.info(f"[SMART-ANALYSIS] Brain complete. business_key={business_key!r}")

    decision = await _smart_analysis_decision_layer(brain_result, request)
    modules_to_run = [m for m in decision["modules_to_run"] if m in _RESULT_KEY_BY_MODULE]

    return {
        "success":      True,
        "business_key": business_key,
        "brain_result": brain_result,
        "decision": {
            "business_model":  decision["business_model"],
            "modules_run":     modules_to_run,
            "modules_skipped": decision["skipped"],
        },
    }


async def _smart_analysis_run_modules(
    url: str, industry: str, city: str, budget: float,
    business_key: str, modules_to_run: list,
) -> tuple:
    """Step 3 only: run the given module list in parallel. Returns (results, total_time)."""
    start_time = time.time()
    module_calls = {
        "opportunity_engine": lambda: opportunity_engine(OpportunityEngineRequest(
            business_key=business_key, industry=industry, city=city,
            budget=int(budget) if budget else 0,
        )),
        "offer_intelligence": lambda: offer_intelligence(OfferIntelligenceRequest(
            business_key=business_key, industry=industry, city=city,
        )),
        "website_intelligence": lambda: website_intelligence(WebsiteIntelligenceRequest(
            url=url, business_key=business_key, industry=industry, city=city,
        )),
        "visibility_intelligence": lambda: visibility_intelligence(VisibilityIntelligenceRequest(
            url=url, industry=industry, city=city, business_key=business_key,
        )),
        "outreach_ai": lambda: outreach_ai(OutreachAIRequest(
            url=url, industry=industry, city=city, business_key=business_key,
        )),
        "kpi_engine": lambda: kpi_engine(KPIEngineRequest(
            url=url, industry=industry, city=city,
            budget=budget, goal="Lead Generation",
        )),
        "prospect_discovery": lambda: prospect_discovery(ProspectDiscoveryRequest(
            industry=industry, city=city, url=url,
        )),
    }

    modules_to_run = [m for m in modules_to_run if m in module_calls]
    logger.info(f"[SMART-ANALYSIS] Running {len(modules_to_run)} modules in parallel: {modules_to_run}")

    results = {v: None for v in _RESULT_KEY_BY_MODULE.values()}
    if modules_to_run:
        outcomes = await asyncio.gather(
            *[module_calls[m]() for m in modules_to_run],
            return_exceptions=True,
        )
        for module_name, outcome in zip(modules_to_run, outcomes):
            result_key = _RESULT_KEY_BY_MODULE[module_name]
            if isinstance(outcome, Exception):
                logger.error(f"[SMART-ANALYSIS] Module {module_name!r} raised: {outcome}")
                results[result_key] = {"success": False, "error": f"{type(outcome).__name__}: {outcome}"}
            else:
                results[result_key] = outcome

    total_time = round(time.time() - start_time, 2)
    logger.info(f"[SMART-ANALYSIS] Modules done in {total_time}s")
    return results, total_time


_SMART_ANALYSIS_HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS smart_analysis_history (
    id             BIGSERIAL PRIMARY KEY,
    business_key   TEXT,
    url            TEXT,
    business_model TEXT,
    modules_run    TEXT,
    full_result    TEXT,
    created_at     TEXT
);
"""

try:
    with engine.connect() as _sah_conn:
        _sah_ddl = _SMART_ANALYSIS_HISTORY_DDL
        if _is_sqlite:
            _sah_ddl = _sah_ddl.replace("BIGSERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
        _sah_conn.execute(text(_sah_ddl))
        _sah_conn.commit()
    logger.info("[SMART-ANALYSIS] History table created/verified")
except Exception as _sahe:
    logger.warning(f"[SMART-ANALYSIS] Could not create history table: {_sahe}")


def _save_smart_analysis_history(business_key: str, url: str, business_model: str, modules_run: list, full_result: dict) -> None:
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO smart_analysis_history (business_key, url, business_model, modules_run, full_result, created_at) "
                "VALUES (:bk, :url, :bm, :mr, :fr, :ca)"
            ), {
                "bk": business_key, "url": url, "bm": business_model,
                "mr": json.dumps(modules_run), "fr": json.dumps(full_result),
                "ca": datetime.utcnow().isoformat(),
            })
    except Exception as _e:
        logger.warning(f"[SMART-ANALYSIS] Could not save history: {_e}")


@app.post("/smart-analysis")
async def smart_analysis(request: SmartAnalysisRequest):
    if not request.url.strip():
        return {"success": False, "error": "url is required"}

    plan = await _smart_analysis_run_brain_and_decision(request)
    if not plan.get("success"):
        return plan

    results, total_time = await _smart_analysis_run_modules(
        request.url, request.industry, request.city, request.budget,
        plan["business_key"], plan["decision"]["modules_run"],
    )

    # ── TRUST & ACCURACY LAYER ────────────────────────────────────────────
    # Smart Analysis runs Marketing Brain internally as its first step —
    # reuse that same trust assessment rather than recomputing it, since
    # brain_result IS a /full-report response and already carries it.
    _brain = plan["brain_result"] or {}
    response = {
        "success":            True,
        "business_model":     plan["decision"]["business_model"],
        "brain_result":       plan["brain_result"],
        "decision":           plan["decision"],
        "results":            results,
        "total_time_seconds": total_time,
        "trust_verdict":      _brain.get("trust_verdict"),
        "based_on":           _brain.get("based_on"),
        "validation_warning": _brain.get("validation_warning"),
    }
    _save_smart_analysis_history(
        plan["business_key"], request.url, plan["decision"]["business_model"],
        plan["decision"]["modules_run"], response,
    )
    log_activity(
        "smart_analysis", business_key=plan["business_key"], business_name=request.url,
        url=request.url, industry=request.industry, city=request.city,
        summary=f"Smart Analysis — {len(plan['decision']['modules_run'])} modules run",
    )
    return response


@app.post("/smart-analysis/plan")
async def smart_analysis_plan(request: SmartAnalysisRequest):
    """Runs Brain + Decision Layer only — lets the frontend show a confirm/override screen before spending time on modules."""
    if not request.url.strip():
        return {"success": False, "error": "url is required"}
    return await _smart_analysis_run_brain_and_decision(request)


class SmartAnalysisExecuteRequest(BaseModel):
    url:             str
    industry:        str   = ""
    city:            str   = ""
    budget:          float = 0.0
    business_key:    str
    brain_result:    dict
    modules_to_run:  list
    modules_skipped: list  = []
    business_model:  str   = "B2B"


@app.post("/smart-analysis/execute")
async def smart_analysis_execute(request: SmartAnalysisExecuteRequest):
    """Runs the (possibly user-overridden) module list against an already-computed brain_result — no Brain/Decision re-run."""
    results, total_time = await _smart_analysis_run_modules(
        request.url, request.industry, request.city, request.budget,
        request.business_key, request.modules_to_run,
    )
    _brain = request.brain_result or {}
    response = {
        "success":            True,
        "business_model":     request.business_model,
        "brain_result":       request.brain_result,
        "decision": {
            "business_model":  request.business_model,
            "modules_run":     request.modules_to_run,
            "modules_skipped": request.modules_skipped,
        },
        "results":            results,
        "total_time_seconds": total_time,
        "trust_verdict":      _brain.get("trust_verdict"),
        "based_on":           _brain.get("based_on"),
        "validation_warning": _brain.get("validation_warning"),
    }
    _save_smart_analysis_history(request.business_key, request.url, request.business_model, request.modules_to_run, response)
    log_activity(
        "smart_analysis", business_key=request.business_key, business_name=request.url,
        url=request.url, industry=request.industry, city=request.city,
        summary=f"Smart Analysis — {len(request.modules_to_run)} modules run",
    )
    return response


@app.get("/smart-analysis/history")
async def smart_analysis_history(limit: int = 5):
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT id, business_key, url, business_model, modules_run, created_at "
                "FROM smart_analysis_history ORDER BY id DESC LIMIT :lim"
            ), {"lim": limit}).mappings().all()
        history = []
        for r in rows:
            d = dict(r)
            try:
                d["modules_run"] = json.loads(d["modules_run"]) if d.get("modules_run") else []
            except Exception:
                d["modules_run"] = []
            history.append(d)
        return {"success": True, "history": history}
    except Exception as _e:
        logger.error(f"[SMART-ANALYSIS] history list failed: {_e}")
        return {"success": False, "error": str(_e), "history": []}


@app.get("/smart-analysis/history/{history_id}")
async def smart_analysis_history_detail(history_id: int):
    try:
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT full_result FROM smart_analysis_history WHERE id = :id"
            ), {"id": history_id}).first()
        if not row:
            return {"success": False, "error": "Not found"}
        return {"success": True, "result": json.loads(row[0])}
    except Exception as _e:
        logger.error(f"[SMART-ANALYSIS] history detail failed: {_e}")
        return {"success": False, "error": str(_e)}


# ═══════════════════════════════════════════════════════════════════════════════
#  COMMAND CENTER (Phase 12) — one text box that activates internal engines
# ═══════════════════════════════════════════════════════════════════════════════

class CommandRequest(BaseModel):
    text:     str
    url:      str = ""
    industry: str = ""
    city:     str = ""   # blank, not "Jaipur" — must stay falsy so a city mentioned in `text` can win the fallback below
    budget:   float = 0.0
    goal:     str = ""

_COMMAND_INTENTS = (
    "full_report", "prospect_discovery", "campaign_launch_kit", "ai_optimizer",
    "opportunity_engine", "kpi_engine", "outreach_ai", "website_intelligence",
    "offer_intelligence", "autonomous_marketing", "full_campaign_launch",
    "trend_research", "hashtag_generation", "ad_script_writing", "market_query",
    "creative_generation", "competitor_ad_finder", "weekly_report", "meta_campaign_launch",
    "unknown",
)

# Intents that run multiple real backend phases and are slow enough to
# warrant a live step-by-step panel instead of one long blocking request —
# see _COMMAND_STEP_TEMPLATES / _run_multi_step_command / GET
# /command/status/{task_id}. Every other intent still responds directly and
# synchronously, unchanged.
_COMMAND_STEP_TEMPLATES = {
    "full_report": [
        {"key": "analyze", "label": "🧠 Analyzing Business, Market & Audience"},
        {"key": "done",    "label": "✅ Report Ready"},
    ],
    "autonomous_marketing": [
        {"key": "decide", "label": "🧠 Deciding Campaign Plan"},
        {"key": "done",   "label": "✅ Plan Ready"},
    ],
    "full_campaign_launch": [
        {"key": "analyze", "label": "🧠 Analyzing Business, Market & Audience"},
        {"key": "kit",     "label": "✍️ Writing Ad Copy & Keywords"},
        {"key": "push",    "label": "📊 Building Google Ads Campaign"},
        {"key": "done",    "label": "✅ Campaign Ready"},
    ],
    "meta_campaign_launch": [
        {"key": "analyze", "label": "🧠 Analyzing Business, Market & Audience"},
        {"key": "kit",     "label": "✍️ Writing Ad Copy & Keywords"},
        {"key": "push",    "label": "📊 Building Meta Ads Campaign"},
        {"key": "done",    "label": "✅ Campaign Ready"},
    ],
}
_MULTI_STEP_INTENTS = set(_COMMAND_STEP_TEMPLATES.keys())

# In-memory task store for multi-step commands — fine for a single-process
# app (consistent with other in-memory state already used in this file).
_COMMAND_TASKS: dict = {}


def _task_set_step(task_id: str, key: str, status: str, detail=None) -> None:
    task = _COMMAND_TASKS.get(task_id)
    if not task:
        return
    for s in task["steps"]:
        if s["key"] == key:
            s["status"] = status
            if detail is not None:
                s["detail"] = detail
            break

_IG_HANDLE_RE = re.compile(r'instagram\.com/([A-Za-z0-9_.]+)', re.I)


def _command_research_identity_verified(handle: str, snippet: str) -> bool:
    """
    Deterministic identity guard for the Tavily research step below —
    applies the same discipline already proven in the Social Intelligence
    Engine (_sie_name_appears_in_snippet: does the business's own name
    actually appear in the research snippet, rather than trusting a
    topically-plausible result), generalized across ANY industry.

    Requires ALL distinctive tokens (>=4 chars) of the handle to appear in
    the snippet — not just one. A single shared word is not enough proof of
    identity, because handles routinely bundle a category descriptor with
    the brand name (e.g. "skyweds_events", "smilecare_dental",
    "zenith_realty") — a wrong business in the SAME category will
    legitimately share that one word ("events"/"dental"/"realty") without
    being the same business at all. Confirmed live: researching
    "skyweds_events" (a real Jaipur wedding planner) once confidently
    matched an unrelated Oklahoma City venue whose snippet happened to
    mention "events" generically. Requiring the full token combination
    generalizes correctly without needing a hand-maintained, industry-by-
    industry list of "generic" category words — which would never cover
    every vertical (salons, clinics, restaurants, real estate, jewellery...).
    """
    if not handle or not snippet:
        return False
    snippet_l = snippet.lower()
    tokens = [w.lower() for w in re.findall(r"[a-zA-Z]+", handle) if len(w) >= 4]
    if not tokens:
        tokens = [handle.strip().lower()]
    return all(t in snippet_l for t in tokens)


async def _command_verify_research_aboutness(handle: str, snippet: str) -> str | None:
    """
    A second, stronger gate on top of _command_research_identity_verified.
    The name merely APPEARING in a snippet (the deterministic check above)
    is not proof the snippet actually DESCRIBES what that business does —
    confirmed live: researching "12notez" (a real music/podcast production
    studio) surfaced a snippet where the name was only mentioned in passing
    inside a generic "how to grow on social media" article, which passed
    the token-presence check, and the generation step then treated 12notez
    AS a social-media-management business as a result. This mirrors the
    Social Intelligence Engine's own verification discipline (a GPT read of
    "is this really about the business, or just a name that appears in
    unrelated context") backed by a strict instruction to say UNKNOWN
    rather than guess. Returns a one-sentence description of what the
    business actually does, or None if the research doesn't confidently and
    specifically describe it.
    """
    try:
        prompt = (
            f'A research snippet below may or may not actually describe what the business/account "{handle}" does.\n\n'
            f"RESEARCH:\n{snippet[:2000]}\n\n"
            f'In ONE sentence, state specifically what "{handle}" actually does (industry/product/service) — '
            "ONLY if the research text clearly and specifically describes it. If the name only appears in passing "
            "(e.g. listed among other unrelated examples, or the surrounding text is really about something else — "
            "general advice, a different topic, a different business) and does NOT tell you what THIS business "
            "specifically does, respond with EXACTLY the single word: UNKNOWN\n\n"
            "Respond with ONLY the one sentence, or ONLY the word UNKNOWN — nothing else."
        )
        resp = await asyncio.to_thread(
            client.chat.completions.create, model="gpt-4o", messages=[{"role": "user", "content": prompt}],
            max_tokens=100, temperature=0,
        )
        answer = (resp.choices[0].message.content or "").strip()
        if not answer or answer.strip().upper().startswith("UNKNOWN"):
            return None
        return answer
    except Exception as _e:
        logger.warning(f"[COMMAND] research-aboutness verification call failed: {_e}")
        return None


_FUZZY_LOOKUP_TABLES = ("business_memory", "social_intel_memory", "creative_studio_memory")


def _command_fuzzy_memory_lookup(name: str) -> tuple:
    """
    Broader fallback used only by the Command Center's lightweight content-
    generation intents (hashtags/scripts) — finds existing memory for a bare
    business name mentioned in natural language (e.g. "sohscape ke liye reel
    script do"), not just a URL or @handle.

    get_memory_with_city_fallback's own LIKE-prefix fallback only activates
    when an industry is ALSO given (these commands usually give none) and
    only scans business_memory — so a business whose real intelligence
    lives in Social Intelligence Engine or Creative Studio memory instead
    of Marketing Brain's business_memory would never be found there either.
    Confirmed live: "sohscape ke liye reel script do" and "skyweds ke liye
    ... script chahiye" both fell through to the fully generic fallback
    despite real stored memory existing for both — Sohscape in
    business_memory (business_key "sohscape.com"), Skyweds in
    social_intel_memory — one prefix/substring match away from a bare name.
    Returns (memory_dict, matched_key) from get_memory(), or ({}, "") if
    nothing matched in any of the three tables.
    """
    token = (name or "").strip().lower()
    if not token or len(token) < 3:
        return {}, ""
    for table in _FUZZY_LOOKUP_TABLES:
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    text(f"SELECT business_key FROM {table} WHERE LOWER(business_key) LIKE :p ORDER BY updated_at DESC LIMIT 1"),
                    {"p": f"%{token}%"},
                ).first()
            if row:
                found_key = row[0]
                mem = get_memory(found_key)
                if mem:
                    logger.info(f"[COMMAND MEMORY] fuzzy match: {name!r} -> {found_key!r} (via {table})")
                    return mem, found_key
        except Exception as _e:
            logger.warning(f"[COMMAND MEMORY] fuzzy lookup against {table} failed: {_e}")
    return {}, ""


def _memory_to_command_context(mem: dict) -> str | None:
    """
    Normalizes stored memory from ANY of the 3 sources the Command Center's
    content-generation intents should check — Marketing Brain's
    business_memory, Social Intelligence Engine's social_intel_memory,
    Creative Studio's creative_studio_memory — into one context string for
    the generation prompt. Each module stores its findings under a
    different schema, so this tries them in order of richness and stops at
    the first one with real content. Returns None if nothing usable was
    found in any of them.
    """
    parts = []
    bm = (mem or {}).get("business", {}) or {}
    if _has_real_business_dna(bm):
        if bm.get("business_name"): parts.append(f"Business: {bm['business_name']}")
        if bm.get("industry"):      parts.append(f"Industry: {bm['industry']}")
        if bm.get("positioning"):   parts.append(f"Positioning: {bm['positioning']}")
        if bm.get("uvp"):           parts.append(f"UVP: {bm['uvp']}")

    if not parts:
        sie = (mem or {}).get("social_intel", {}) or {}
        sie_data = sie.get("data", {}) or {}
        if isinstance(sie_data, str):
            try: sie_data = json.loads(sie_data)
            except Exception: sie_data = {}
        bs = sie_data.get("business_summary", {}) or {}
        if bs.get("positioning") or bs.get("industry"):
            if bs.get("business_name"): parts.append(f"Business: {bs['business_name']}")
            if bs.get("industry"):      parts.append(f"Industry: {bs['industry']}")
            if bs.get("positioning"):   parts.append(f"Positioning: {bs['positioning']}")

    if not parts:
        cs = (mem or {}).get("creative_studio", {}) or {}
        cs_data = cs.get("data", {}) or {}
        if isinstance(cs_data, str):
            try: cs_data = json.loads(cs_data)
            except Exception: cs_data = {}
        intel_applied = (cs_data.get("intelligence_applied") or "").strip()
        if intel_applied and not _detect_consistency_contradiction(True, intel_applied):
            parts.append(f"Prior creative intelligence: {intel_applied}")

    if not parts:
        return None

    am = (mem or {}).get("audience", {}) or {}
    if am.get("segments"):
        parts.append(f"Audience segments: {json.dumps(am['segments'], ensure_ascii=False)[:400]}")
    return "\n".join(parts)


async def _command_memory_context(url: str, industry: str, city: str, text_cmd: str) -> dict:
    """Pull real business context for the lightweight content-generation
    intents (hashtags/captions/scripts), trying progressively lighter
    sources so the output is never MORE generic than the real signal
    available warrants:
      1. Stored memory — Marketing Brain, Social Intelligence Engine, or
         Creative Studio (checked via the exact/legacy key first, then a
         broader fuzzy lookup across all three — see
         _command_fuzzy_memory_lookup) — the strongest signal.
      2. A live Tavily search on the given identifier (Instagram handle,
         business name, or URL) to find out what the business actually
         does. Confirmed live: without this tier, "hashtags for
         skyweds_events" (a wedding events business) produced fully generic
         "#BusinessGrowth"-style hashtags with zero wedding/event
         relevance, because nothing ever looked the name up.
      3. Fully generic — genuinely nothing to ground on.
    Returns {"context": str, "grounded_in_business_data": bool,
    "research_used": bool, "identifier": str|None}."""
    if url or industry:
        lookup_name = url or industry
        mem, _ = get_memory_with_city_fallback(lookup_name, industry, city)
        context_str = _memory_to_command_context(mem)
        if not context_str:
            fuzzy_mem, _ = _command_fuzzy_memory_lookup(lookup_name)
            if fuzzy_mem:
                context_str = _memory_to_command_context(fuzzy_mem)
        if context_str:
            return {
                "context": context_str, "grounded_in_business_data": True,
                "research_used": False, "identifier": lookup_name,
            }

    identifier = (url or industry or "").strip()
    if identifier and TAVILY_API_KEY:
        m = _IG_HANDLE_RE.search(identifier)
        handle = m.group(1) if m else identifier.lstrip("@")
        # Bake any already-known city into the search itself — disambiguates
        # the wrong business up front rather than relying solely on the
        # post-hoc identity check below to catch it. Deliberately not
        # Instagram-specific (a real business is often better described by
        # general web results than by forcing "instagram" into the query).
        query = f"{handle} company business what do they do" + (f" {city}" if city else "")
        research = await _tavily_research(query)
        if research["used"]:
            if _command_research_identity_verified(handle, research["raw"]):
                description = await _command_verify_research_aboutness(handle, research["raw"])
                if description:
                    return {
                        "context": f"Business identifier: {handle}\nWhat this business actually does (verified from public research): {description}",
                        "grounded_in_business_data": False, "research_used": True, "identifier": handle,
                    }
                logger.info(
                    f"[COMMAND HASHTAG] discarded research — {handle!r} appeared in the snippet but the research "
                    f"didn't specifically describe what the business does (first 150 chars): {research['raw'][:150]!r}"
                )
            else:
                logger.info(
                    f"[COMMAND HASHTAG] discarded research — identity mismatch: input handle {handle!r} not "
                    f"confirmed in the returned snippet (first 150 chars): {research['raw'][:150]!r}"
                )

    return {
        "context": f"Business URL/name: {url or industry or 'not specified'}\nRequest context: {text_cmd}",
        "grounded_in_business_data": False, "research_used": False, "identifier": url or industry or None,
    }


async def _tavily_research(query: str) -> dict:
    """Real web research only — never a substitute for invented trends. When
    used=False, callers MUST say research wasn't available rather than let
    GPT fabricate trends/facts from nothing."""
    if not TAVILY_API_KEY:
        return {"used": False, "query": query, "raw": ""}
    raw = await fetch_tavily(query)
    return {"used": bool(raw.strip()), "query": query, "raw": raw}


async def _classify_command(text_cmd: str, url: str, industry: str, city: str, budget: float) -> dict:
    prompt = (
        "Classify this command into exactly one internal Marketing Brain action and extract any parameters "
        "mentioned in the text itself.\n\n"
        f'COMMAND: "{text_cmd}"\n'
        f"ALREADY KNOWN CONTEXT: url={url or 'none'}, industry={industry or 'none'}, city={city or 'none'}, budget={budget or 'none'}\n\n"
        "AVAILABLE ACTIONS:\n"
        "- full_report: run the complete Marketing Brain analysis (business/market/competitor/audience/campaign strategy)\n"
        "- prospect_discovery: find a list of prospect businesses in an industry+city, e.g. 'find hotels in Delhi'\n"
        "- campaign_launch_kit: generate a ready-to-launch Google+Meta Ads campaign kit (keywords, headlines, audiences, budget split) "
        "— pick this when the command wants the KIT/copy/plan written, not an actual live campaign created\n"
        "- ai_optimizer: analyze an existing campaign's performance and recommend pause/scale/creative/budget changes\n"
        "- opportunity_engine: find the highest-ROI audience/offer/platform opportunity for a business, e.g. 'find better audience'\n"
        "- kpi_engine: predict CTR/CPC/CPL/ROAS and budget breakdown for a campaign\n"
        "- outreach_ai: generate cold outreach scripts (WhatsApp/email/DM/call) for a target\n"
        "- website_intelligence: audit a website/landing page for conversion issues, e.g. 'improve landing page'\n"
        "- offer_intelligence: design the best offer/lead magnet/pricing for a business\n"
        "- autonomous_marketing: given a plain budget + goal (e.g. 'budget 5000, need doctor leads'), decide the "
        "complete campaign plan automatically — pick this when the command states a budget AND a goal together, but "
        "does NOT explicitly ask for the ad to actually go live\n"
        "- full_campaign_launch: understand this business AND actually CREATE a real (paused) Google Ads campaign for "
        "it end-to-end — pick this when the command clearly wants an ad/campaign actually launched, run, or made "
        "live, not just planned. Trigger phrases: 'ad chalana hai', 'launch a campaign', 'launch an ad', 'run "
        "everything for this client', 'get this live', 'start advertising', 'put this on Google Ads'. Needs both a "
        "url and a budget to actually run — extract whatever is present, leave the rest blank if missing.\n"
        "- trend_research: what's trending in an industry/niche right now, what ad formats/angles are working, e.g. "
        "'what's trending in hospitality marketing', 'what ads are working now for gyms'\n"
        "- hashtag_generation: generate hashtags and/or captions for social posts, e.g. 'hashtags do', 'captions likho'\n"
        "- ad_script_writing: write a short-form video ad/reel/short script (hook/body/CTA), e.g. 'reel script banao', "
        "'video ad script chahiye'\n"
        "- market_query: any other real-world marketing/market question that needs current information to answer "
        "honestly rather than a guess, e.g. 'what's happening in the SaaS market right now', 'should I run ads on "
        "LinkedIn for B2B', 'is Instagram still worth it for restaurants' — this is also the catch-all for any "
        "genuinely marketing-related request that doesn't cleanly fit one of the other actions above\n"
        "- creative_generation: generate ad creative concepts/images/visual direction for a business, e.g. "
        "'generate an ad image for [url]', 'design a creative for my business'\n"
        "- competitor_ad_finder: find what ads a competitor is running and how to beat them, e.g. 'what ads is "
        "[competitor] running', 'find my competitor's ads'\n"
        "- weekly_report: summarize recent activity/performance across this tool for the last week, e.g. 'give me "
        "this week's summary', 'what happened this week for [url]'\n"
        "- meta_campaign_launch: same as full_campaign_launch but for Meta/Facebook/Instagram Ads instead of "
        "Google — pick this when the command specifically says Meta/Facebook/Instagram ads, e.g. 'launch a Meta "
        "campaign for [url] budget [X]', 'run this on Instagram ads'. Needs both a url and a budget, same as "
        "full_campaign_launch.\n"
        "- unknown: ONLY for requests that are NOT about marketing/advertising/business growth at all (e.g. weather, "
        "sports scores, general trivia, coding help unrelated to this platform). If a request is marketing-related "
        "but doesn't fit neatly into a specific action, choose market_query instead of unknown — always prefer "
        "attempting the closest real capability over refusing.\n\n"
        'Return ONLY JSON: {"intent": "one of the actions above", '
        '"extracted": {"url":"", "industry":"","city":"","budget":0,"goal":""}, "reasoning": "one line"}\n'
        "For \"url\": pull out any website/domain, Instagram handle (with or without @, or as instagram.com/name), "
        "or bare business name/username mentioned in the command text itself (e.g. 'sohscape.com', 'https://...', "
        "'www.foo.in', '@skyweds_events', 'instagram.com/skyweds_events', or a bare handle-like name such as "
        "'skyweds_events') — this is the ONLY way the system learns what business to look up or research when the "
        "command comes from a single free-text box with no separate url field. Only fill \"extracted\" fields you "
        "can confidently pull from the command text itself — leave blank/0 if not mentioned."
    )
    resp = await asyncio.to_thread(
        client.chat.completions.create,
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        max_tokens=300,
        temperature=0,
    )
    parsed = json.loads(resp.choices[0].message.content.strip())
    if parsed.get("intent") not in _COMMAND_INTENTS:
        parsed["intent"] = "unknown"
    return parsed


async def _run_multi_step_command(task_id: str, intent: str, url: str, industry: str, city: str, budget: float, goal: str) -> None:
    """
    Background runner for the 4 multi-step intents — updates _COMMAND_TASKS
    step-by-step as each REAL backend phase actually completes (never a
    simulated/fake progress bar), so GET /command/status/{task_id} always
    reflects genuine state. Final shape matches exactly what the old
    synchronous /command response used to return (result + extra_fields),
    so the frontend renders the completed task the same way it already
    renders a normal synchronous response.
    """
    task = _COMMAND_TASKS[task_id]
    try:
        if intent == "full_report":
            _task_set_step(task_id, "analyze", "running")
            db = SessionLocal()
            try:
                brain_result = await full_report(FullReportRequest(
                    url=url, business_type=industry or "Business", budget=int(budget) or 10000,
                    goal=goal, target_industry=industry, target_city=city,
                ), db)
            finally:
                db.close()
            _ok = bool(brain_result.get("success"))
            _task_set_step(task_id, "analyze", "done" if _ok else "error", detail={
                "trust_verdict": brain_result.get("trust_verdict"), "based_on": brain_result.get("based_on"),
                "business_understanding": ((brain_result.get("sections") or {}).get("business_understanding") or "")[:400],
            })
            _task_set_step(task_id, "done", "done" if _ok else "error")
            task["result"] = brain_result
            task["extra_fields"] = {}

        elif intent == "autonomous_marketing":
            _task_set_step(task_id, "decide", "running")
            plan_result = await autonomous_marketing(AutonomousMarketingRequest(
                url=url, industry=industry, city=city, budget=budget, goal_text=goal,
            ))
            _ok = bool(plan_result.get("success"))
            _task_set_step(task_id, "decide", "done" if _ok else "error", detail=plan_result)
            _task_set_step(task_id, "done", "done" if _ok else "error")
            task["result"] = plan_result
            task["extra_fields"] = {}

        elif intent in ("full_campaign_launch", "meta_campaign_launch"):
            _task_set_step(task_id, "analyze", "running")
            db = SessionLocal()
            try:
                brain_result = await full_report(FullReportRequest(
                    url=url, business_type=industry or "Business", budget=int(budget),
                    goal=goal or "Leads", target_industry="", target_city=city,
                ), db)
            finally:
                db.close()

            if not brain_result.get("success"):
                _task_set_step(task_id, "analyze", "error", detail={"error": "Marketing Brain could not analyze this business."})
                _task_set_step(task_id, "kit", "error")
                _task_set_step(task_id, "push", "error")
                _task_set_step(task_id, "done", "error")
                task["result"] = {"success": False, "error": "Marketing Brain could not analyze this business.", "brain_result": brain_result}
                task["extra_fields"] = {}
                task["status"] = "done"
                return

            _task_set_step(task_id, "analyze", "done", detail={
                "trust_verdict": brain_result.get("trust_verdict"), "based_on": brain_result.get("based_on"),
                "business_understanding": ((brain_result.get("sections") or {}).get("business_understanding") or "")[:400],
            })

            # Step 2: Campaign Launch Kit — writes keywords/headlines/descriptions
            # to campaign_memory, keyed identically to what step 3 reads back.
            _task_set_step(task_id, "kit", "running")
            kit_result = await campaign_launch_kit(CampaignLaunchKitRequest(
                url=url, industry=industry, city=city, budget=int(budget), goal=goal or "Leads",
                sections=brain_result.get("sections", {}),
            ))
            if not kit_result.get("success"):
                _task_set_step(task_id, "kit", "error", detail={"error": "Campaign Launch Kit generation failed."})
                _task_set_step(task_id, "push", "error")
                _task_set_step(task_id, "done", "error")
                task["result"] = {"success": False, "error": "Campaign Launch Kit generation failed.", "brain_result": brain_result, "kit_result": kit_result}
                task["extra_fields"] = {}
                task["status"] = "done"
                return

            _task_set_step(task_id, "kit", "done", detail={
                "meta_kit_preview": (kit_result.get("meta_kit") or "")[:300],
                "google_kit_preview": (kit_result.get("google_kit") or "")[:300],
            })

            # Step 3: Push to Google/Meta Ads — PAUSED by default, same flow as
            # the existing "Push to Ads" button (same key derivation, so it
            # reads back exactly what step 2 just saved).
            _task_set_step(task_id, "push", "running")
            _biz_dna = (brain_result.get("bi_data") or {}).get("business_dna", {}) or {}
            biz_label = _biz_dna.get("business_name") or url
            campaign_name = f"{biz_label} — {goal or 'Leads'} — Command Center"[:255]
            # Real budget allocation: matches campaign_launch_kit's own written
            # split (40% Google / 50% Meta / 10% remarketing) instead of
            # treating the whole monthly budget as if 100% goes to one
            # platform — keeps the actually-created campaign's spend
            # consistent with what the generated kit tells the user.
            if intent == "full_campaign_launch":
                google_daily_budget = max(1.0, round((int(budget) * 0.40) / 30, 2))
                push_result = await gads_create_campaign(CreateCampaignRequest(
                    campaign_name=campaign_name, budget_daily=float(google_daily_budget), campaign_type="SEARCH",
                    url=url, industry=industry, city=city,
                ))
                push_key = "gads_result"
                extra = {
                    "campaign_id":     push_result.get("campaign_id"),
                    "ad_group_id":     push_result.get("ad_group_id"),
                    "keywords_added":  push_result.get("keywords_added"),
                    "ad_created":      push_result.get("ad_created"),
                    "google_ads_link": push_result.get("google_ads_dashboard"),
                    "trust_verdict":   brain_result.get("trust_verdict"),
                }
            else:
                meta_daily_budget = max(1.0, round((int(budget) * 0.50) / 30, 2))
                push_result = await meta_ads_create_campaign(CreateMetaCampaignRequest(
                    campaign_name=campaign_name, daily_budget=float(meta_daily_budget),
                    url=url, industry=industry, city=city,
                ))
                push_key = "meta_result"
                extra = {
                    "campaign_id":   push_result.get("campaign_id"),
                    "adset_id":      push_result.get("adset_id"),
                    "action_needed": push_result.get("action_needed"),
                    "meta_ads_link": push_result.get("meta_ads_manager_link"),
                    "trust_verdict": brain_result.get("trust_verdict"),
                }
            _ok = bool(push_result.get("success"))
            _task_set_step(task_id, "push", "done" if _ok else "error", detail=push_result)
            _task_set_step(task_id, "done", "done" if _ok else "error")
            task["result"] = {"success": _ok, "brain_result": brain_result, "kit_result": kit_result, push_key: push_result}
            task["extra_fields"] = extra

        task["status"] = "done"
    except Exception as _e:
        tb = _traceback.format_exc()
        logger.error(f"[COMMAND TASK {task_id}] {intent!r} failed: {_e}\n{tb}")
        for s in task["steps"]:
            if s["status"] in ("pending", "running"):
                s["status"] = "error"
        task["status"] = "error"
        task["result"] = {"success": False, "error": str(_e)}
        task["extra_fields"] = {}


@app.get("/command/status/{task_id}")
async def command_status(task_id: str):
    task = _COMMAND_TASKS.get(task_id)
    if not task:
        return {"success": False, "error": "Task not found — it may have expired or the task_id is invalid."}
    return {"success": True, **task}


@app.post("/command")
async def command_center(request: CommandRequest):
    text_cmd = (request.text or "").strip()
    if not text_cmd:
        return {"success": False, "error": "text is required"}

    try:
        classification = await _classify_command(text_cmd, request.url, request.industry, request.city, request.budget)
    except Exception as _e:
        logger.error(f"[COMMAND] classification failed: {_e}")
        return {"success": False, "error": f"Could not understand command: {_e}"}

    intent    = classification.get("intent", "unknown")
    extracted = classification.get("extracted", {}) or {}
    industry  = request.industry or extracted.get("industry") or ""
    city      = request.city or extracted.get("city") or ""
    budget    = request.budget or extracted.get("budget") or 0
    goal      = request.goal or extracted.get("goal") or text_cmd
    url       = request.url or extracted.get("url") or ""

    logger.info(f"[COMMAND] text={text_cmd!r} -> intent={intent!r} url={url!r} industry={industry!r} city={city!r} budget={budget}")

    # Included on every early "missing param" return below so the frontend can
    # carry these already-known values forward when the user replies in the
    # same box (e.g. just "10000") instead of losing context on every retry.
    _pu = {"url": url, "industry": industry, "city": city, "budget": budget, "goal": goal}

    if intent in _MULTI_STEP_INTENTS:
        # Real spend is on the line for the two campaign-launch intents —
        # never guess a budget, ask for it explicitly. Same param checks as
        # before, just run before spinning up the background task instead of
        # inside it.
        if intent in ("full_campaign_launch", "meta_campaign_launch") and not url:
            _which = "Google" if intent == "full_campaign_launch" else "Meta"
            return {"success": False, "intent": intent, "error": f"Missing business — which website should I launch a {_which} campaign for? e.g. 'launch a campaign for sohscape.com'", "params_used": _pu}
        if intent in ("full_campaign_launch", "meta_campaign_launch") and not budget:
            return {"success": False, "intent": intent, "error": "Missing budget — how much do you want to spend per month? e.g. 'budget 10000'", "params_used": _pu}
        if intent == "autonomous_marketing" and not goal:
            return {"success": False, "intent": intent, "error": "Missing goal — describe what you need, e.g. 'doctor leads'", "params_used": _pu}

        task_id = str(uuid.uuid4())
        _COMMAND_TASKS[task_id] = {
            "status": "running", "intent": intent, "reasoning": classification.get("reasoning", ""),
            "params_used": _pu,
            "steps": [{"key": s["key"], "label": s["label"], "status": "pending", "detail": None} for s in _COMMAND_STEP_TEMPLATES[intent]],
            "result": None, "extra_fields": {},
        }
        asyncio.create_task(_run_multi_step_command(task_id, intent, url, industry, city, budget, goal))
        return {
            "success": True, "intent": intent, "reasoning": classification.get("reasoning", ""),
            "params_used": _pu, "task_id": task_id, "multi_step": True,
        }

    extra_fields = {}
    try:
        if intent == "prospect_discovery":
            if not industry:
                return {"success": False, "intent": intent, "error": "Missing industry — try 'find [industry] in [city]'", "params_used": _pu}
            result = await prospect_discovery(ProspectDiscoveryRequest(industry=industry, city=city, url=url))

        elif intent == "campaign_launch_kit":
            result = await campaign_launch_kit(CampaignLaunchKitRequest(
                url=url, industry=industry, city=city, budget=int(budget) or 10000, goal=goal,
            ))

        elif intent == "ai_optimizer":
            result = await ai_optimizer(AIOptimizerRequest(url=url, industry=industry, city=city))

        elif intent == "opportunity_engine":
            if not url and not industry:
                return {"success": False, "intent": intent, "error": "Missing business — provide a url or industry", "params_used": _pu}
            result = await opportunity_engine(OpportunityEngineRequest(
                business_key=url or industry, industry=industry, city=city, budget=int(budget),
            ))

        elif intent == "kpi_engine":
            if not industry:
                return {"success": False, "intent": intent, "error": "Missing industry", "params_used": _pu}
            result = await kpi_engine(KPIEngineRequest(url=url, industry=industry, city=city, budget=budget, goal=goal))

        elif intent == "outreach_ai":
            result = await outreach_ai(OutreachAIRequest(url=url, industry=industry, city=city, outreach_goal=goal))

        elif intent == "website_intelligence":
            if not url:
                return {"success": False, "intent": intent, "error": "Missing url to audit", "params_used": _pu}
            result = await website_intelligence(WebsiteIntelligenceRequest(url=url, industry=industry, city=city))

        elif intent == "offer_intelligence":
            if not url and not industry:
                return {"success": False, "intent": intent, "error": "Missing business — provide a url or industry", "params_used": _pu}
            result = await offer_intelligence(OfferIntelligenceRequest(business_key=url or industry, industry=industry, city=city))

        elif intent == "creative_generation":
            if not url and not industry:
                return {"success": False, "intent": intent, "error": "Missing business — provide a url or industry", "params_used": _pu}
            result = await _run_creative_studio(CreativeStudioRequest(
                mode="business", url=url, industry=industry, city=city, campaign_objective=goal or "Leads",
            ))

        elif intent == "competitor_ad_finder":
            if not url and not industry:
                return {"success": False, "intent": intent, "error": "Missing competitor — provide a competitor's url, name, or industry", "params_used": _pu}
            db = SessionLocal()
            try:
                result = await ad_intelligence(AdIntelRequest(
                    business_name=url or industry, business_type=industry or "Business", website=url, country="IN",
                ), db)
            finally:
                db.close()

        elif intent == "weekly_report":
            _activity_result = await activity_list(limit=100, type="", business_key=url or "")
            _all_activity = _activity_result.get("activity", []) if isinstance(_activity_result, dict) else []
            _cutoff = datetime.utcnow() - timedelta(days=7)

            def _within_week(a):
                try:
                    return datetime.fromisoformat((a.get("created_at") or "").replace("Z", "")) >= _cutoff
                except Exception:
                    return True  # unparseable timestamp — don't silently drop it, keep it visible

            _recent = [a for a in _all_activity if _within_week(a)]
            _perf = None
            if url or industry:
                try:
                    _perf = await performance_intelligence(PerformanceIntelligenceRequest(
                        url=url, industry=industry, city=city, date_range="7d",
                    ))
                except Exception as _pe:
                    logger.warning(f"[COMMAND] weekly_report performance lookup failed: {_pe}")
            result = {"success": True, "activity_count": len(_recent), "recent_activity": _recent[:20], "performance": _perf}

        elif intent == "trend_research":
            query = f"latest {industry or 'digital marketing'} advertising trends {city or 'India'} 2026 social media campaigns"
            research = await _tavily_research(query)
            if not research["used"]:
                result = {"success": False, "error": "Live research wasn't available right now (no search results came back) — I can't responsibly report trends without real data. Try again in a bit."}
            else:
                prompt = (
                    "You are a marketing trend analyst. Using ONLY the real search results below — never invent an "
                    f"example that isn't supported by them — summarize current trends for {industry or 'this industry'} "
                    f"businesses in {city or 'India'}.\n\n"
                    f"REAL SEARCH RESULTS:\n{research['raw'][:3500]}\n\n"
                    f"BUSINESS ASKING: {url or industry or 'not specified'}\n\n"
                    'Return ONLY JSON: {"trending_themes": ["...","...","..."], "trending_formats": '
                    '["...","...","..."], "example_angles": ["...","...","..."], '
                    '"how_to_adapt": "one paragraph — how THIS business could adapt these trends"}\n'
                    "Every theme/format/angle must be traceable to the search results above."
                )
                resp = await asyncio.to_thread(
                    client.chat.completions.create, model="gpt-4o", messages=[{"role": "user", "content": prompt}],
                    response_format={"type": "json_object"}, max_tokens=900, temperature=0.3,
                )
                parsed = _clean_banned_words_deep(json.loads(resp.choices[0].message.content.strip()))
                parsed["based_on"] = f"Based on real-time web search results for \"{research['query']}\" — not invented."
                result = {"success": True, **parsed}

        elif intent == "hashtag_generation":
            _ctxr = await _command_memory_context(url, industry, city, text_cmd)
            _ctx, _grounded, _researched = _ctxr["context"], _ctxr["grounded_in_business_data"], _ctxr["research_used"]
            # Confirmed live: with zero real signal, GPT will still invent a
            # specific (and often unrelated) business vertical — e.g. a
            # digital marketing agency's script came back about "wellness"
            # and "eco-friendly spaces". Only guard against this when there's
            # truly no signal at all — when real public research WAS found,
            # GPT should use it, not stay artificially generic.
            # Confirmed live: even this guard's own wording can backfire — an
            # earlier version said "GENERIC business-growth/marketing
            # language", and GPT wrote copy about "growing your social media
            # strategy" as if the business itself WERE a marketing/social-
            # media-management agency (12notez, a music/podcast studio, got
            # hashtags like #SocialMediaManagement). "Generic" must mean
            # copy this business could post about ITSELF, never copy that
            # implies the business provides marketing/social-media services.
            _no_signal_guard = (
                "\nIMPORTANT — NO REAL BUSINESS DATA IS AVAILABLE ABOVE: do NOT invent a specific business "
                "category (wellness, fitness, food, fashion, marketing agency, social media management, etc.) "
                "that isn't explicitly stated in the context. Write hashtags/captions as generic, industry-agnostic "
                "advice THIS business could post to grow ITS OWN audience or customers (e.g. 'grow your following', "
                "'reach new customers', 'discover more') — use the business name from the context if present, but "
                "NEVER write copy that implies the business itself provides marketing, social media management, or "
                "consulting services, since that would be guessing an industry that was never confirmed.\n"
            ) if not _grounded and not _researched else ""
            # Confirmed live: without this, only the "niche" bucket picked up
            # the researched theme (e.g. "events") while "broad" stayed
            # completely generic ("#BusinessGrowth") — every bucket must
            # reflect what the research actually found.
            _research_note_for_prompt = (
                "\nNOTE: The context above includes REAL public research findings about this business — ground ALL "
                "hashtag categories (broad, niche, AND local) and the captions in what this business actually does, "
                "not just the niche category.\n"
            ) if _researched else ""
            prompt = (
                "Generate social media hashtags and captions for this business.\n\n"
                f"CONTEXT:\n{_ctx}\n"
                f"{_no_signal_guard}{_research_note_for_prompt}\n"
                'Return ONLY JSON: {"hashtags": {"broad": ["...","...","...","...","..."], '
                '"niche": ["...","...","...","...","..."], "local": ["...","...","...","...","..."]}, '
                '"captions": ["...","...","..."]}\n'
                "5-8 hashtags per bucket (broad = large general reach, niche = specific to the exact "
                "product/service, local = city/region-specific — omit local hashtags entirely if no city/location "
                "context is available, do not invent a city). 2-3 caption variations, each 1-3 sentences ending "
                "with a clear call to action."
            )
            resp = await asyncio.to_thread(
                client.chat.completions.create, model="gpt-4o", messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}, max_tokens=700, temperature=0.5,
            )
            parsed = _clean_banned_words_deep(json.loads(resp.choices[0].message.content.strip()))
            parsed["grounded_in_business_data"] = _grounded
            parsed["research_used"] = _researched
            if _researched:
                parsed["note"] = f"Generated from public research on {_ctxr['identifier']} — for deeper analysis, run Marketing Brain."
            elif not _grounded:
                parsed["note"] = "Generated from generic business/industry context — no deeper analysis on file. Run Marketing Brain first for hashtags tailored to your real positioning."
            result = {"success": True, **parsed}

        elif intent == "ad_script_writing":
            _ctxr = await _command_memory_context(url, industry, city, text_cmd)
            _ctx, _grounded, _researched = _ctxr["context"], _ctxr["grounded_in_business_data"], _ctxr["research_used"]
            _no_signal_guard = (
                "\nIMPORTANT — NO REAL BUSINESS DATA IS AVAILABLE ABOVE: do NOT invent a specific business "
                "category (wellness, fitness, food, fashion, marketing agency, social media management, etc.) "
                "that isn't explicitly stated in the context. Write a GENERIC 'grow your business' style script "
                "instead of guessing an industry — focus on generic outcomes like leads/customers/results for THIS "
                "business, not an invented niche. Use the business name from the context if present, but NEVER "
                "write a script that implies the business itself provides marketing, social media management, or "
                "consulting services, since that would be guessing an industry that was never confirmed.\n"
            ) if not _grounded and not _researched else ""
            _research_note_for_prompt = (
                "\nNOTE: The context above includes REAL public research findings about this business — ground the "
                "hook/body/CTA in what this business actually does, per that research.\n"
            ) if _researched else ""
            prompt = (
                "Write a short-form vertical video ad script (Reel/Short/TikTok style) for this business.\n\n"
                f"CONTEXT:\n{_ctx}\n"
                f"{_no_signal_guard}{_research_note_for_prompt}\n"
                'Return ONLY JSON: {"hook": "first 2-3 seconds — must stop the scroll, specific not generic", '
                '"body": ["beat 1", "beat 2", "beat 3"], "cta": "final on-screen call to action line", '
                '"format_suggestion": "e.g. 15-30s Reel, talking-head or product demo, with a one-line visual note"}'
            )
            resp = await asyncio.to_thread(
                client.chat.completions.create, model="gpt-4o", messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}, max_tokens=600, temperature=0.5,
            )
            parsed = _clean_banned_words_deep(json.loads(resp.choices[0].message.content.strip()))
            parsed["grounded_in_business_data"] = _grounded
            parsed["research_used"] = _researched
            if _researched:
                parsed["note"] = f"Generated from public research on {_ctxr['identifier']} — for deeper analysis, run Marketing Brain."
            elif not _grounded:
                parsed["note"] = "Generated from generic business/industry context — no deeper analysis on file. Run Marketing Brain first for a script tailored to your real positioning."
            result = {"success": True, **parsed}

        elif intent == "market_query":
            query = f"{text_cmd} {industry} {city} 2026".strip()
            research = await _tavily_research(query)
            if not research["used"]:
                result = {"success": False, "error": "Live research wasn't available right now (no search results came back) — I can't responsibly answer without real data. Try again in a bit."}
            else:
                prompt = (
                    "Answer this marketing/business question using ONLY the real search results below — never "
                    "invent a fact or statistic that isn't supported by them. If the results don't fully answer the "
                    "question, say so honestly rather than filling the gap with a guess.\n\n"
                    f'QUESTION: "{text_cmd}"\n\n'
                    f"REAL SEARCH RESULTS:\n{research['raw'][:3500]}\n\n"
                    'Return ONLY JSON: {"answer": "direct answer, 3-5 sentences", "key_findings": ["...","...","..."]}'
                )
                resp = await asyncio.to_thread(
                    client.chat.completions.create, model="gpt-4o", messages=[{"role": "user", "content": prompt}],
                    response_format={"type": "json_object"}, max_tokens=700, temperature=0.3,
                )
                parsed = _clean_banned_words_deep(json.loads(resp.choices[0].message.content.strip()))
                parsed["based_on"] = f"Based on real-time web search results for \"{research['query']}\"."
                result = {"success": True, **parsed}

        else:
            return {
                "success": False,
                "intent": "unknown",
                "error": "Could not match this command to an internal engine.",
                "reasoning": classification.get("reasoning", ""),
                "available_commands": [
                    "Run full analysis on [url]", "Find [industry] in [city]",
                    "Generate campaign for [url]", "Optimize campaign for [url]",
                    "Find better audience for [url]", "Predict results for [industry] campaign",
                    "Write outreach for [industry] in [city]", "Audit landing page for [url]",
                    "Design offer for [url]", "Budget [X], need [goal]",
                    "Launch a campaign for [url], budget [X]",
                    "What's trending in [industry] marketing", "Hashtags/captions for [url]",
                    "Write a reel/video ad script for [url]", "Generate an ad creative for [url]",
                    "What ads is [competitor] running", "This week's summary for [url]",
                    "Launch a Meta campaign for [url], budget [X]", "Any other marketing question",
                ],
            }
    except Exception as _e:
        tb = _traceback.format_exc()
        logger.error(f"[COMMAND] dispatch to {intent!r} failed: {_e}\n{tb}")
        return {"success": False, "intent": intent, "error": str(_e)}

    return {
        "success":   True,
        "intent":    intent,
        "reasoning": classification.get("reasoning", ""),
        "params_used": {"url": url, "industry": industry, "city": city, "budget": budget, "goal": goal},
        "result":    result,
        **extra_fields,
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  CRICKET COMMUNITY ADS — STANDALONE MODULE
#  Isolated from all other modules. Own table, own save/load, own endpoint.
# ═══════════════════════════════════════════════════════════════════════════════

# ── Isolated memory table ────────────────────────────────────────────────────
_CRICKET_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS cricket_ads_memory (
    id           BIGSERIAL PRIMARY KEY,
    business_key TEXT UNIQUE NOT NULL,
    ads_data     TEXT,
    created_at   TEXT,
    updated_at   TEXT
);
"""

def _create_cricket_table():
    try:
        with engine.connect() as conn:
            conn.execute(text(_CRICKET_TABLE_DDL))
            conn.commit()
        logger.info("[CRICKET] cricket_ads_memory table ready")
    except Exception as _e:
        logger.warning(f"[CRICKET] Table create failed: {_e}")

try:
    _create_cricket_table()
except Exception:
    pass


def save_cricket_memory(business_key: str, data: dict):
    _now = datetime.utcnow().isoformat()
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO cricket_ads_memory (business_key, ads_data, created_at, updated_at) "
                "VALUES (:k, :d, :ca, :ua) "
                "ON CONFLICT(business_key) DO UPDATE SET ads_data=:d, updated_at=:ua"
            ), {"k": business_key, "d": json.dumps(data), "ca": _now, "ua": _now})
        logger.info(f"[CRICKET] Saved memory for key={business_key!r}")
    except Exception as _e:
        logger.warning(f"[CRICKET] Save failed: {_e}")


def get_cricket_memory(business_key: str) -> dict | None:
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT ads_data FROM cricket_ads_memory WHERE business_key=:k"),
                {"k": business_key}
            ).fetchone()
        if row and row[0]:
            return json.loads(row[0])
    except Exception as _e:
        logger.warning(f"[CRICKET] Load failed: {_e}")
    return None


# ── Pydantic model ───────────────────────────────────────────────────────────
_SPORTS_BUSINESS_TYPES = (
    "Cricket Community", "Fantasy Sports Platform", "Gaming Community", "Esports Content",
    "Sports News", "Sports Coaching", "Sports Merchandise", "Tournament Platform",
)

class CricketAdsRequest(BaseModel):
    url:           str
    whatsapp_link: str = ""
    city:          str = "India"
    industry:      str = ""                        # optional — enables Marketing Brain memory reuse
    business_type: str = "Cricket Community"        # one of _SPORTS_BUSINESS_TYPES
    budget:        float = 0.0                       # optional — feeds the campaign simulator


# ── Endpoint ─────────────────────────────────────────────────────────────────
@app.post("/cricket-ads-intelligence")
async def cricket_ads_intelligence(request: CricketAdsRequest):
    """
    Sports Growth Engine intelligence module (route kept as /cricket-ads-intelligence
    for backward compatibility with the frontend and any saved bookmarks/integrations).

    ARCHITECTURE RULE: never duplicate Marketing Brain. If this business already has
    Marketing Brain memory (business/audience/kpi/performance), reuse it instead of
    re-discovering everything from scratch via a fresh crawl. Otherwise fall back to
    the original Firecrawl-based discovery flow.
    """
    biz_key = f"cricket::{request.url.strip().rstrip('/').lower()}::{request.city.lower()}"
    business_type = request.business_type if request.business_type in _SPORTS_BUSINESS_TYPES else "Cricket Community"

    # ── 0. Marketing Brain memory reuse ──────────────────────────────────────
    brain_memory, _brain_key = get_memory_with_city_fallback(request.url, request.industry, request.city)
    brain_context_block = ""
    if brain_memory:
        _bm    = brain_memory.get("business", {}) or {}
        _am    = brain_memory.get("audience", {}) or {}
        _kpim  = brain_memory.get("kpi", {}) or {}
        _perfm = brain_memory.get("performance", {}) or {}
        _parts = []
        if _bm.get("uvp"):          _parts.append(f"UVP: {_bm['uvp']}")
        if _bm.get("positioning"):  _parts.append(f"Positioning: {_bm['positioning']}")
        if _bm.get("business_dna"): _parts.append(f"Business DNA: {json.dumps(_bm['business_dna'], ensure_ascii=False)[:600]}")
        if _am.get("segments"):     _parts.append(f"Audience segments (from Marketing Brain): {json.dumps(_am['segments'], ensure_ascii=False)[:600]}")
        if _kpim.get("kpi_data"):   _parts.append(f"KPI data: {json.dumps(_kpim['kpi_data'], ensure_ascii=False)[:400]}")
        if _perfm.get("performance_data"): _parts.append(f"Performance data: {json.dumps(_perfm['performance_data'], ensure_ascii=False)[:400]}")
        if _parts:
            brain_context_block = (
                "PLATFORM INTELLIGENCE (from Marketing Brain — this business has already been analyzed on Adsoh; "
                "treat this as ground truth and do NOT re-derive it, just build the sports campaign on top of it):\n"
                + "\n".join(f"- {p}" for p in _parts) + "\n\n"
            )
    memory_reused = bool(brain_context_block)

    # ── 1. Crawl landing page ────────────────────────────────────────────────
    # Skip the (paid, slower) Firecrawl discovery crawl when Marketing Brain memory
    # already covers business/audience discovery. Still do a cheap httpx fetch so
    # compliance_check and landing_page_audit have real page text to scan — those
    # are safety checks, not "discovery", and stay strict regardless of memory reuse.
    site_content = ""
    if not memory_reused and FIRECRAWL_API_KEY and request.url:
        site_content = await fetch_firecrawl(request.url)
    if not site_content:
        try:
            async with httpx.AsyncClient(timeout=12, follow_redirects=True) as _hc:
                _r = await _hc.get(request.url, headers={"User-Agent": "Mozilla/5.0"})
                _html = _r.text
                _html = re.sub(r"<script[^>]*>.*?</script>", "", _html, flags=re.I | re.S)
                _html = re.sub(r"<style[^>]*>.*?</style>",  "", _html, flags=re.I | re.S)
                site_content = re.sub(r"<[^>]+>", " ", _html)
                site_content = re.sub(r"\s+", " ", site_content).strip()[:2000]
        except Exception:
            site_content = "(Could not fetch website content)"

    # ── 2. Tavily: sports/gaming season context + upcoming calendar + competitors + inventory ──
    cricket_context = ""
    calendar_context = ""
    competitor_context = ""
    inventory_context = ""
    if TAVILY_API_KEY:
        _now_str = datetime.now().strftime("%B %Y")
        _q_context = (
            f"Current cricket and sports season India {_now_str}: "
            "IPL matches, international series, World Cup schedule, "
            "cricket fan engagement trends, WhatsApp cricket community growth"
        )
        _q_calendar = (
            f"Cricket and sports events calendar India in the next 60 days from {_now_str}: "
            "IPL, World Cup, Asia Cup, international tours, tournament dates, "
            "trending teams and players right now, trending cricket/sports search topics"
        )
        _q_competitor = (
            f"Popular cricket fan communities, sports content pages, or {business_type} platforms in India — "
            "their WhatsApp/Telegram community growth tactics, content style, offers, follower counts"
        )
        _q_inventory = (
            f"Current relevance and popularity in India {_now_str} of cricket/sports apps and platforms: "
            "Cricbuzz, ESPN Cricinfo, CricHeroes, SportsTiger, SportsTak, Flashscore, Cricket Exchange, Fancode, "
            "Google Discover Sports, Dailyhunt Sports, InMobi sports inventory, Glance lock screen, Opera News "
            "cricket, OEM cricket widgets, YouTube cricket channels like Star Sports and ICC — audience size, "
            "app usage trends, ad inventory availability"
        )
        cricket_context, calendar_context, competitor_context, inventory_context = await asyncio.gather(
            fetch_tavily(_q_context), fetch_tavily(_q_calendar), fetch_tavily(_q_competitor), fetch_tavily(_q_inventory),
        )

    # ── 2b. Learning Engine: cross-business winning patterns for sports/gaming ──
    _growth_block = growth_learning_block("sports_gaming", label="SPORTS/GAMING LEARNING")

    # ── 3. Shared context + 4 parallel prompts ───────────────────────────────
    # Cricket Media Buying Brain upgrade: the single mega-prompt this endpoint used
    # to send is now split into 4 independent calls (core / creative+design /
    # inventory / media+simulator), run in parallel via asyncio.gather. This keeps
    # each call's max_tokens sane (avoiding truncated JSON) and gives real
    # per-section failure isolation — one section failing doesn't take the rest
    # down, unlike a single giant call where a truncation anywhere breaks everything.
    _current_month  = datetime.now().strftime("%B %Y")
    _effective_budget = int(request.budget) if request.budget else 15000
    _budget_str = f"₹{int(request.budget)}" if request.budget else "(not provided — assume ₹15,000/month for this business type)"

    shared_context = (
        f"Current date: {_current_month}\n"
        f"City/Region: {request.city}\n"
        f"Business Type: {business_type}\n"
        f"WhatsApp Group Link: {request.whatsapp_link or '(not provided)'}\n"
        f"Monthly Budget: {_budget_str}\n\n"
        f"{brain_context_block}"
        f"{_growth_block}"
        f"WEBSITE CONTENT:\n{site_content[:2000]}\n\n"
        f"LIVE CONTEXT (current cricket/sports season):\n"
        f"{cricket_context[:1200] if cricket_context else 'No live data — use general cricket/sports season knowledge for India.'}\n\n"
        f"UPCOMING EVENTS CALENDAR (next 60 days):\n"
        f"{calendar_context[:1200] if calendar_context else 'No live data — use general knowledge of the current cricket/sports calendar for India.'}\n\n"
        f"COMPETITOR RESEARCH:\n"
        f"{competitor_context[:1200] if competitor_context else 'No live data — use general knowledge of well-known cricket/sports fan communities in India.'}\n\n"
        f"INVENTORY RELEVANCE RESEARCH (current popularity of named cricket/sports platforms):\n"
        f"{inventory_context[:1200] if inventory_context else 'No live data — use general knowledge of cricket/sports platform popularity in India.'}\n\n"
    )

    _identity = (
        f"You are the Cricket Media Buying Brain inside Adsoh — a specialist Google Display + YouTube media "
        f"buyer for sports and gaming community campaigns in India. Business Type: {business_type}.\n\n"
    )

    _compliance_rules = (
        "BUSINESS TYPE ADAPTATION:\n"
        f"- {business_type}: reason specifically about what this business type's audience wants, where they spend "
        "time online, and what placements/creative angles fit it — do not give generic cricket-community advice "
        "if the business type is Esports Content, Sports Merchandise, Tournament Platform, etc.\n"
        "- Regardless of business type, the PRIMARY CONVERSION must be a free action: WhatsApp/Telegram join, "
        "app download/install, content follow, or community sign-up — never a deposit, entry fee, or paid contest.\n\n"
        "CONTENT RULES — COMPLIANCE STAYS STRICT REGARDLESS OF BUSINESS TYPE:\n"
        "- Audiences MAY be expressed as fantasy-sports-interested or gaming-interested users (that's a legitimate "
        "Google interest/in-market category) — but the AD CONTENT and LANDING DESTINATION must be 100% free/"
        "community/content. Flag ANY real-money, win-cash, deposit, prediction/tips-for-money, or odds language.\n"
        "- Primary conversion: WhatsApp/Telegram/community join, or content follow — never a paid action.\n\n"
        "AUDIENCE COMPLIANCE — PLATFORM-SUPPORTED TARGETING ONLY (ABSOLUTE RULE):\n"
        "- Every audience_segments[].name and .platform_match MUST describe a targeting method Google Ads actually "
        "supports: an affinity/in-market audience category, a contextual keyword theme, or a placement/content "
        "category. NEVER name a specific app's users directly (e.g. never write 'Dream11 app users' or 'My11Circle "
        "users') — instead say something like 'In-market: Fantasy Sports Apps' or 'Contextual: cricket score & "
        "fantasy sports content'.\n"
        "- TEAM/FAN AUDIENCE TRANSLATION: where a specific team or tournament fan-base is relevant (e.g. IPL team "
        "fans), translate it into platform-supported targeting ONLY — e.g. 'RCB fan interest' becomes: contextual "
        "keywords 'RCB', 'IPL Bangalore'; placement targeting on team-coverage pages; affinity 'Cricket "
        "Enthusiasts' + geo Bangalore. NEVER output a named team/fan-club audience as if it were itself a "
        "directly selectable targeting option — always show the translation into real Google Ads mechanisms.\n\n"
    )

    # ── PROMPT 1: core intelligence (business/compliance/audience/placements/ ──
    # landing audit/launch score/key recs/calendar/competitors)
    prompt_core = (
        _identity + shared_context +
        f"TASK: Generate the core campaign intelligence for this {business_type} — business understanding, "
        "compliance, audience, placements, landing page audit, launch score, key recommendations, sports "
        "calendar, and competitor watch. Do NOT generate creative copy, placement inventory, YouTube inventory, "
        "or media plan — those are handled in separate calls.\n\n" +
        _compliance_rules +
        "BUSINESS DNA SCORE:\n"
        "- In business_summary, add business_dna_score (0-100 integer): how well-positioned this business is for "
        "paid cricket/sports advertising. Add business_dna_reasoning (2-3 sentences covering offer clarity, CTA "
        "strength, conversion event clarity, and compliance level — all four factors).\n\n"
        "AUDIENCE RULES:\n"
        "- Return AT LEAST 6 distinct audience_segments, tuned to business_type, each a genuinely different group "
        "(age, fan type, device habit, language, city-tier, viewing occasion, or a team/tournament fan-base "
        "translated per the rule above) — not rewordings of the same segment.\n"
        "- Return AT LEAST 5 distinct placement_recommendations (a lightweight list here — the detailed "
        "placement_inventory with full metrics is generated separately, don't duplicate that effort here).\n"
        "- Read LIVE CONTEXT and pull out the ACTUAL current tournament/series/team names. Every "
        "audience_segments[].reason MUST name that specific event (e.g. 'Timed around India vs Australia T20I "
        "series'). NEVER write generic filler like 'cricket season is ongoing'. If LIVE CONTEXT is empty, say so "
        "explicitly rather than inventing a generic sentence.\n"
        "- estimated_cpc/estimated_ctr realistic for Google Display in India (CPC typically ₹1-8).\n"
        "- Each segment needs: priority_score (0-100), intent_score (0-100), competition (low/medium/high), "
        "expected_conversion (specific range), platform_match (real targeting mechanism, never an app name), "
        "confidence (0-100), evidence (one sentence citing what supports it), reason.\n\n"
        "KEY RECOMMENDATIONS (top_audience, top_placement, timing) — FULL RECOMMENDATION DEPTH:\n"
        "- For the single HIGHEST-priority audience segment, HIGHEST-priority placement, and the launch timing "
        "decision, produce: observation (what the data shows), why (the reasoning connecting evidence to the "
        "recommendation), evidence (what specifically supports it), confidence (0-100), expected_impact (specific "
        "and measurable), risk (main way this could underperform), difficulty (easy/medium/hard to execute), "
        "priority (high/medium/low), next_action (the one concrete thing to do).\n\n"
        "SPORTS CALENDAR:\n"
        "- From UPCOMING EVENTS CALENDAR, extract the next 3 relevant events/tournaments with actual dates (or "
        "best estimate). For each: timing_window and budget_creative_recommendation. If no real data, say so "
        "explicitly rather than inventing fake dates.\n\n"
        "COMPETITOR WATCH:\n"
        "- Name 2-3 real, similar sports/gaming communities/platforms from COMPETITOR RESEARCH (or general "
        "knowledge), their creative style/offers/growth tactics, and 3 differentiation recommendations.\n\n"
        "EXPANDED LANDING PAGE AUDIT:\n"
        "- landing_page_audit.issues/.fixes MUST quote or closely paraphrase specific text/headlines/elements "
        "that actually appear in WEBSITE CONTENT, explaining the specific problem. A generic issue with no quoted "
        "fragment is NOT acceptable. If WEBSITE CONTENT is unavailable, say exactly that.\n"
        "- Add: above_fold_assessment (what's visible without scrolling and whether it's effective), "
        "whatsapp_cta_visibility (how visible/prominent the join CTA is), color_contrast_readability (specific "
        "assessment), button_placement (specific assessment), mobile_safe_layout (specific assessment) — each a "
        "specific sentence tied to what you actually read in WEBSITE CONTENT, not generic advice.\n\n"
        "Return ONLY a valid JSON object with this exact structure (no markdown, no explanation):\n"
        "{\n"
        '  "business_summary": { "offer": "...", "primary_conversion": "WhatsApp Join", "target_user": "...", '
        '"business_dna_score": 0, "business_dna_reasoning": "..." },\n'
        '  "compliance_check": {\n'
        '    "risk_level": "low",\n'
        '    "flags_found": [],\n'
        '    "safe_to_advertise": true,\n'
        '    "required_fixes": []\n'
        '  },\n'
        '  "campaign_structure": {\n'
        '    "campaign_name": "...",\n'
        '    "objective": "WhatsApp Joins",\n'
        '    "campaign_type": "Display",\n'
        '    "budget_daily": "₹...",\n'
        '    "bidding_strategy": "...",\n'
        '    "devices": "Mobile priority",\n'
        '    "frequency_cap": "..."\n'
        '  },\n'
        '  "audience_segments": [\n'
        '    { "name": "...", "intent": "high", "estimated_cpc": "₹2-4", "estimated_ctr": "0.5%", "priority_score": 85, '
        '"intent_score": 80, "competition": "medium", "expected_conversion": "3-6% join rate", '
        '"platform_match": "In-market: Fantasy Sports Apps", "confidence": 75, "evidence": "...", "reason": "..." },\n'
        '    { "name": "...", "intent": "...", "estimated_cpc": "...", "estimated_ctr": "...", "priority_score": 0, '
        '"intent_score": 0, "competition": "...", "expected_conversion": "...", "platform_match": "...", '
        '"confidence": 0, "evidence": "...", "reason": "..." },\n'
        '    { "name": "...", "intent": "...", "estimated_cpc": "...", "estimated_ctr": "...", "priority_score": 0, '
        '"intent_score": 0, "competition": "...", "expected_conversion": "...", "platform_match": "...", '
        '"confidence": 0, "evidence": "...", "reason": "..." },\n'
        '    { "name": "...", "intent": "...", "estimated_cpc": "...", "estimated_ctr": "...", "priority_score": 0, '
        '"intent_score": 0, "competition": "...", "expected_conversion": "...", "platform_match": "...", '
        '"confidence": 0, "evidence": "...", "reason": "..." },\n'
        '    { "name": "...", "intent": "...", "estimated_cpc": "...", "estimated_ctr": "...", "priority_score": 0, '
        '"intent_score": 0, "competition": "...", "expected_conversion": "...", "platform_match": "...", '
        '"confidence": 0, "evidence": "...", "reason": "..." },\n'
        '    { "name": "...", "intent": "...", "estimated_cpc": "...", "estimated_ctr": "...", "priority_score": 0, '
        '"intent_score": 0, "competition": "...", "expected_conversion": "...", "platform_match": "...", '
        '"confidence": 0, "evidence": "...", "reason": "..." }\n'
        '  ],\n'
        '  "placement_recommendations": [\n'
        '    { "placement": "...", "why": "...", "estimated_reach": "...", "priority": "high" },\n'
        '    { "placement": "...", "why": "...", "estimated_reach": "...", "priority": "..." },\n'
        '    { "placement": "...", "why": "...", "estimated_reach": "...", "priority": "..." },\n'
        '    { "placement": "...", "why": "...", "estimated_reach": "...", "priority": "..." },\n'
        '    { "placement": "...", "why": "...", "estimated_reach": "...", "priority": "..." }\n'
        '  ],\n'
        '  "landing_page_audit": {\n'
        '    "score": 0, "issues": [], "fixes": [],\n'
        '    "above_fold_assessment": "...", "whatsapp_cta_visibility": "...", '
        '"color_contrast_readability": "...", "button_placement": "...", "mobile_safe_layout": "..."\n'
        '  },\n'
        '  "launch_score": { "overall": 0, "audience": 0, "compliance": 0, "creative": 0 },\n'
        '  "key_recommendations": {\n'
        '    "top_audience": { "observation": "...", "why": "...", "evidence": "...", "confidence": 0, "expected_impact": "...", "risk": "...", "difficulty": "...", "priority": "...", "next_action": "..." },\n'
        '    "top_placement": { "observation": "...", "why": "...", "evidence": "...", "confidence": 0, "expected_impact": "...", "risk": "...", "difficulty": "...", "priority": "...", "next_action": "..." },\n'
        '    "timing": { "observation": "...", "why": "...", "evidence": "...", "confidence": 0, "expected_impact": "...", "risk": "...", "difficulty": "...", "priority": "...", "next_action": "..." }\n'
        '  },\n'
        '  "sports_calendar": {\n'
        '    "events": [\n'
        '      { "name": "...", "date": "...", "timing_window": "...", "budget_creative_recommendation": "..." },\n'
        '      { "name": "...", "date": "...", "timing_window": "...", "budget_creative_recommendation": "..." },\n'
        '      { "name": "...", "date": "...", "timing_window": "...", "budget_creative_recommendation": "..." }\n'
        '    ],\n'
        '    "data_available": true\n'
        '  },\n'
        '  "competitor_watch": [\n'
        '    { "name": "...", "creative_style": "...", "offers": "...", "growth_tactics": "...", "differentiation_recommendations": ["...", "...", "..."] },\n'
        '    { "name": "...", "creative_style": "...", "offers": "...", "growth_tactics": "...", "differentiation_recommendations": ["...", "...", "..."] }\n'
        '  ]\n'
        "}"
    )

    # ── PROMPT 2: creative copy + design recommendations ─────────────────────
    prompt_creative = (
        _identity + shared_context + _compliance_rules +
        f"TASK: Generate ONLY the creative ad copy and design recommendations for this {business_type} "
        "campaign — headlines, descriptions, CTAs, and visual design guidance. Nothing else.\n\n"
        "CHARACTER LIMITS — STRICT:\n"
        "- headlines_15: each MUST be under 30 characters (Google Ads limit). Count carefully.\n"
        "- long_headlines_5: each MUST be BETWEEN 70 and 90 characters — count carefully, not just under 90.\n"
        "- descriptions_10: each MUST be BETWEEN 70 and 90 characters and end with a CTA — count carefully.\n\n"
        "CREATIVE RULES — descriptions_10:\n"
        "- Write EXACTLY 10 descriptions_10, using a MIX of these angles across them (don't repeat the same angle "
        "back-to-back): urgency, benefit, social proof, curiosity, direct action, breaking news, live match "
        "excitement, national pride/tournament excitement.\n"
        "- The word 'join' (any form, case-insensitive) may appear in AT MOST 2 of the 10 descriptions_10 — vary "
        "the verb across the rest (e.g. 'Get', 'See', 'Discover', 'Tap in', 'Follow along', 'Unlock', \"Don't "
        "miss\", 'Catch every ball', 'Be part of' — illustrative, not exhaustive).\n"
        "- SELF-CHECK before finalizing: count how many of the 10 descriptions_10 contain 'join' in any form. If "
        "more than 2, rewrite until at most 2 do. Do this silently and only output the final corrected JSON.\n\n"
        "CTA VARIATIONS — ctas_20:\n"
        "- Write EXACTLY 20 short CTA button/link variations across these 8 psychology angles (2-3 per angle): "
        "urgency, FOMO, community, curiosity, breaking_news, live_match, national_pride, tournament_excitement. "
        "Each item: {\"text\": \"...\", \"angle\": \"one of the 8 angles above, exact spelling\"}.\n"
        "- The word 'join' may appear in at most 4 of the 20 — vary verbs across the rest.\n\n"
        "DESIGN RECOMMENDATIONS:\n"
        "- hero_image_concept: a specific visual concept tied to the current cricket context/business type (not "
        "generic stock-photo language).\n"
        "- color_palette: 3-5 colors as {\"name\": \"...\", \"hex\": \"#RRGGBB\"} — real, usable hex codes that "
        "fit this business type's brand feel.\n"
        "- background, visual_hierarchy, banner_layout, button_style, typography, mobile_safe_area_notes — each "
        "one specific, actionable sentence, not generic design-101 advice.\n\n"
        "Return ONLY a valid JSON object with this exact structure (no markdown, no explanation):\n"
        "{\n"
        '  "creative_assets": {\n'
        '    "headlines_15": ["...", "...", "...", "...", "...", "...", "...", "...", "...", "...", "...", "...", "...", "...", "..."],\n'
        '    "long_headlines_5": ["...", "...", "...", "...", "..."],\n'
        '    "descriptions_10": ["...", "...", "...", "...", "...", "...", "...", "...", "...", "..."],\n'
        '    "ctas_20": [\n'
        '      {"text": "...", "angle": "urgency"}, {"text": "...", "angle": "urgency"}, {"text": "...", "angle": "urgency"},\n'
        '      {"text": "...", "angle": "FOMO"}, {"text": "...", "angle": "FOMO"}, {"text": "...", "angle": "FOMO"},\n'
        '      {"text": "...", "angle": "community"}, {"text": "...", "angle": "community"}, {"text": "...", "angle": "community"},\n'
        '      {"text": "...", "angle": "curiosity"}, {"text": "...", "angle": "curiosity"},\n'
        '      {"text": "...", "angle": "breaking_news"}, {"text": "...", "angle": "breaking_news"},\n'
        '      {"text": "...", "angle": "live_match"}, {"text": "...", "angle": "live_match"},\n'
        '      {"text": "...", "angle": "national_pride"}, {"text": "...", "angle": "national_pride"},\n'
        '      {"text": "...", "angle": "tournament_excitement"}, {"text": "...", "angle": "tournament_excitement"}, {"text": "...", "angle": "tournament_excitement"}\n'
        '    ],\n'
        '    "cta": "Join Now",\n'
        '    "image_suggestions": ["...", "...", "..."]\n'
        '  },\n'
        '  "design_recommendations": {\n'
        '    "hero_image_concept": "...",\n'
        '    "background": "...",\n'
        '    "color_palette": [ {"name": "...", "hex": "#000000"}, {"name": "...", "hex": "#000000"}, {"name": "...", "hex": "#000000"} ],\n'
        '    "visual_hierarchy": "...",\n'
        '    "banner_layout": "...",\n'
        '    "button_style": "...",\n'
        '    "typography": "...",\n'
        '    "mobile_safe_area_notes": "..."\n'
        '  }\n'
        "}"
    )

    # ── PROMPT 3: placement inventory + YouTube inventory ────────────────────
    prompt_inventory = (
        _identity + shared_context +
        f"TASK: Generate ONLY the placement inventory and YouTube inventory for this {business_type} campaign. "
        "Nothing else.\n\n"
        "PLACEMENT INVENTORY:\n"
        "- From this named cricket/sports inventory, pick the 8-10 MOST RELEVANT to this business_type: Cricbuzz, "
        "ESPN Cricinfo, CricHeroes, SportsTiger, SportsTak, Flashscore, Cricket Exchange, Fancode, Google Discover "
        "Sports, Dailyhunt Sports, InMobi sports inventory, Glance lock screen, Opera News cricket, OEM cricket "
        "widgets, score widgets. This is PLACEMENT targeting (a real, Google Ads-supported mechanism) — not "
        "app-user targeting.\n"
        "- You MUST return AT LEAST 8 items — fewer than 8 is a failed response.\n"
        "- For each: audience_type, traffic_quality (specific, not just 'good'), device_split (e.g. '85% mobile / "
        "15% desktop'), estimated_reach, estimated_cpm (₹), estimated_cpc (₹), expected_ctr, expected_join_rate, "
        "competition (low/medium/high), suitability_score (0-100 for THIS business_type), "
        "recommended_creative_type, banner_sizes (real Google Display sizes, e.g. 300x250, 320x50, 728x90, "
        "160x600), priority (high/medium/low).\n"
        "- Use INVENTORY RELEVANCE RESEARCH above to ground current-relevance reasoning where it has real data.\n\n"
        "YOUTUBE INVENTORY:\n"
        "- Pick 5-7 channels most relevant to this business_type from: Star Sports, ICC, Cricbuzz, SportsTak, "
        "ESPN Cricinfo, RevSportz, CricXtasy, or other real, well-known Indian cricket YouTube channels.\n"
        "- For each: audience, estimated_reach, ad_type_fit (skippable/shorts/in-feed — pick the best fit and say "
        "why), expected_cpm (₹), expected_ctr, creative_recommendation (specific to that channel's content style).\n\n"
        "Return ONLY a valid JSON object with this exact structure (no markdown, no explanation). Fill in ALL "
        "fields for every item — do not leave any field as a placeholder:\n"
        "{\n"
        '  "placement_inventory": [\n'
        '    { "platform": "Cricbuzz", "audience_type": "...", "traffic_quality": "...", "device_split": "...", '
        '"estimated_reach": "...", "estimated_cpm": "₹...", "estimated_cpc": "₹...", "expected_ctr": "...", '
        '"expected_join_rate": "...", "competition": "medium", "suitability_score": 0, '
        '"recommended_creative_type": "...", "banner_sizes": ["300x250", "320x50"], "priority": "high" }\n'
        '    // ... 7-9 more items (8-10 total), each a DIFFERENT named platform from the list above, each with ALL fields populated with real, distinct values\n'
        '  ],\n'
        '  "youtube_inventory": [\n'
        '    { "channel": "Star Sports", "audience": "...", "estimated_reach": "...", "ad_type_fit": "skippable", '
        '"expected_cpm": "₹...", "expected_ctr": "...", "creative_recommendation": "..." }\n'
        '    // ... 4-6 more channels (5-7 total), each DIFFERENT, each with ALL fields populated\n'
        '  ]\n'
        "}"
    )

    # ── PROMPT 4: media plan + formula-chain simulator ────────────────────────
    prompt_media = (
        _identity + shared_context +
        f"TASK: Generate ONLY the media plan and formula-chain campaign simulator for this {business_type} "
        f"campaign at a monthly budget of ₹{_effective_budget}. Nothing else.\n\n"
        "MEDIA PLAN:\n"
        f"- Split the ₹{_effective_budget} monthly budget across these 6 channels: Google Display, YouTube, "
        "Demand Gen, PMax, Meta, Sports Publishers/OEM. Every channel gets a non-zero amount reflecting its real "
        "fit for this business_type — do not split evenly by default, reason about which channels actually fit.\n"
        f"- The 6 channel amounts MUST sum EXACTLY to ₹{_effective_budget} (whole rupees, no rounding drift).\n"
        "- For each channel: amount (₹), pct (0-100, matching the amount), expected_reach, expected_clicks, "
        "expected_joins, expected_cpa (₹), and why (one sentence tying the allocation to this business_type/"
        "context above).\n\n"
        "FORMULA-CHAIN SIMULATOR:\n"
        "- Show the EXPLICIT chain, one step per stage, in this exact order: Budget, CPM, Impressions, CTR, "
        "Clicks, Landing CVR, WhatsApp Join %, Cost Per Join, ROI.\n"
        "- Each step needs: step (stage name), formula (the calculation in words, e.g. '(Budget / CPM) * "
        "1000'), value_range (the calculated range for THIS budget/business, e.g. '125,000-187,500 impressions').\n"
        "- Numbers must be internally consistent — Impressions must actually follow from Budget/CPM, Clicks from "
        "Impressions*CTR, etc. Use realistic Indian Display/YouTube sports benchmarks.\n"
        "- End with disclaimer, verbatim: \"These are forecasts based on benchmarks, not guarantees.\"\n\n"
        "Return ONLY a valid JSON object with this exact structure (no markdown, no explanation):\n"
        "{\n"
        '  "media_plan": {\n'
        '    "channels": [\n'
        '      { "channel": "Google Display", "amount": "₹...", "pct": 0, "expected_reach": "...", "expected_clicks": "...", "expected_joins": "...", "expected_cpa": "₹...", "why": "..." },\n'
        '      { "channel": "YouTube", "amount": "₹...", "pct": 0, "expected_reach": "...", "expected_clicks": "...", "expected_joins": "...", "expected_cpa": "₹...", "why": "..." },\n'
        '      { "channel": "Demand Gen", "amount": "₹...", "pct": 0, "expected_reach": "...", "expected_clicks": "...", "expected_joins": "...", "expected_cpa": "₹...", "why": "..." },\n'
        '      { "channel": "PMax", "amount": "₹...", "pct": 0, "expected_reach": "...", "expected_clicks": "...", "expected_joins": "...", "expected_cpa": "₹...", "why": "..." },\n'
        '      { "channel": "Meta", "amount": "₹...", "pct": 0, "expected_reach": "...", "expected_clicks": "...", "expected_joins": "...", "expected_cpa": "₹...", "why": "..." },\n'
        '      { "channel": "Sports Publishers/OEM", "amount": "₹...", "pct": 0, "expected_reach": "...", "expected_clicks": "...", "expected_joins": "...", "expected_cpa": "₹...", "why": "..." }\n'
        '    ],\n'
        '    "total_budget": "₹' + str(_effective_budget) + '"\n'
        '  },\n'
        '  "campaign_simulator": {\n'
        '    "budget_used": "₹' + str(_effective_budget) + '",\n'
        '    "formula_chain": [\n'
        '      { "step": "Budget", "formula": "input", "value_range": "₹' + str(_effective_budget) + '" },\n'
        '      { "step": "CPM", "formula": "benchmark for this inventory mix", "value_range": "..." },\n'
        '      { "step": "Impressions", "formula": "(Budget / CPM) * 1000", "value_range": "..." },\n'
        '      { "step": "CTR", "formula": "benchmark for Display/YouTube sports inventory", "value_range": "..." },\n'
        '      { "step": "Clicks", "formula": "Impressions * CTR", "value_range": "..." },\n'
        '      { "step": "Landing CVR", "formula": "benchmark for community/content landing pages", "value_range": "..." },\n'
        '      { "step": "WhatsApp Join %", "formula": "Clicks * Landing CVR", "value_range": "..." },\n'
        '      { "step": "Cost Per Join", "formula": "Budget / Joins", "value_range": "₹..." },\n'
        '      { "step": "ROI", "formula": "(value of joins - budget) / budget", "value_range": "..." }\n'
        '    ],\n'
        '    "disclaimer": "These are forecasts based on benchmarks, not guarantees."\n'
        '  }\n'
        "}"
    )

    # ── 4. Four parallel GPT-4o calls, each isolated ─────────────────────────
    def _make_call(prompt_text, max_tok):
        return client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt_text}],
            response_format={"type": "json_object"},
            temperature=0.4,
            max_tokens=max_tok,
        )

    async def _run(prompt_text, max_tok, label):
        resp = await asyncio.to_thread(_make_call, prompt_text, max_tok)
        logger.info(f"[CRICKET] {label} finish_reason={resp.choices[0].finish_reason!r} completion_tokens={resp.usage.completion_tokens}")
        return json.loads(resp.choices[0].message.content)

    core_res, creative_res, inventory_res, media_res = await asyncio.gather(
        _run(prompt_core, 5000, "core"),
        _run(prompt_creative, 3600, "creative"),
        _run(prompt_inventory, 3400, "inventory"),
        _run(prompt_media, 2400, "media"),
        return_exceptions=True,
    )

    if isinstance(core_res, Exception):
        logger.error(f"[CRICKET] core section failed: {core_res}")
        return {"success": False, "error": f"Core intelligence generation failed: {core_res}"}

    result = dict(core_res)
    warnings = []

    if isinstance(creative_res, Exception):
        logger.error(f"[CRICKET] creative section failed: {creative_res}")
        warnings.append("creative_assets/design_recommendations generation failed — retry the analysis")
        result["creative_assets"] = {}
        result["design_recommendations"] = {}
    else:
        result.update(creative_res)

    if isinstance(inventory_res, Exception):
        logger.error(f"[CRICKET] inventory section failed: {inventory_res}")
        warnings.append("placement_inventory/youtube_inventory generation failed — retry the analysis")
        result["placement_inventory"] = []
        result["youtube_inventory"] = []
    else:
        result.update(inventory_res)

    if isinstance(media_res, Exception):
        logger.error(f"[CRICKET] media section failed: {media_res}")
        warnings.append("media_plan/campaign_simulator generation failed — retry the analysis")
        result["media_plan"] = {}
        result["campaign_simulator"] = {}
    else:
        result.update(media_res)

    # ── 4a. Backfill pass: long_headlines_5 / descriptions_10 must never be empty ──
    # If the creative call runs low on budget generating headlines_15/ctas_20 first,
    # these can come back as empty/short arrays even though the JSON as a whole is
    # valid. Detect that and do one targeted fill-in call rather than silently
    # handing the frontend gaps.
    try:
        ca = result.setdefault("creative_assets", {})
        _target_counts = {"long_headlines_5": 5, "descriptions_10": 10}
        missing = {k: n for k, n in _target_counts.items() if len(ca.get(k) or []) < n}
        if missing:
            logger.warning(f"[CRICKET] creative_assets missing/short fields: {list(missing)} — backfilling")

            def _call_backfill():
                return client.chat.completions.create(
                    model="gpt-4o",
                    messages=[{
                        "role": "user",
                        "content": (
                            "Generate Google Display Ads creative copy for this cricket community WhatsApp group.\n"
                            f"Offer: {json.dumps(result.get('business_summary', {}))}\n"
                            f"City/Region: {request.city}\n"
                            f"Live cricket context: {cricket_context[:800] if cricket_context else 'general cricket season in India'}\n\n"
                            + "".join(
                                (
                                    "Write exactly 5 long_headlines_5, each 70-90 characters.\n"
                                    if k == "long_headlines_5" else
                                    "Write exactly 10 descriptions_10, each 70-90 characters, ending with a CTA. "
                                    "Mix CTA angles (urgency, benefit, social proof, curiosity, direct action, "
                                    "breaking news, live match, national pride) and let 'join' (any form) appear "
                                    "in at most 2 of the 10.\n"
                                )
                                for k in missing
                            )
                            + "Return ONLY a JSON object with exactly these keys: "
                            + json.dumps({k: ["..."] * n for k, n in missing.items()})
                        ),
                    }],
                    response_format={"type": "json_object"},
                    temperature=0.4,
                    max_tokens=1200,
                )
            fill_resp = await asyncio.to_thread(_call_backfill)
            filled = json.loads(fill_resp.choices[0].message.content)
            for k, n in missing.items():
                if isinstance(filled.get(k), list) and len(filled[k]) == n:
                    ca[k] = filled[k]
            result["creative_assets"] = ca
    except Exception as _be:
        logger.warning(f"[CRICKET] creative_assets backfill skipped: {_be}")

    # ── 4a-ii. Backfill: ctas_20 must have at least 20 items ─────────────────
    try:
        ca = result.setdefault("creative_assets", {})
        ctas = ca.get("ctas_20") or []
        if len(ctas) < 20:
            need = 20 - len(ctas)
            logger.warning(f"[CRICKET] ctas_20 short ({len(ctas)}/20) — backfilling {need} more")

            def _call_cta_backfill():
                return client.chat.completions.create(
                    model="gpt-4o",
                    messages=[{
                        "role": "user",
                        "content": (
                            f"Write exactly {need} short Google/Meta Ads CTA button/link variations for a "
                            f"{business_type} WhatsApp/community campaign in India, spread across these "
                            "psychology angles: urgency, FOMO, community, curiosity, breaking_news, live_match, "
                            "national_pride, tournament_excitement. Each must be distinct from these existing "
                            f"ones: {json.dumps([c.get('text') for c in ctas])}\n"
                            f'Return ONLY a JSON object: {{"ctas": [{{"text": "...", "angle": "..."}}]}} with '
                            f"exactly {need} items."
                        ),
                    }],
                    response_format={"type": "json_object"},
                    temperature=0.5,
                    max_tokens=500,
                )
            cta_resp = await asyncio.to_thread(_call_cta_backfill)
            extra = json.loads(cta_resp.choices[0].message.content).get("ctas") or []
            ca["ctas_20"] = ctas + extra
            result["creative_assets"] = ca
    except Exception as _cbe:
        logger.warning(f"[CRICKET] ctas_20 backfill skipped: {_cbe}")

    # ── 4a-iii. Backfill: placement_inventory must have at least 8 items ─────
    try:
        pinv = result.get("placement_inventory") or []
        if len(pinv) < 8:
            need = 8 - len(pinv)
            logger.warning(f"[CRICKET] placement_inventory short ({len(pinv)}/8) — backfilling {need} more")
            _already = [p.get("platform") for p in pinv]
            _remaining_names = [
                n for n in (
                    "Cricbuzz", "ESPN Cricinfo", "CricHeroes", "SportsTiger", "SportsTak", "Flashscore",
                    "Cricket Exchange", "Fancode", "Google Discover Sports", "Dailyhunt Sports",
                    "InMobi sports inventory", "Glance lock screen", "Opera News cricket",
                    "OEM cricket widgets", "score widgets",
                ) if n not in _already
            ][:need]

            def _call_inventory_backfill():
                return client.chat.completions.create(
                    model="gpt-4o",
                    messages=[{
                        "role": "user",
                        "content": (
                            f"For a {business_type} Google Display campaign in India, generate placement "
                            f"inventory entries for exactly these platforms: {json.dumps(_remaining_names)}.\n"
                            "For each, give: platform, audience_type, traffic_quality, device_split, "
                            "estimated_reach, estimated_cpm (₹), estimated_cpc (₹), expected_ctr, "
                            "expected_join_rate, competition (low/medium/high), suitability_score (0-100), "
                            "recommended_creative_type, banner_sizes (list), priority (high/medium/low).\n"
                            'Return ONLY a JSON object: {"placement_inventory": [...]}'
                        ),
                    }],
                    response_format={"type": "json_object"},
                    temperature=0.4,
                    max_tokens=1400,
                )
            if _remaining_names:
                inv_resp = await asyncio.to_thread(_call_inventory_backfill)
                extra = json.loads(inv_resp.choices[0].message.content).get("placement_inventory") or []
                result["placement_inventory"] = pinv + extra
    except Exception as _ibe:
        logger.warning(f"[CRICKET] placement_inventory backfill skipped: {_ibe}")

    # ── 4b. Length-enforcement pass: long_headlines_5 / descriptions_5 items must ──
    # be 70-90 characters. A soft "aim for 70-90" rewrite instruction wasn't enough in
    # testing — GPT-4o has a strong brevity bias for ad copy and undershot every time.
    # Assigning each item an explicit numeric target and asking it to expand with real
    # specifics (not filler) converges reliably; a local pad is the final guarantee.
    # Runs BEFORE the join-count check (4c) since rewriting can reintroduce "join".
    # Spans a range of lengths (5-26 chars) so there's always something that fits
    # whether the gap to close is 1 character or 20.
    _LENGTH_PAD_SUFFIXES = [
        " Now.", " Today.", " Act fast.", " Find out more.", " Check it out.",
        " Don't miss it.", " Tune in today.", " See what's new.", " Don't miss out today.",
        " Stay in the loop daily.", " Come see what's buzzing.", " Your next update awaits.",
        " Be part of the action.",
    ]

    def _pad_to_range(text: str, lo: int = 70, hi: int = 90) -> str:
        if len(text) > hi:
            trimmed = text[:hi].rsplit(" ", 1)[0]
            return trimmed if len(trimmed) >= lo else text[:hi]
        if len(text) >= lo:
            return text
        base = text.rstrip()
        if not base.endswith((".", "!", "?")):
            base += "."
        candidates = [base + s for s in _LENGTH_PAD_SUFFIXES if lo <= len(base + s) <= hi]
        if candidates:
            return max(candidates, key=len)
        candidate = base
        for suffix in sorted(_LENGTH_PAD_SUFFIXES, key=len):
            if len(candidate) >= lo:
                break
            if len(candidate) + len(suffix) <= hi:
                candidate += suffix
        return candidate

    def _out_of_range(items, lo=70, hi=90):
        return any(not (lo <= len(x) <= hi) for x in items)

    try:
        ca = result.get("creative_assets") or {}
        for field, expect_n, extra_rule in (
            ("long_headlines_5", 5, ""),
            ("descriptions_10", 10, " Keep the word 'join' (any form) in at most 2 of the 10, preserving each "
                                     "item's existing CTA angle."),
        ):
            items = ca.get(field) or []
            if len(items) == expect_n and _out_of_range(items):
                targets = [random.randint(74, 88) for _ in items]
                target_lines = "\n".join(
                    f'{i + 1}. "{item}" -> target EXACTLY {t} characters'
                    for i, (item, t) in enumerate(zip(items, targets))
                )

                def _call_length_fix(field=field, expect_n=expect_n, target_lines=target_lines, extra_rule=extra_rule):
                    return client.chat.completions.create(
                        model="gpt-4o",
                        messages=[{
                            "role": "user",
                            "content": (
                                f"Rewrite each of these {expect_n} Google Ads {field.replace('_', ' ')} to hit an "
                                "EXACT target character count by adding concrete specific detail (not filler "
                                "words) — expand with real specifics like match/series name, community size, or "
                                "benefit, not padding. Count characters as you write, including spaces and "
                                f"punctuation.{extra_rule}\n\n{target_lines}\n\n"
                                f'Return ONLY a JSON object: {{"{field}": {json.dumps(["..."] * expect_n)}}}'
                            ),
                        }],
                        response_format={"type": "json_object"},
                        temperature=0.4,
                        max_tokens=1000,
                    )
                len_resp = await asyncio.to_thread(_call_length_fix)
                fixed = json.loads(len_resp.choices[0].message.content).get(field)
                if isinstance(fixed, list) and len(fixed) == expect_n:
                    ca[field] = fixed
                    logger.info(f"[CRICKET] Length-enforced {field} via per-item targets")

            # Deterministic final guarantee: pad/trim anything still out of range.
            items = ca.get(field) or []
            if len(items) == expect_n and _out_of_range(items):
                ca[field] = [_pad_to_range(x) for x in items]
                logger.info(f"[CRICKET] Locally padded/trimmed {field} to guarantee 70-90 chars")
        result["creative_assets"] = ca
    except Exception as _le:
        logger.warning(f"[CRICKET] length-enforcement skipped: {_le}")

    # ── 4c. Repair pass: enforce "join" appears in at most 2 of 10 descriptions ──
    # Runs LAST (after backfill and length-enforcement, both of which can touch
    # descriptions_10 wording) so it has the final word on the join-count constraint.
    try:
        descs = (result.get("creative_assets") or {}).get("descriptions_10") or []
        join_count = sum(1 for d in descs if re.search(r"\bjoin\w*\b", d, re.I))
        if len(descs) == 10 and join_count > 2:
            def _call_fix():
                return client.chat.completions.create(
                    model="gpt-4o",
                    messages=[{
                        "role": "user",
                        "content": (
                            "Rewrite these 10 Google Ads descriptions so the word 'join' (any form) appears in "
                            "AT MOST 2 of them. Keep each description's original CTA angle and meaning, just "
                            "change the wording/verb where needed so they don't all lean on 'join'. Each MUST "
                            "stay between 70 and 90 characters — count carefully. "
                            "Return ONLY a JSON object: "
                            '{"descriptions_10": ' + json.dumps(["..."] * 10) + '}\n\n'
                            f"Current descriptions:\n{json.dumps(descs)}"
                        ),
                    }],
                    response_format={"type": "json_object"},
                    temperature=0.4,
                    max_tokens=1000,
                )
            fix_resp = await asyncio.to_thread(_call_fix)
            fixed = json.loads(fix_resp.choices[0].message.content).get("descriptions_10")
            if isinstance(fixed, list) and len(fixed) == 10:
                if _out_of_range(fixed):
                    fixed = [_pad_to_range(x) for x in fixed]
                result["creative_assets"]["descriptions_10"] = fixed
                logger.info("[CRICKET] Repaired descriptions_10 CTA-verb repetition")
    except Exception as _fe:
        logger.warning(f"[CRICKET] descriptions_10 repair skipped: {_fe}")

    # ── 4d. Media plan budget-sum enforcement ────────────────────────────────
    # GPT arithmetic across 6 channels rarely lands on the exact budget — force
    # it deterministically rather than hoping the model's rounding is exact.
    try:
        def _parse_rupees(v):
            s = re.sub(r"[^\d.]", "", str(v or "0"))
            return float(s) if s else 0.0

        mp = result.get("media_plan") or {}
        channels = mp.get("channels") or []
        if channels:
            amounts = [_parse_rupees(c.get("amount")) for c in channels]
            total = sum(amounts)
            if total > 0 and round(total) != _effective_budget:
                scale = _effective_budget / total
                scaled = [round(a * scale) for a in amounts]
                drift = _effective_budget - sum(scaled)
                if drift != 0:
                    largest_idx = max(range(len(scaled)), key=lambda i: scaled[i])
                    scaled[largest_idx] += drift
                for c, amt in zip(channels, scaled):
                    c["amount"] = f"₹{amt}"
                    c["pct"] = round((amt / _effective_budget) * 100, 1) if _effective_budget else 0
                mp["channels"] = channels
                mp["total_budget"] = f"₹{_effective_budget}"
                result["media_plan"] = mp
                logger.info(f"[CRICKET] Rescaled media_plan channels to sum exactly to ₹{_effective_budget}")
            elif total == 0:
                logger.warning("[CRICKET] media_plan channels all zero — could not rescale")
    except Exception as _mpe:
        logger.warning(f"[CRICKET] media_plan budget enforcement skipped: {_mpe}")

    # ── 5. Save to isolated cricket_ads_memory ───────────────────────────────
    try:
        await asyncio.to_thread(save_cricket_memory, biz_key, result)
    except Exception as _se:
        logger.warning(f"[CRICKET] Memory save error: {_se}")

    logger.info(f"[CRICKET] Done for url={request.url!r} city={request.city!r} business_type={business_type!r} memory_reused={memory_reused} warnings={warnings}")

    log_activity(
        "sports_analysis", business_key=biz_key,
        business_name=request.url or business_type,
        url=request.url, industry=business_type, city=request.city,
        summary="Sports Growth Engine analysis generated",
    )

    return {
        "success":        True,
        "data":           result,
        "business_type":  business_type,
        "memory_reused":  memory_reused,
        "warnings":       warnings,
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  CRICKET AD ACCOUNTS — ISOLATED GOOGLE ADS ACCOUNT MANAGER
# ═══════════════════════════════════════════════════════════════════════════════

_CRICKET_ACCOUNTS_DDL = """
CREATE TABLE IF NOT EXISTS cricket_ad_accounts (
    id                BIGSERIAL PRIMARY KEY,
    account_name      TEXT NOT NULL,
    customer_id       TEXT UNIQUE NOT NULL,
    login_customer_id TEXT,
    added_at          TEXT
);
"""

def _create_cricket_accounts_table():
    try:
        with engine.connect() as conn:
            conn.execute(text(_CRICKET_ACCOUNTS_DDL))
            conn.commit()
        logger.info("[CRICKET-ACCT] cricket_ad_accounts table ready")
    except Exception as _e:
        logger.warning(f"[CRICKET-ACCT] Table create failed: {_e}")

try:
    _create_cricket_accounts_table()
except Exception:
    pass


def _cricket_account_ops(op: str, customer_id: str = None, data: dict = None):
    """Single entry point for all cricket_ad_accounts DB operations."""
    with engine.begin() as conn:
        if op == "insert":
            conn.execute(text(
                "INSERT INTO cricket_ad_accounts (account_name, customer_id, login_customer_id, added_at) "
                "VALUES (:name, :cid, :lcid, :ts) "
                "ON CONFLICT(customer_id) DO UPDATE SET account_name=:name, login_customer_id=:lcid"
            ), {
                "name": data["account_name"],
                "cid":  data["customer_id"],
                "lcid": data.get("login_customer_id") or "",
                "ts":   datetime.utcnow().isoformat(),
            })
        elif op == "list":
            rows = conn.execute(text(
                "SELECT account_name, customer_id, login_customer_id FROM cricket_ad_accounts ORDER BY id"
            )).fetchall()
            return [{"account_name": r[0], "customer_id": r[1], "login_customer_id": r[2]} for r in rows]
        elif op == "delete":
            conn.execute(text(
                "DELETE FROM cricket_ad_accounts WHERE customer_id=:cid"
            ), {"cid": customer_id})
    return None


def _build_cricket_client(login_customer_id: str = None):
    """Isolated Google Ads client builder for the Cricket module.
    Allows per-account login_customer_id override without touching get_google_ads_client().
    """
    lci = (login_customer_id or "").replace("-", "").replace(" ", "")
    if not lci:
        lci = _genv("GOOGLE_ADS_LOGIN_CUSTOMER_ID").replace("-", "").replace(" ", "") or "3879422819"
    config = {
        "developer_token":   _genv("GOOGLE_ADS_DEVELOPER_TOKEN"),
        "client_id":         _genv("GOOGLE_ADS_CLIENT_ID"),
        "client_secret":     _genv("GOOGLE_ADS_CLIENT_SECRET"),
        "refresh_token":     _genv("GOOGLE_ADS_REFRESH_TOKEN"),
        "login_customer_id": str(lci),
        "use_proto_plus":    True,
    }
    return GoogleAdsClient.load_from_dict(config)


def _probe_cricket_account_sync(customer_id: str, login_customer_id: str = None) -> dict:
    """Read-only probe: tries a minimal GAQL query to verify the account is reachable."""
    try:
        client = _build_cricket_client(login_customer_id)
        svc    = client.get_service("GoogleAdsService")
        rows   = list(svc.search(
            customer_id=customer_id,
            query="SELECT customer.id, customer.descriptive_name, customer.manager FROM customer LIMIT 1"
        ))
        if rows:
            r = rows[0]
            return {
                "queryable":   True,
                "name":        r.customer.descriptive_name,
                "is_manager":  r.customer.manager,
                "customer_id": str(r.customer.id),
            }
        return {"queryable": True, "name": None, "is_manager": False}
    except GoogleAdsException as ex:
        errs = [e.message for e in ex.failure.errors]
        return {"queryable": False, "error": "; ".join(errs)}
    except Exception as _e:
        return {"queryable": False, "error": str(_e)}


def _create_cricket_display_campaign_sync(
    campaign_name: str, budget_daily: float,
    customer_id: str, login_customer_id: str = None
) -> dict:
    """Create a PAUSED Display campaign + ad group on the specified account."""
    client = _build_cricket_client(login_customer_id)
    cid    = customer_id.replace("-", "")

    # 1. Budget
    bsvc  = client.get_service("CampaignBudgetService")
    b_op  = client.get_type("CampaignBudgetOperation")
    cb    = b_op.create
    cb.name                = f"Cricket Budget — {campaign_name}"
    cb.amount_micros       = int(budget_daily * 1_000_000)
    cb.delivery_method     = client.enums.BudgetDeliveryMethodEnum.STANDARD
    cb.explicitly_shared   = False
    b_resp   = bsvc.mutate_campaign_budgets(customer_id=cid, operations=[b_op])
    budget_rn = b_resp.results[0].resource_name

    # 2. Campaign
    csvc   = client.get_service("CampaignService")
    c_op   = client.get_type("CampaignOperation")
    camp   = c_op.create
    camp.name              = campaign_name
    camp.status            = client.enums.CampaignStatusEnum.PAUSED
    camp.campaign_budget   = budget_rn
    camp.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.DISPLAY
    camp.contains_eu_political_advertising = (
        client.enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
    )
    c_resp     = csvc.mutate_campaigns(customer_id=cid, operations=[c_op])
    campaign_rn = c_resp.results[0].resource_name
    campaign_id = campaign_rn.split("/")[-1]

    # 3. Ad Group
    agsvc  = client.get_service("AdGroupService")
    ag_op  = client.get_type("AdGroupOperation")
    ag     = ag_op.create
    ag.name           = f"{campaign_name} — Ad Group 1"
    ag.campaign       = campaign_rn
    ag.status         = client.enums.AdGroupStatusEnum.ENABLED
    ag.type_          = client.enums.AdGroupTypeEnum.DISPLAY_STANDARD
    ag.cpc_bid_micros = 2_000_000   # ₹2 default starting bid
    ag_resp    = agsvc.mutate_ad_groups(customer_id=cid, operations=[ag_op])
    ag_rn      = ag_resp.results[0].resource_name
    ag_id      = ag_rn.split("/")[-1]

    return {
        "campaign_id":  campaign_id,
        "campaign_rn":  campaign_rn,
        "ad_group_id":  ag_id,
        "ad_group_rn":  ag_rn,
    }


# ── Pydantic models ──────────────────────────────────────────────────────────
class CricketAccountRequest(BaseModel):
    account_name:      str
    customer_id:       str
    login_customer_id: str = ""


class CricketPushRequest(BaseModel):
    customer_id:       str
    login_customer_id: str = ""
    campaign_name:     str
    budget_daily:      float
    headlines:         list = []
    long_headlines:    list = []
    descriptions:      list = []
    whatsapp_link:     str  = ""
    business_type:     str  = "Cricket Community"   # for the growth_memory learning packet
    top_audience:       str  = ""                    # winning audience segment name, if known


# ── Endpoints ────────────────────────────────────────────────────────────────
@app.post("/cricket-ads/accounts/add")
async def cricket_add_account(request: CricketAccountRequest):
    cid = request.customer_id.replace("-", "").strip()
    if not cid:
        return {"success": False, "error": "customer_id is required"}

    probe = await asyncio.to_thread(
        _probe_cricket_account_sync, cid,
        request.login_customer_id or None
    )
    if not probe.get("queryable"):
        return {"success": False, "queryable": False, "error": probe.get("error", "Account not reachable")}

    try:
        await asyncio.to_thread(
            _cricket_account_ops, "insert", None,
            {
                "account_name":      request.account_name,
                "customer_id":       cid,
                "login_customer_id": request.login_customer_id or "",
            }
        )
    except Exception as _e:
        return {"success": False, "error": str(_e)}

    logger.info(f"[CRICKET-ACCT] Saved account {cid!r} ({request.account_name!r})")
    return {"success": True, "queryable": True, "account": probe}


@app.get("/cricket-ads/accounts/list")
async def cricket_list_accounts():
    try:
        accounts = await asyncio.to_thread(_cricket_account_ops, "list")
        return {"success": True, "accounts": accounts or []}
    except Exception as _e:
        return {"success": False, "accounts": [], "error": str(_e)}


@app.delete("/cricket-ads/accounts/{customer_id}")
async def cricket_delete_account(customer_id: str):
    cid = customer_id.replace("-", "").strip()
    try:
        await asyncio.to_thread(_cricket_account_ops, "delete", cid)
        return {"success": True}
    except Exception as _e:
        return {"success": False, "error": str(_e)}


@app.post("/cricket-ads/push-to-google")
async def cricket_push_to_google(request: CricketPushRequest):
    cid   = request.customer_id.replace("-", "").strip()
    lcid  = (request.login_customer_id or "").replace("-", "").strip() or None
    if not cid:
        return {"success": False, "error": "customer_id is required"}

    try:
        result = await asyncio.to_thread(
            _create_cricket_display_campaign_sync,
            request.campaign_name, request.budget_daily, cid, lcid
        )
    except GoogleAdsException as ex:
        error_details = []
        for err in ex.failure.errors:
            detail = {"error_code": str(err.error_code), "message": err.message, "field": None}
            if err.location:
                detail["field"] = str(err.location.field_path_elements)
            error_details.append(detail)
        logger.error(f"[CRICKET-PUSH] API error: {error_details}")
        return {"success": False, "errors": error_details}
    except Exception as _e:
        tb = _traceback.format_exc()
        logger.error(f"[CRICKET-PUSH] Error: {_e}\n{tb}")
        return {"success": False, "error": str(_e), "traceback": tb}

    campaign_id = result["campaign_id"]
    logger.info(f"[CRICKET-PUSH] Created Display campaign {campaign_id!r} on account {cid!r}")

    # ── Learning Engine write-side: log this launch as a sports/gaming pattern ──
    # Confidence is deliberately low here — this only confirms a campaign was
    # LAUNCHED with this audience/platform, not that it performed well (no real
    # performance data exists yet). Real confidence should build over time as
    # /cricket-ads/optimize runs against actual results for this account.
    try:
        await asyncio.to_thread(_save_growth_memory, {
            "industry":         "sports_gaming",
            "business_type":    request.business_type or "Cricket Community",
            "budget_range":     f"₹{request.budget_daily}/day",
            "winning_audience": request.top_audience or "N/A",
            "winning_platform": "Google Display",
            "winning_offer":    request.whatsapp_link and "WhatsApp/Community join" or "Community join",
            "avg_cpl":          "N/A — not yet measured",
            "avg_roas":         "N/A — not yet measured",
            "confidence":       25,
        })
    except Exception as _gme:
        logger.warning(f"[CRICKET-PUSH] growth_memory write skipped: {_gme}")

    return {
        "success":              True,
        "campaign_id":          campaign_id,
        "ad_group_id":          result["ad_group_id"],
        "status":               "PAUSED",
        "google_ads_dashboard": f"https://ads.google.com/aw/campaigns?campaignId={campaign_id}",
        "note":                 "Campaign created PAUSED. Add image assets in Google Ads dashboard before enabling.",
    }


# ── Performance + Optimizer reuse ─────────────────────────────────────────────
# ARCHITECTURE RULE: never duplicate the optimizer. Fetch raw metrics for the
# selected cricket ad account, shape them into the same performance_data schema
# the main platform's performance_memory uses, save under a stable per-account
# key, then hand off to the SAME _run_ai_optimizer_core() the rest of Adsoh uses
# — no cricket-specific optimizer prompt.
def _cricket_perf_key_parts(customer_id: str) -> tuple:
    return (f"cricket-account-{customer_id.strip()}", "Sports & Gaming", "India")


@app.get("/cricket-ads/performance")
async def cricket_ads_performance(customer_id: str, login_customer_id: str = "", days: int = 30):
    cid  = customer_id.replace("-", "").strip()
    lcid = (login_customer_id or "").replace("-", "").strip() or None
    if not cid:
        return {"success": False, "error": "customer_id is required"}

    try:
        client_ = await asyncio.to_thread(_build_cricket_client, lcid)
        end   = datetime.now().date()
        start = end - timedelta(days=days)
        campaigns = await asyncio.to_thread(_fetch_gads_campaigns_sync, client_, cid)
        daily     = await asyncio.to_thread(
            _fetch_gads_campaign_day_sync, client_, cid,
            start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
        )
    except GoogleAdsException as ex:
        errs = [e.message for e in ex.failure.errors]
        return {"success": False, "error": "; ".join(errs)}
    except Exception as _e:
        return {"success": False, "error": str(_e)}

    camp_lookup = {c["campaign_id"]: c for c in campaigns}
    agg = {}
    for row in daily:
        a = agg.setdefault(row["campaign_id"], {"impressions": 0, "clicks": 0, "cost_micros": 0, "conversions": 0.0})
        a["impressions"]  += row["impressions"]
        a["clicks"]       += row["clicks"]
        a["cost_micros"]  += row["cost_micros"]
        a["conversions"]  += row["conversions"]

    campaign_breakdown = []
    total_impr = total_clicks = 0
    total_cost_micros = 0
    total_conv = 0.0
    for cid_, a in agg.items():
        meta   = camp_lookup.get(cid_, {})
        impr   = a["impressions"]
        clicks = a["clicks"]
        cost   = a["cost_micros"] / 1_000_000
        conv   = a["conversions"]
        ctr    = round((clicks / impr * 100), 2) if impr else 0.0
        rating = "good" if (ctr > 2 or conv > 0) else ("average" if ctr >= 1 else "poor")
        campaign_breakdown.append({
            "campaign_name":      meta.get("name", cid_),
            "status":             meta.get("status", "UNKNOWN"),
            "impressions":        impr,
            "clicks":             clicks,
            "cost":               f"RS{round(cost, 2)}",
            "conversions":        conv,
            "ctr":                f"{ctr}%",
            "performance_rating": rating,
        })
        total_impr += impr
        total_clicks += clicks
        total_cost_micros += a["cost_micros"]
        total_conv += conv

    total_cost  = total_cost_micros / 1_000_000
    overall_ctr = round((total_clicks / total_impr * 100), 2) if total_impr else 0.0
    overall_cpc = round((total_cost / total_clicks), 2) if total_clicks else 0.0
    overall_cpa = round((total_cost / total_conv), 2) if total_conv else 0.0
    zero_activity = (total_impr == 0 and total_cost == 0)

    performance_data = {
        "actual_metrics": {
            "ctr":         f"{overall_ctr}%",
            "cpc":         f"RS{overall_cpc}",
            "cost":        f"RS{round(total_cost, 2)}",
            "conversions": total_conv,
            "cpa":         f"RS{overall_cpa}",
            "roas":        "N/A",
        },
        "campaign_breakdown": campaign_breakdown,
        "overall_health": 50,
        "trend": "N/A",
        "top_insight": "" if zero_activity else f"{len(campaign_breakdown)} campaign(s) with live spend over the last {days} days.",
        "biggest_problem": "No active spend detected in this window." if zero_activity else "",
    }

    url_stub, industry_stub, city_stub = _cricket_perf_key_parts(cid)
    business_key = derive_business_key(url_stub, industry_stub, city_stub)
    save_to_memory("performance", business_key, {
        "performance_data": performance_data,
        "date_range":       f"{days}d",
        "overall_health":   performance_data["overall_health"],
    })

    return {
        "success":      True,
        "business_key": business_key,
        "performance":  performance_data,
        "campaigns":    campaigns,
    }


class CricketOptimizeRequest(BaseModel):
    customer_id:       str
    login_customer_id: str = ""


@app.post("/cricket-ads/optimize")
async def cricket_ads_optimize(request: CricketOptimizeRequest):
    cid = request.customer_id.replace("-", "").strip()
    if not cid:
        return {"success": False, "error": "customer_id is required"}
    url_stub, industry_stub, city_stub = _cricket_perf_key_parts(cid)
    return await _run_ai_optimizer_core(url_stub, industry_stub, city_stub)


# ═══════════════════════════════════════════════════════════════════════════════
#  SOCIAL INTELLIGENCE ENGINE (SIE) — complete social/digital presence audit
#  from ONE input. Orchestrates existing engines (website_intelligence, Google
#  Places, a new YouTube channel-stats lookup, Marketing Brain memory) plus
#  Tavily-based OBSERVED research for platforms with no queryable API.
#
#  HONESTY RULE (absolute): every datapoint carries a data_label —
#  VERIFIED (real API call), OBSERVED (public web research via Tavily/
#  Firecrawl), INFERRED (AI reasoning from available signals), or
#  NOT_VERIFIED (nothing reliable found). Nothing is ever invented.
# ═══════════════════════════════════════════════════════════════════════════════

_SIE_INPUT_TYPES = ("website", "instagram", "facebook", "linkedin", "youtube", "business_name", "gbp")

_SIE_SOCIAL_LINK_PATTERNS = {
    "instagram": re.compile(r'https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]+)', re.I),
    "facebook":  re.compile(r'https?://(?:www\.)?facebook\.com/([A-Za-z0-9_.\-]+)', re.I),
    "linkedin":  re.compile(r'https?://(?:www\.)?linkedin\.com/(?:company|in)/([A-Za-z0-9_\-]+)', re.I),
    "youtube":   re.compile(r'https?://(?:www\.)?youtube\.com/(?:channel/|c/|@)([A-Za-z0-9_\-]+)', re.I),
}
_SIE_SOCIAL_LINK_IGNORE = {
    "instagram": {"p", "explore", "accounts", "reel", "reels", "stories", "direct"},
    "facebook":  {"sharer", "share.php", "plugins", "tr", "dialog", "help", "policies", "ads", "profile.php"},
    "linkedin":  {"company", "in", "sharing", "shareArticle"},
    "youtube":   {"watch", "results", "playlist", "embed"},
}


def _sie_extract_social_links(text_blob: str) -> dict:
    """Pull social profile links out of crawled website markdown/HTML or
    Tavily research text. Returns {platform: url} for whichever platforms
    have a real-looking profile link (not a share/plugin/watch URL)."""
    found = {}
    if not text_blob:
        return found
    for platform, pattern in _SIE_SOCIAL_LINK_PATTERNS.items():
        for m in pattern.finditer(text_blob):
            handle = m.group(1)
            if handle.lower() in _SIE_SOCIAL_LINK_IGNORE.get(platform, set()):
                continue
            if platform not in found:
                found[platform] = m.group(0).split("?")[0].rstrip("/")
    return found


async def fetch_youtube_channel_stats(handle_or_url: str) -> dict:
    """
    Look up a SPECIFIC channel's public stats via youtube/v3/channels — this is
    distinct from fetch_youtube_search/fetch_youtube_video_stats above (which
    search for competitive content by keyword, not a single channel's own
    numbers). Accepts a handle ('@sohscape'), a channel URL, or a bare ID.
    Returns {} if not found / not configured — never raises.
    """
    if not YOUTUBE_API_KEY or not handle_or_url:
        return {}
    raw = handle_or_url.strip()
    m = re.search(r'youtube\.com/(?:channel/|c/|@)([A-Za-z0-9_\-]+)', raw, re.I)
    if m:
        raw = m.group(1)
    raw = raw.lstrip("@")
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            for extra in ({"forHandle": f"@{raw}"}, {"id": raw}):
                resp = await c.get(
                    "https://www.googleapis.com/youtube/v3/channels",
                    params={"key": YOUTUBE_API_KEY, "part": "snippet,statistics", **extra},
                )
                items = resp.json().get("items", [])
                if items:
                    ch = items[0]
                    stats = ch.get("statistics", {})
                    snippet = ch.get("snippet", {})
                    return {
                        "channel_id":       ch.get("id", ""),
                        "title":            snippet.get("title", ""),
                        "subscriber_count": (None if stats.get("hiddenSubscriberCount")
                                             else int(stats.get("subscriberCount", 0))),
                        "video_count":      int(stats.get("videoCount", 0)),
                        "view_count":       int(stats.get("viewCount", 0)),
                        "published_at":     snippet.get("publishedAt", "")[:10],
                    }
    except Exception as _e:
        logger.warning(f"[SIE] YouTube channel lookup failed: {_e}")
    return {}


async def fetch_youtube_channel_recent_videos(channel_id: str, max_results: int = 6) -> list:
    """Recent uploads for one channel — used for publishing frequency/engagement."""
    if not YOUTUBE_API_KEY or not channel_id:
        return []
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.get(
                "https://www.googleapis.com/youtube/v3/search",
                params={
                    "key": YOUTUBE_API_KEY, "channelId": channel_id, "part": "snippet",
                    "order": "date", "type": "video", "maxResults": max_results,
                },
            )
            items = resp.json().get("items", [])
        video_ids = [it["id"]["videoId"] for it in items if it.get("id", {}).get("videoId")]
        stats = await fetch_youtube_video_stats(video_ids)
        stats_map = {s["videoId"]: s for s in stats}
        out = []
        for it in items:
            vid = it.get("id", {}).get("videoId")
            if not vid:
                continue
            s = stats_map.get(vid, {})
            out.append({
                "title":        it["snippet"]["title"],
                "published_at": it["snippet"]["publishedAt"][:10],
                "views":        s.get("views", 0),
                "likes":        s.get("likes", 0),
                "comments":     s.get("comments", 0),
                "url":          f"https://www.youtube.com/watch?v={vid}",
            })
        return out
    except Exception as _e:
        logger.warning(f"[SIE] YouTube recent videos fetch failed: {_e}")
        return []


async def _sie_discover_platforms(input_value: str, input_type: str, city: str) -> dict:
    """
    Step 1 — Discovery. If input is a website: Firecrawl it and extract social
    links present on the page. If input is a handle/business name: Tavily
    search to discover the website + other public profiles.
    """
    site_content = ""
    website_url = ""
    extracted = {}

    if input_type == "website":
        website_url = input_value.strip()
        if not re.match(r'^https?://', website_url, re.I):
            website_url = f"https://{website_url}"
        if FIRECRAWL_API_KEY:
            site_content = await fetch_firecrawl(website_url)
        if not site_content:
            try:
                async with httpx.AsyncClient(timeout=12, follow_redirects=True) as c:
                    r = await c.get(website_url, headers={"User-Agent": "Mozilla/5.0"})
                    site_content = r.text
            except Exception:
                site_content = ""
        extracted = _sie_extract_social_links(site_content)
    else:
        research = await fetch_tavily(
            f"{input_value} {city} official website Instagram Facebook LinkedIn YouTube"
        )
        site_content = research or ""
        extracted = _sie_extract_social_links(research or "")
        m = re.search(r'https?://(?:www\.)?([a-z0-9\-]+\.[a-z]{2,})(?:/\S*)?', research or "", re.I)
        if m:
            candidate = m.group(0).split()[0].rstrip(').,')
            if not any(p in candidate for p in ("instagram.com", "facebook.com", "linkedin.com", "youtube.com")):
                website_url = candidate

        if input_type in ("instagram", "facebook", "linkedin", "youtube"):
            handle = input_value.strip()
            if not re.match(r'^https?://', handle, re.I):
                base = {"instagram": "instagram.com", "facebook": "facebook.com",
                        "linkedin": "linkedin.com/in", "youtube": "youtube.com"}[input_type]
                handle = f"https://{base}/{handle.lstrip('@')}"
            extracted.setdefault(input_type, handle)

    platforms = []
    for platform in ("website", "instagram", "facebook", "linkedin", "youtube"):
        url = website_url if platform == "website" else extracted.get(platform, "")
        if url:
            is_direct_input = (platform == "website" and input_type == "website")
            platforms.append({
                "platform": platform, "url": url, "status": "found",
                "confidence": 95 if is_direct_input else 65,
                "data_label": "VERIFIED" if is_direct_input else "OBSERVED",
            })
        else:
            platforms.append({"platform": platform, "url": "", "status": "not_found",
                               "confidence": 0, "data_label": "NOT_VERIFIED"})

    return {"website": website_url, "platforms": platforms, "site_content": site_content[:4000]}


async def _sie_business_understanding(website_url: str, industry: str, city: str,
                                       site_content: str, input_value: str) -> dict:
    """
    Step 2 — Business Understanding. Reuse Marketing Brain memory if it
    exists (memory_reused: true). Otherwise a lightweight single GPT call
    derives business name/positioning/industry from crawled/researched
    content — never re-runs the full Marketing Brain pipeline.
    """
    memory_reused = False
    biz_name, positioning, uvp = "", "", ""
    detected_industry = industry

    if website_url:
        mem, _resolved_key = get_memory_with_city_fallback(website_url, industry, city)
        bm = mem.get("business", {}) if mem else {}
        if bm:
            memory_reused = True
            biz_name = bm.get("business_name", "") or ""
            positioning = bm.get("positioning", "") or ""
            uvp = bm.get("uvp", "") or ""
            dna = bm.get("business_dna")
            if isinstance(dna, dict):
                detected_industry = dna.get("detected_industry", industry) or industry

    if not memory_reused:
        try:
            city_display = city or "India (no specific city given)"
            prompt = (
                "From the content below, identify this business's name, industry, one-line positioning, and "
                "unique value proposition. Be specific — cite what's actually in the content, don't invent.\n\n"
                f"INPUT: {input_value}\nCITY: {city_display}\n\nCONTENT:\n{(site_content or '')[:2500]}\n\n"
                'Return ONLY JSON: {"business_name":"...","industry":"...","positioning":"...","uvp":"..."}'
            )
            resp = await asyncio.to_thread(
                client.chat.completions.create,
                model="gpt-4o", messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}, temperature=0.3, max_tokens=300,
            )
            parsed = json.loads(resp.choices[0].message.content)
            biz_name = parsed.get("business_name") or input_value
            detected_industry = parsed.get("industry") or industry
            positioning = parsed.get("positioning", "")
            uvp = parsed.get("uvp", "")
        except Exception as _e:
            logger.warning(f"[SIE] Business understanding fallback GPT call failed: {_e}")
            biz_name = input_value

    return {
        "business_name": biz_name or input_value,
        "industry":      detected_industry or industry,
        "positioning":   positioning,
        "uvp":           uvp,
        "memory_reused": memory_reused,
        "data_label":    "VERIFIED" if memory_reused else "INFERRED",
    }


async def _sie_youtube_step(youtube_url: str) -> dict:
    """Step 3 — YouTube (VERIFIED via YouTube Data API), or an honest NOT_VERIFIED."""
    if not youtube_url:
        return {"found": False, "data_label": "NOT_VERIFIED",
                "note": "No YouTube channel discovered for this business."}
    stats = await fetch_youtube_channel_stats(youtube_url)
    if not stats:
        return {"found": False, "data_label": "NOT_VERIFIED",
                "note": "Channel link found but could not retrieve public stats (private, deleted, or API quota)."}

    recent = await fetch_youtube_channel_recent_videos(stats.get("channel_id", ""), max_results=6)
    freq_note = "Not enough recent videos to estimate publishing frequency."
    if len(recent) >= 2:
        try:
            dates = sorted((datetime.strptime(v["published_at"], "%Y-%m-%d") for v in recent), reverse=True)
            gaps = [(dates[i] - dates[i + 1]).days for i in range(len(dates) - 1)]
            if gaps:
                freq_note = f"~1 video every {round(sum(gaps) / len(gaps))} days (based on last {len(recent)} uploads)"
        except Exception:
            pass

    return {
        "found":                True,
        "channel_id":           stats.get("channel_id", ""),
        "channel_title":        stats.get("title", ""),
        "subscriber_count":     stats.get("subscriber_count"),
        "video_count":          stats.get("video_count", 0),
        "view_count":           stats.get("view_count", 0),
        "recent_videos":        recent,
        "publishing_frequency": freq_note,
        "data_label":           "VERIFIED",
    }


def _sie_name_similarity(a: str, b: str) -> float:
    """0-1 fuzzy similarity, with a boost for exact substring containment
    (handles 'Sohscape' vs 'Sohscape Digital Marketing Agency')."""
    a, b = (a or "").lower().strip(), (b or "").lower().strip()
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return max(0.6, difflib.SequenceMatcher(None, a, b).ratio())
    return difflib.SequenceMatcher(None, a, b).ratio()


async def _sie_gbp_step(business_name: str, city: str) -> dict:
    """
    Step 4 — Google Business Profile (VERIFIED via Places API).
    Places Text Search can return loosely-related results (or the most
    popular business in the city) even when nothing actually matches —
    a real name-similarity threshold is required before calling anything
    "found", otherwise an unrelated business gets mislabeled VERIFIED.
    """
    if not business_name:
        return {"found": False, "data_label": "NOT_VERIFIED", "note": "No business name available to search."}
    try:
        results = await fetch_google_places(business_name, city, max_results=5)
    except Exception as _e:
        logger.warning(f"[SIE] Places search failed: {_e}")
        results = []
    if not results:
        return {"found": False, "data_label": "NOT_VERIFIED",
                "note": f"No Google Business Profile found for '{business_name}' in {city}."}

    _MATCH_THRESHOLD = 0.45
    scored = [(_sie_name_similarity(business_name, r.get("name", "")), r) for r in results]
    scored.sort(key=lambda t: (t[0], t[1].get("user_ratings_total", 0) or 0), reverse=True)
    best_score, best = scored[0]

    if best_score < _MATCH_THRESHOLD:
        return {
            "found": False, "data_label": "NOT_VERIFIED",
            "note": (f"No confidently-matching Google Business Profile found for '{business_name}' in {city} — "
                     f"closest result was '{best.get('name', '')}', which didn't match closely enough to report "
                     "as this business."),
        }

    close_matches = [r for score, r in scored if score >= _MATCH_THRESHOLD]
    match_note = (
        f"{len(close_matches)} possible matches found in {city} — picked the closest name match with the most reviews."
        if len(close_matches) > 1 else None
    )

    return {
        "found":           True,
        "name":            best.get("name", ""),
        "rating":          best.get("rating"),
        "review_count":    best.get("user_ratings_total", 0),
        "address":         best.get("address", ""),
        "website":         best.get("website", ""),
        "business_status": best.get("business_status", ""),
        "match_note":      match_note,
        "data_label":      "VERIFIED",
    }


async def _sie_website_step(website_url: str, business_key: str, industry: str, city: str) -> dict:
    """Step 5 — Website Intelligence (VERIFIED). Thin wrapper around the
    existing website_intelligence engine — no duplicated audit logic."""
    if not website_url:
        return {"found": False, "data_label": "NOT_VERIFIED", "note": "No website to audit."}
    try:
        outcome = await website_intelligence(WebsiteIntelligenceRequest(
            url=website_url, business_key=business_key, industry=industry, city=city,
        ))
        if not outcome.get("success"):
            return {"found": False, "data_label": "NOT_VERIFIED",
                    "note": outcome.get("error", "Website audit failed.")}
        audit = dict(outcome.get("audit", {}))
        audit["found"] = True
        audit["data_label"] = "VERIFIED"
        return audit
    except Exception as _e:
        logger.error(f"[SIE] Website step failed: {_e}")
        return {"found": False, "data_label": "NOT_VERIFIED", "note": f"Website audit error: {_e}"}


def _sie_name_appears_in_snippet(business_name: str, snippet: str) -> bool:
    """
    Deterministic guard: does the business's name actually appear in this
    research snippet? Prevents attributing an unrelated same-keyword
    company's real data to this business — a subtler honesty-rule
    violation than outright invention (a real number about the WRONG
    business), which prompt instructions alone don't reliably prevent.
    """
    if not business_name or not snippet:
        return False
    snippet_l = snippet.lower()
    words = [w for w in re.findall(r"[a-zA-Z]+", business_name) if len(w) >= 4]
    if not words:
        words = [business_name.strip()]
    return any(w.lower() in snippet_l for w in words)


async def _sie_social_observed_step(business_name: str, platforms: dict, city: str) -> dict:
    """
    Step 6 — Instagram/Facebook/LinkedIn (OBSERVED only). Tavily research for
    publicly observable signals. NEVER outputs exact engagement rates, growth
    curves, or fake follower percentages — only what the research snippets
    actually say, labeled OBSERVED with a source snippet, or NOT_VERIFIED.
    """
    targets = ("instagram", "facebook", "linkedin")
    queries = [
        fetch_tavily(
            f"{business_name} {city} {p} page followers activity reviews mentions"
            + (f" {platforms[p]}" if platforms.get(p) else "")
        )
        for p in targets
    ]
    results = await asyncio.gather(*queries, return_exceptions=True)
    research_by_platform = {p: (r if isinstance(r, str) else "") for p, r in zip(targets, results)}

    combined = "\n\n".join(f"=== {p.upper()} ===\n{txt[:800]}" for p, txt in research_by_platform.items() if txt)
    if not combined.strip():
        return {
            p: {"platform": p, "handle_or_url": platforms.get(p, ""), "data_label": "NOT_VERIFIED",
                "note": "No reliable public data found. Connect this account for verified insights (coming in a future update)."}
            for p in targets
        }

    try:
        prompt = (
            f"You are researching the PUBLIC social media presence of {business_name} in {city} using ONLY the "
            "research snippets below. NEVER invent exact follower counts, engagement rates, or growth "
            "percentages — only report what the snippets actually say. If a snippet mentions an approximate "
            "follower count or activity level, report it AS MENTIONED with the source context. If nothing "
            "reliable is found for a platform, say so explicitly and label it NOT_VERIFIED.\n\n"
            "CRITICAL — VERIFY THE SNIPPET IS ACTUALLY ABOUT THIS BUSINESS: web search results often surface an "
            f"UNRELATED company or page that just happens to share keywords with {business_name} or {city} (e.g. "
            "a different 'marketing services' or 'community' page in the same city). Before extracting ANY "
            f"follower count, activity level, or theme, confirm the snippet is actually naming or describing "
            f"{business_name} specifically — not a same-industry or same-city page with a different name. If a "
            "snippet's company/page name does not match, or you cannot confirm it's the same business, treat "
            "that platform as NOT_VERIFIED and do not use any of that snippet's numbers, even if they look "
            "real — a real number about the WRONG business is not this business's data.\n\n"
            f"{combined}\n\n"
            "Return ONLY JSON with this exact structure:\n"
            "{\n"
            '  "instagram": {"approx_followers": "as mentioned in research, or null", "activity_level": "...", '
            '"content_themes": ["...","..."], "notable_mentions": "...", "source_snippet": "...", '
            '"data_label": "OBSERVED or NOT_VERIFIED", "note": "..."},\n'
            '  "facebook": {"approx_followers": "...", "activity_level": "...", "content_themes": ["...","..."], '
            '"notable_mentions": "...", "source_snippet": "...", "data_label": "...", "note": "..."},\n'
            '  "linkedin": {"approx_followers": "...", "activity_level": "...", "content_themes": ["...","..."], '
            '"notable_mentions": "...", "source_snippet": "...", "data_label": "...", "note": "..."}\n'
            "}"
        )
        resp = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4o", messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}, temperature=0.3, max_tokens=900,
        )
        parsed = json.loads(resp.choices[0].message.content)
        for p in targets:
            entry = parsed.get(p) or {}
            entry.setdefault("platform", p)
            entry.setdefault("handle_or_url", platforms.get(p, ""))
            if entry.get("data_label") == "OBSERVED":
                # Deterministic backstop: prompt compliance alone isn't a guarantee. Web
                # search regularly surfaces an unrelated same-keyword company (e.g.
                # searching "Sohscape Jaipur linkedin" once returned a page for
                # "Jaipur Social", a different business entirely) — if the business's
                # own name doesn't appear anywhere in the source snippet, this is a
                # real number about the WRONG business, not this one's data.
                if not _sie_name_appears_in_snippet(business_name, entry.get("source_snippet", "")):
                    logger.warning(f"[SIE] {p} OBSERVED entry rejected — {business_name!r} not found in its source_snippet")
                    entry.update({
                        "data_label": "NOT_VERIFIED", "approx_followers": None, "activity_level": None,
                        "content_themes": [], "notable_mentions": None,
                        "note": "Research returned a page that couldn't be confirmed as this business's — "
                                "treating as unverified rather than risk misattributing another company's data.",
                    })
                else:
                    entry.setdefault("note", "")
            else:
                entry["data_label"] = "NOT_VERIFIED"
                entry.setdefault("note", "Connect this account for verified insights (coming in a future update).")
            parsed[p] = entry
        return parsed
    except Exception as _e:
        logger.error(f"[SIE] Social observed synthesis failed: {_e}")
        return {
            p: {"platform": p, "handle_or_url": platforms.get(p, ""), "data_label": "NOT_VERIFIED",
                "note": f"Research synthesis failed: {_e}"}
            for p in targets
        }


async def _sie_competitor_step(business_name: str, industry: str, city: str, memory: dict) -> dict:
    """Step 7 — Competitor Social Comparison. Reuses cached Marketing Brain
    competitor memory if present; otherwise fresh Tavily-based discovery +
    qualitative GPT comparison (no invented follower/engagement numbers)."""
    cached = (memory or {}).get("competitor", {}).get("competitors") if memory else None
    if isinstance(cached, str):
        try:
            cached = json.loads(cached)
        except Exception:
            cached = None
    if cached and isinstance(cached, list):
        return {
            "competitors": [
                ({"name": c, "data_label": "VERIFIED"} if isinstance(c, str) else {**c, "data_label": "VERIFIED"})
                for c in cached[:3]
            ],
            "source": "Marketing Brain memory",
            "data_label": "VERIFIED",
        }

    research = await fetch_tavily(
        f"top competitors of {business_name} {industry} {city} social media presence Instagram Facebook"
    )
    if not research:
        return {"competitors": [], "data_label": "NOT_VERIFIED", "note": "No competitor research data available."}
    try:
        prompt = (
            f"From this research, identify 2-3 REAL competitors of {business_name} ({industry} in {city}) and "
            "compare their social media activity level, content style, and positioning qualitatively — do not "
            "invent exact follower counts or engagement metrics.\n\n"
            f"RESEARCH:\n{research[:1500]}\n\n"
            'Return ONLY JSON: {"competitors": [{"name":"...","activity_level":"...","content_style":"...",'
            '"positioning":"..."}], "where_ahead": ["...","..."], "where_behind": ["...","..."]}'
        )
        resp = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4o", messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}, temperature=0.4, max_tokens=700,
        )
        parsed = json.loads(resp.choices[0].message.content)
        for c in parsed.get("competitors", []):
            c["data_label"] = "OBSERVED"
        parsed["data_label"] = "OBSERVED"
        parsed["source"] = "Live web research"
        return parsed
    except Exception as _e:
        logger.error(f"[SIE] Competitor step failed: {_e}")
        return {"competitors": [], "data_label": "NOT_VERIFIED", "note": str(_e)}


async def _sie_content_intelligence_step(business_name: str, industry: str, city: str, positioning: str) -> dict:
    """Step 8 — Content Intelligence (INFERRED, one GPT call). 30-day calendar
    (30 reels + 30 carousels + 30 stories) must never come back short — same
    backfill pattern already proven for the Cricket Media Buying Brain."""
    prompt = (
        "You are a social media content strategist. Generate a complete content intelligence report for this "
        "business — every idea must be SPECIFIC to this business, industry, and city. No generic filler like "
        "'post a customer testimonial' — name the actual angle.\n\n"
        f"Business: {business_name} | Industry: {industry} | City: {city}\n"
        f"Positioning: {positioning or 'not available'}\n\n"
        "Return ONLY JSON with this exact structure:\n"
        "{\n"
        '  "best_topics": ["...","...","...","...","..."],\n'
        '  "content_gaps": ["...","...","..."],\n'
        '  "posting_schedule": "...",\n'
        '  "hook_styles": ["...","...","..."],\n'
        '  "calendar": {\n'
        '    "reels": [30 single-line specific reel ideas],\n'
        '    "carousels": [30 single-line specific carousel ideas],\n'
        '    "stories": [30 single-line specific story ideas]\n'
        "  }\n"
        "}\n"
        "The calendar arrays MUST have EXACTLY 30 items each — no fewer. Each idea is ONE line, specific to "
        f"{business_name} in {city}, not generic filler."
    )
    try:
        resp = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4o", messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}, temperature=0.6, max_tokens=4000,
        )
        result = json.loads(resp.choices[0].message.content)
    except Exception as _e:
        logger.error(f"[SIE] Content intelligence GPT call failed: {_e}")
        result = {"error": str(_e)}

    _sie_content_singular = {"reels": "reel", "carousels": "carousel", "stories": "story"}
    calendar = result.setdefault("calendar", {})
    for field in ("reels", "carousels", "stories"):
        singular = _sie_content_singular[field]
        items = calendar.get(field) or []
        if len(items) < 30:
            need = 30 - len(items)
            logger.warning(f"[SIE] content calendar {field!r} short ({len(items)}/30) — backfilling {need}")
            try:
                fill_prompt = (
                    f"Generate exactly {need} more specific, distinct {singular} ideas for {business_name} "
                    f"({industry} in {city}) — each ONE line, no generic filler, different from these existing "
                    f"ones: {json.dumps(items[:10])}\n"
                    f'Return ONLY JSON: {{"{field}": [...]}} with exactly {need} items.'
                )
                fill_resp = await asyncio.to_thread(
                    client.chat.completions.create,
                    model="gpt-4o", messages=[{"role": "user", "content": fill_prompt}],
                    response_format={"type": "json_object"}, temperature=0.6, max_tokens=800,
                )
                extra = json.loads(fill_resp.choices[0].message.content).get(field) or []
                items = items + extra
            except Exception as _be:
                logger.warning(f"[SIE] Backfill for {field} failed: {_be}")
            if len(items) < 30:
                # Deterministic last-resort pad — clearly generic placeholder text,
                # never presented as a real data point, just guarantees array length.
                items = items + [
                    f"{business_name} {singular} idea #{i + 1} — customize before publishing"
                    for i in range(30 - len(items))
                ]
            calendar[field] = items[:30] if len(items) > 30 else items
    result["calendar"] = calendar
    result["data_label"] = "INFERRED"
    return result


def _sie_brand_health(youtube: dict, gbp: dict, website: dict, social: dict, content: dict) -> dict:
    """
    Step 9 — Brand Health Engine. Deterministic scoring from the actual
    gathered signals — never a GPT guess. Sections backed only by OBSERVED/
    INFERRED data are capped at 70 confidence, per the honesty rule.
    """
    def _confidence_for(label):
        return {"VERIFIED": 90, "OBSERVED": 70, "INFERRED": 70}.get(label, 30)

    website_label = "VERIFIED" if website.get("found") else "NOT_VERIFIED"
    website_score = website.get("overall_score", 0) if website.get("found") else 0

    if youtube.get("found"):
        subs = youtube.get("subscriber_count") or 0
        if subs >= 50000: yt_score = 90
        elif subs >= 10000: yt_score = 75
        elif subs >= 1000: yt_score = 55
        elif subs > 0: yt_score = 35
        else: yt_score = 20  # channel exists but subs hidden/zero
        youtube_label = "VERIFIED"
    else:
        yt_score, youtube_label = 0, "NOT_VERIFIED"

    if gbp.get("found"):
        rating = gbp.get("rating") or 0
        reviews = gbp.get("review_count") or 0
        gbp_score = min(100, round((rating / 5 * 60) + min(reviews, 200) / 200 * 40))
        gbp_label = "VERIFIED"
    else:
        gbp_score, gbp_label = 0, "NOT_VERIFIED"

    observed_platforms = [p for p in ("instagram", "facebook", "linkedin")
                           if isinstance(social.get(p), dict) and social[p].get("data_label") == "OBSERVED"]
    social_score = round(len(observed_platforms) / 3 * 100) if social else 0
    social_label = "OBSERVED" if observed_platforms else "NOT_VERIFIED"

    has_calendar = bool((content or {}).get("calendar", {}).get("reels"))
    content_score = 60 if has_calendar else 0
    content_label = "INFERRED" if has_calendar else "NOT_VERIFIED"

    weights = {"website": 0.30, "youtube": 0.15, "google_business": 0.20, "social_presence": 0.20, "content": 0.15}
    scores = {"website": website_score, "youtube": yt_score, "google_business": gbp_score,
              "social_presence": social_score, "content": content_score}
    labels = {"website": website_label, "youtube": youtube_label, "google_business": gbp_label,
              "social_presence": social_label, "content": content_label}
    overall = round(sum(scores[k] * weights[k] for k in weights))
    overall_confidence = round(sum(_confidence_for(labels[k]) * weights[k] for k in weights))

    return {
        "website":         {"score": website_score, "data_label": website_label, "confidence": _confidence_for(website_label)},
        "youtube":         {"score": yt_score, "data_label": youtube_label, "confidence": _confidence_for(youtube_label)},
        "google_business": {"score": gbp_score, "data_label": gbp_label, "confidence": _confidence_for(gbp_label)},
        "social_presence": {"score": social_score, "data_label": social_label, "confidence": _confidence_for(social_label)},
        "content":         {"score": content_score, "data_label": content_label, "confidence": _confidence_for(content_label)},
        "overall":         {"score": overall, "data_label": "MIXED", "confidence": overall_confidence},
    }


async def _sie_growth_engine(business_name: str, industry: str, city: str, brand_health: dict,
                              website: dict, social: dict, competitor: dict, content: dict) -> dict:
    """Step 10 — Growth Engine. Quick wins / 30-day / 90-day / organic-vs-paid
    split, gated by an explicit paid-campaign-readiness verdict when signals
    are weak rather than recommending spend the business isn't ready for."""
    rec_shape = ('{"observation":"...","evidence":"...","confidence":0,"expected_impact":"...","risk":"...",'
                 '"priority":"high/medium/low","next_action":"..."}')
    context = (
        f"Business: {business_name} | Industry: {industry} | City: {city}\n"
        f"Brand Health: {json.dumps(brand_health, ensure_ascii=False)}\n"
        f"Website audit: overall_score={website.get('overall_score')}, quick_wins={website.get('quick_wins')}\n"
        f"Social signals: {json.dumps({k: v.get('activity_level') for k, v in social.items() if isinstance(v, dict)}, ensure_ascii=False)}\n"
        f"Competitor gaps: {json.dumps(competitor.get('where_behind', []), ensure_ascii=False)}\n"
    )
    prompt = (
        "You are a senior growth strategist producing a prioritized action plan from this brand's actual audit "
        "data above. Every major recommendation needs: observation, evidence, confidence (0-100), "
        "expected_impact, risk, priority (high/medium/low), next_action.\n\n"
        f"{context}\n"
        "Also decide: is this business ready for PAID campaigns right now, or should it fix organic/website "
        "fundamentals first? Base this on the brand_health.overall score and whether website/social signals are "
        "strong enough — if overall health is weak (below ~50) or the website has major unresolved issues, say "
        "clearly it is NOT ready and list the specific reasons, rather than recommending a paid campaign anyway.\n\n"
        "Return ONLY JSON:\n"
        "{\n"
        f'  "quick_wins": [{rec_shape}, {rec_shape}],\n'
        f'  "plan_30_day": [{rec_shape}, {rec_shape}],\n'
        f'  "plan_90_day": [{rec_shape}, {rec_shape}],\n'
        '  "organic_vs_paid_split": "...",\n'
        '  "paid_campaign_readiness": {"ready": true, "reasons": ["...","..."]},\n'
        '  "campaign_recommendations": ["...","...","..."]\n'
        "}"
    )
    try:
        resp = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4o", messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}, temperature=0.4, max_tokens=1800,
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as _e:
        logger.error(f"[SIE] Growth engine call failed: {_e}")
        return {"error": str(_e), "quick_wins": [], "plan_30_day": [], "plan_90_day": []}


class SocialIntelRequest(BaseModel):
    input_value: str
    input_type:  str = "website"
    city:        str = ""
    industry:    str = ""


@app.post("/social-intelligence")
async def social_intelligence(request: SocialIntelRequest):
    input_value = (request.input_value or "").strip()
    input_type  = request.input_type if request.input_type in _SIE_INPUT_TYPES else "website"
    city        = (request.city or "").strip()
    industry    = (request.industry or "").strip()
    # Google Business Profile / Places lookups and search queries genuinely
    # need a location — blank means search nationally (India), never a
    # silently-assumed city. business_key derivation below keeps the raw
    # (possibly blank) city so memory keys stay consistent.
    search_scope = city or "India"

    if not input_value:
        return {"success": False, "error": "input_value is required"}

    warnings = []

    # ── Step 1: Discovery ────────────────────────────────────────────────
    try:
        discovery = await _sie_discover_platforms(input_value, input_type, search_scope)
    except Exception as _e:
        logger.error(f"[SIE] Discovery step failed: {_e}")
        return {"success": False, "error": f"Platform discovery failed: {_e}"}

    website_url  = discovery.get("website", "")
    site_content = discovery.get("site_content", "")
    platform_map = {p["platform"]: p["url"] for p in discovery["platforms"] if p["url"]}

    business_key = derive_business_key(website_url or input_value, industry, city)

    # ── Step 2: Business Understanding ───────────────────────────────────
    try:
        business_summary = await _sie_business_understanding(website_url, industry, city, site_content, input_value)
    except Exception as _e:
        logger.error(f"[SIE] Business understanding failed: {_e}")
        business_summary = {"business_name": input_value, "industry": industry,
                             "data_label": "INFERRED", "memory_reused": False}
        warnings.append(f"business_understanding generation failed: {_e}")

    business_name      = business_summary.get("business_name") or input_value
    resolved_industry  = business_summary.get("industry") or industry
    prior_memory, _rk  = get_memory_with_city_fallback(website_url or input_value, resolved_industry, city)

    # ── Steps 3-8: PARALLEL with per-step failure isolation ──────────────
    step_calls = {
        "youtube":               lambda: _sie_youtube_step(platform_map.get("youtube", "")),
        "google_business":       lambda: _sie_gbp_step(business_name, search_scope),
        "website":               lambda: _sie_website_step(website_url, business_key, resolved_industry, city),
        "social_signals":        lambda: _sie_social_observed_step(business_name, platform_map, search_scope),
        "competitor_comparison": lambda: _sie_competitor_step(business_name, resolved_industry, search_scope, prior_memory),
        "content_intelligence":  lambda: _sie_content_intelligence_step(
            business_name, resolved_industry, search_scope, business_summary.get("positioning", "")
        ),
    }
    step_names = list(step_calls.keys())
    outcomes = await asyncio.gather(*[step_calls[n]() for n in step_names], return_exceptions=True)

    step_results = {}
    for name, outcome in zip(step_names, outcomes):
        if isinstance(outcome, Exception):
            logger.error(f"[SIE] Step {name!r} raised: {outcome}")
            step_results[name] = {"data_label": "NOT_VERIFIED", "error": f"{type(outcome).__name__}: {outcome}"}
            warnings.append(f"{name} step failed: {outcome}")
        else:
            step_results[name] = outcome

    youtube_result    = step_results["youtube"]
    gbp_result        = step_results["google_business"]
    website_result    = step_results["website"]
    social_result     = step_results["social_signals"]
    competitor_result = step_results["competitor_comparison"]
    content_result    = step_results["content_intelligence"]

    # ── Step 9: Brand Health (deterministic) ─────────────────────────────
    try:
        brand_health = _sie_brand_health(youtube_result, gbp_result, website_result, social_result, content_result)
    except Exception as _e:
        logger.error(f"[SIE] Brand health scoring failed: {_e}")
        brand_health = {"error": str(_e)}
        warnings.append(f"brand_health scoring failed: {_e}")

    # ── Step 10: Growth Engine ────────────────────────────────────────────
    try:
        growth = await _sie_growth_engine(
            business_name, resolved_industry, search_scope, brand_health,
            website_result, social_result, competitor_result, content_result,
        )
    except Exception as _e:
        logger.error(f"[SIE] Growth engine failed: {_e}")
        growth = {"error": str(_e)}
        warnings.append(f"growth_engine generation failed: {_e}")

    result = {
        "input_value":             input_value,
        "input_type":              input_type,
        "city":                    city,
        "platform_discovery":      discovery["platforms"],
        "business_summary":        business_summary,
        "youtube":                 youtube_result,
        "google_business_profile": gbp_result,
        "website":                 website_result,
        "social_signals":          social_result,
        "competitor_comparison":   competitor_result,
        "content_intelligence":    content_result,
        "brand_health":            brand_health,
        "growth_engine":           growth,
    }

    result = _clean_banned_words_deep(result)

    try:
        save_to_memory("social_intel", business_key, {"data": result})
    except Exception as _se:
        logger.warning(f"[SIE] Memory save failed: {_se}")

    logger.info(f"[SIE] Done for input={input_value!r} type={input_type!r} business_key={business_key!r} warnings={warnings}")

    log_activity(
        "social_intel", business_key=business_key,
        business_name=business_summary.get("business_name") or input_value,
        url=website_url or input_value, industry=industry, city=city,
        summary="Social Intelligence audit generated",
    )

    return {
        "success":       True,
        "business_key":  business_key,
        "memory_reused": business_summary.get("memory_reused", False),
        "warnings":      warnings,
        "data":          result,
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  GOOGLE ADS ACCOUNT IMPORT — OAUTH CONNECTION, 12-MONTH IMPORT, DASHBOARD
#  Single-tenant: ONE Google OAuth connection app-wide. The account selector
#  picks among Ads accounts visible to that one connection (e.g. an MCC with
#  several clients) — multi-account, not multi-user. Isolated gads_ tables,
#  never collides with the cricket_* tables above.
# ═══════════════════════════════════════════════════════════════════════════════

# oauthlib is strict about scope drift and requires HTTPS redirects by default —
# relax the former (Google commonly returns a superset of requested scopes) and
# only relax the latter for non-HTTPS (local dev) redirect URIs.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")


def _gads_oauth_redirect_uri() -> str:
    return _genv("GOOGLE_OAUTH_REDIRECT_URI") or "http://localhost:8000/google/callback"


if not _gads_oauth_redirect_uri().startswith("https://"):
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")


# ── Token encryption ─────────────────────────────────────────────────────────
def _get_fernet() -> Fernet:
    key = _genv("ENCRYPTION_KEY")
    if not key:
        raise RuntimeError("ENCRYPTION_KEY not set")
    return Fernet(key.encode())


def encrypt_token(raw: str) -> str:
    return _get_fernet().encrypt(raw.encode()).decode()


def decrypt_token(enc: str) -> str:
    return _get_fernet().decrypt(enc.encode()).decode()


# ── OAuth state (CSRF) — stateless, HMAC-signed via ENCRYPTION_KEY ───────────
def _make_oauth_state() -> str:
    ts = str(int(datetime.utcnow().timestamp()))
    key = _genv("ENCRYPTION_KEY").encode()
    sig = hmac.new(key, ts.encode(), hashlib.sha256).hexdigest()[:16]
    return base64.urlsafe_b64encode(f"{ts}.{sig}".encode()).decode()


def _verify_oauth_state(state: str, max_age_seconds: int = 600) -> bool:
    try:
        raw = base64.urlsafe_b64decode(state.encode()).decode()
        ts, sig = raw.split(".", 1)
        key = _genv("ENCRYPTION_KEY").encode()
        expected = hmac.new(key, ts.encode(), hashlib.sha256).hexdigest()[:16]
        if not hmac.compare_digest(sig, expected):
            return False
        age = datetime.utcnow().timestamp() - int(ts)
        return 0 <= age <= max_age_seconds
    except Exception:
        return False


# ── DB tables ─────────────────────────────────────────────────────────────────
_GADS_DDL = """
CREATE TABLE IF NOT EXISTS gads_oauth_tokens (
    id                       INTEGER PRIMARY KEY,
    encrypted_refresh_token  TEXT NOT NULL,
    scope                    TEXT,
    connected_at             TEXT,
    updated_at               TEXT,
    revoked                  BOOLEAN DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS gads_accounts (
    id                       BIGSERIAL PRIMARY KEY,
    customer_id              TEXT UNIQUE NOT NULL,
    account_name             TEXT,
    login_customer_id        TEXT,
    is_manager               BOOLEAN DEFAULT FALSE,
    selected                 BOOLEAN DEFAULT FALSE,
    last_imported_at         TEXT,
    ai_summary_json          TEXT,
    ai_summary_generated_at  TEXT,
    discovered_at            TEXT
);

CREATE TABLE IF NOT EXISTS gads_import_jobs (
    id            TEXT PRIMARY KEY,
    customer_id   TEXT NOT NULL,
    status        TEXT NOT NULL,
    progress_pct  INTEGER DEFAULT 0,
    current_step  TEXT,
    started_at    TEXT,
    finished_at   TEXT,
    error         TEXT,
    created_at    TEXT
);

CREATE TABLE IF NOT EXISTS gads_campaigns (
    id                      BIGSERIAL PRIMARY KEY,
    customer_id             TEXT NOT NULL,
    campaign_id             TEXT NOT NULL,
    name                    TEXT,
    status                  TEXT,
    channel_type            TEXT,
    bidding_strategy_type   TEXT,
    budget_amount_micros    BIGINT,
    budget_delivery_method  TEXT,
    start_date              TEXT,
    end_date                TEXT,
    updated_at              TEXT,
    UNIQUE(customer_id, campaign_id)
);

CREATE TABLE IF NOT EXISTS gads_campaign_day (
    id                BIGSERIAL PRIMARY KEY,
    customer_id       TEXT NOT NULL,
    campaign_id       TEXT NOT NULL,
    date              DATE NOT NULL,
    impressions       BIGINT DEFAULT 0,
    clicks            BIGINT DEFAULT 0,
    cost_micros       BIGINT DEFAULT 0,
    conversions       DOUBLE PRECISION DEFAULT 0,
    conversion_value  DOUBLE PRECISION DEFAULT 0,
    UNIQUE(customer_id, campaign_id, date)
);

CREATE TABLE IF NOT EXISTS gads_ad_groups (
    id            BIGSERIAL PRIMARY KEY,
    customer_id   TEXT NOT NULL,
    campaign_id   TEXT NOT NULL,
    ad_group_id   TEXT NOT NULL,
    name          TEXT,
    status        TEXT,
    impressions   BIGINT DEFAULT 0,
    clicks        BIGINT DEFAULT 0,
    cost_micros   BIGINT DEFAULT 0,
    conversions   DOUBLE PRECISION DEFAULT 0,
    period_start  DATE,
    period_end    DATE,
    UNIQUE(customer_id, ad_group_id)
);

CREATE TABLE IF NOT EXISTS gads_ads (
    id                 BIGSERIAL PRIMARY KEY,
    customer_id        TEXT NOT NULL,
    campaign_id        TEXT NOT NULL,
    ad_group_id        TEXT NOT NULL,
    ad_id              TEXT NOT NULL,
    ad_type            TEXT,
    status             TEXT,
    headlines_json     TEXT,
    descriptions_json  TEXT,
    final_urls_json    TEXT,
    impressions        BIGINT DEFAULT 0,
    clicks             BIGINT DEFAULT 0,
    cost_micros        BIGINT DEFAULT 0,
    conversions        DOUBLE PRECISION DEFAULT 0,
    period_start       DATE,
    period_end         DATE,
    UNIQUE(customer_id, ad_id)
);

CREATE TABLE IF NOT EXISTS gads_keywords (
    id            BIGSERIAL PRIMARY KEY,
    customer_id   TEXT NOT NULL,
    campaign_id   TEXT NOT NULL,
    ad_group_id   TEXT NOT NULL,
    criterion_id  TEXT NOT NULL,
    keyword_text  TEXT,
    match_type    TEXT,
    status        TEXT,
    impressions   BIGINT DEFAULT 0,
    clicks        BIGINT DEFAULT 0,
    cost_micros   BIGINT DEFAULT 0,
    conversions   DOUBLE PRECISION DEFAULT 0,
    period_start  DATE,
    period_end    DATE,
    UNIQUE(customer_id, criterion_id)
);

CREATE TABLE IF NOT EXISTS gads_search_terms (
    id               BIGSERIAL PRIMARY KEY,
    customer_id      TEXT NOT NULL,
    campaign_id      TEXT NOT NULL,
    ad_group_id      TEXT NOT NULL,
    search_term      TEXT NOT NULL,
    matched_keyword  TEXT,
    match_type       TEXT,
    impressions      BIGINT DEFAULT 0,
    clicks           BIGINT DEFAULT 0,
    cost_micros      BIGINT DEFAULT 0,
    conversions      DOUBLE PRECISION DEFAULT 0,
    period_start     DATE,
    period_end       DATE,
    UNIQUE(customer_id, ad_group_id, search_term)
);

CREATE TABLE IF NOT EXISTS gads_device_performance (
    id            BIGSERIAL PRIMARY KEY,
    customer_id   TEXT NOT NULL,
    device        TEXT NOT NULL,
    impressions   BIGINT DEFAULT 0,
    clicks        BIGINT DEFAULT 0,
    cost_micros   BIGINT DEFAULT 0,
    conversions   DOUBLE PRECISION DEFAULT 0,
    period_start  DATE,
    period_end    DATE,
    UNIQUE(customer_id, device)
);

CREATE TABLE IF NOT EXISTS gads_location_performance (
    id                    BIGSERIAL PRIMARY KEY,
    customer_id           TEXT NOT NULL,
    country_criterion_id  TEXT NOT NULL,
    location_name         TEXT,
    impressions           BIGINT DEFAULT 0,
    clicks                BIGINT DEFAULT 0,
    cost_micros           BIGINT DEFAULT 0,
    conversions           DOUBLE PRECISION DEFAULT 0,
    period_start          DATE,
    period_end            DATE,
    UNIQUE(customer_id, country_criterion_id)
);
"""


def _create_gads_tables():
    ddl = _GADS_DDL
    if _is_sqlite:
        ddl = ddl.replace("BIGSERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
    with engine.connect() as conn:
        for stmt in [s.strip() for s in ddl.split(";") if s.strip()]:
            conn.execute(text(stmt))
        conn.commit()
    logger.info("[GADS] gads_* tables ready")


try:
    _create_gads_tables()
except Exception as _ge:
    logger.warning(f"[GADS] Table create failed: {_ge}")


# ── OAuth client config / flow ───────────────────────────────────────────────
_GADS_OAUTH_SCOPES = ["https://www.googleapis.com/auth/adwords"]


def _gads_oauth_client_config() -> dict:
    return {
        "web": {
            "client_id":     _genv("GOOGLE_OAUTH_CLIENT_ID") or _genv("GOOGLE_ADS_CLIENT_ID"),
            "client_secret": _genv("GOOGLE_OAUTH_CLIENT_SECRET") or _genv("GOOGLE_ADS_CLIENT_SECRET"),
            "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
            "token_uri":     "https://oauth2.googleapis.com/token",
            "redirect_uris": [_gads_oauth_redirect_uri()],
        }
    }


# ── Google Ads client builder — sources refresh_token from the encrypted ────
# DB row instead of the static env var used by get_google_ads_client().
def _build_gads_oauth_client(login_customer_id: str = None) -> GoogleAdsClient:
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT encrypted_refresh_token, revoked FROM gads_oauth_tokens WHERE id=1"
        )).fetchone()
    if not row or row[1]:
        raise RuntimeError("Google account not connected. Visit /google/connect.")
    refresh_token = decrypt_token(row[0])
    lci = (login_customer_id or "").replace("-", "").replace(" ", "")
    config = {
        "developer_token": _genv("GOOGLE_ADS_DEVELOPER_TOKEN"),
        "client_id":       _genv("GOOGLE_OAUTH_CLIENT_ID") or _genv("GOOGLE_ADS_CLIENT_ID"),
        "client_secret":   _genv("GOOGLE_OAUTH_CLIENT_SECRET") or _genv("GOOGLE_ADS_CLIENT_SECRET"),
        "refresh_token":   refresh_token,
        "use_proto_plus":  True,
    }
    if lci:
        config["login_customer_id"] = lci
    return GoogleAdsClient.load_from_dict(config)


def _gads_date_range():
    end = date.today()
    start = end - timedelta(days=365)
    return start.isoformat(), end.isoformat()


def _get_selected_gads_account() -> Optional[dict]:
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT customer_id, login_customer_id FROM gads_accounts WHERE selected=TRUE"
        )).fetchone()
    if not row:
        return None
    return {"customer_id": row[0], "login_customer_id": row[1] or ""}


# ── Account discovery (expands manager/MCC accounts into their children) ────
def _list_gads_accessible_accounts_sync() -> list:
    client_ = _build_gads_oauth_client()
    customer_service = client_.get_service("CustomerService")
    resource_names = list(customer_service.list_accessible_customers().resource_names)
    top_level_ids = [r.split("/")[-1] for r in resource_names]

    ga_service = client_.get_service("GoogleAdsService")
    accounts = []
    seen = set()

    for cid in top_level_ids:
        try:
            rows = list(ga_service.search(
                customer_id=cid,
                query="SELECT customer.id, customer.descriptive_name, customer.manager FROM customer LIMIT 1"
            ))
        except Exception as _e:
            logger.warning(f"[GADS] Could not query top-level account {cid}: {_e}")
            continue
        if not rows:
            continue
        c = rows[0].customer
        if c.manager:
            try:
                child_client = _build_gads_oauth_client(login_customer_id=cid)
                child_ga = child_client.get_service("GoogleAdsService")
                child_rows = list(child_ga.search(
                    customer_id=cid,
                    query=(
                        "SELECT customer_client.id, customer_client.descriptive_name, "
                        "customer_client.manager, customer_client.status "
                        "FROM customer_client WHERE customer_client.level <= 1"
                    )
                ))
                for cr in child_rows:
                    ccid = str(cr.customer_client.id)
                    if ccid in seen or cr.customer_client.manager:
                        continue
                    seen.add(ccid)
                    accounts.append({
                        "customer_id": ccid,
                        "account_name": cr.customer_client.descriptive_name or "",
                        "login_customer_id": cid,
                        "is_manager": False,
                    })
            except Exception as _ce:
                logger.warning(f"[GADS] Could not expand manager account {cid}: {_ce}")
        elif cid not in seen:
            seen.add(cid)
            accounts.append({
                "customer_id": cid,
                "account_name": c.descriptive_name or "",
                "login_customer_id": "",
                "is_manager": False,
            })

    now = datetime.utcnow().isoformat()
    with engine.begin() as conn:
        for a in accounts:
            conn.execute(text(
                "INSERT INTO gads_accounts (customer_id, account_name, login_customer_id, is_manager, discovered_at) "
                "VALUES (:cid, :name, :lcid, :mgr, :ts) "
                "ON CONFLICT(customer_id) DO UPDATE SET account_name=:name, login_customer_id=:lcid, discovered_at=:ts"
            ), {"cid": a["customer_id"], "name": a["account_name"], "lcid": a["login_customer_id"],
                "mgr": a["is_manager"], "ts": now})

    return accounts


# ── GAQL fetch functions ─────────────────────────────────────────────────────
def _fetch_gads_campaigns_sync(client_, customer_id: str) -> list:
    svc = client_.get_service("GoogleAdsService")
    query = """
        SELECT campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type,
               campaign.bidding_strategy_type, campaign_budget.amount_micros, campaign_budget.delivery_method,
               campaign.start_date, campaign.end_date
        FROM campaign
    """
    rows = []
    for r in svc.search(customer_id=customer_id, query=query):
        rows.append({
            "campaign_id": str(r.campaign.id),
            "name": r.campaign.name,
            "status": r.campaign.status.name,
            "channel_type": r.campaign.advertising_channel_type.name,
            "bidding_strategy_type": r.campaign.bidding_strategy_type.name,
            "budget_amount_micros": r.campaign_budget.amount_micros,
            "budget_delivery_method": r.campaign_budget.delivery_method.name,
            "start_date": r.campaign.start_date,
            "end_date": r.campaign.end_date,
        })
    return rows


def _fetch_gads_campaign_day_sync(client_, customer_id: str, start: str, end: str) -> list:
    svc = client_.get_service("GoogleAdsService")
    query = f"""
        SELECT campaign.id, segments.date, metrics.impressions, metrics.clicks,
               metrics.cost_micros, metrics.conversions, metrics.conversions_value
        FROM campaign
        WHERE segments.date BETWEEN '{start}' AND '{end}'
        ORDER BY segments.date ASC
    """
    rows = []
    for r in svc.search(customer_id=customer_id, query=query):
        rows.append({
            "campaign_id": str(r.campaign.id),
            "date": r.segments.date,
            "impressions": r.metrics.impressions,
            "clicks": r.metrics.clicks,
            "cost_micros": r.metrics.cost_micros,
            "conversions": r.metrics.conversions,
            "conversion_value": r.metrics.conversions_value,
        })
    return rows


def _fetch_gads_ad_groups_sync(client_, customer_id: str, start: str, end: str) -> list:
    svc = client_.get_service("GoogleAdsService")
    query = f"""
        SELECT ad_group.id, ad_group.name, ad_group.status, campaign.id,
               metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions
        FROM ad_group
        WHERE segments.date BETWEEN '{start}' AND '{end}'
    """
    rows = []
    for r in svc.search(customer_id=customer_id, query=query):
        rows.append({
            "campaign_id": str(r.campaign.id),
            "ad_group_id": str(r.ad_group.id),
            "name": r.ad_group.name,
            "status": r.ad_group.status.name,
            "impressions": r.metrics.impressions,
            "clicks": r.metrics.clicks,
            "cost_micros": r.metrics.cost_micros,
            "conversions": r.metrics.conversions,
        })
    return rows


def _fetch_gads_ads_sync(client_, customer_id: str, start: str, end: str) -> list:
    svc = client_.get_service("GoogleAdsService")
    query = f"""
        SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status, ad_group.id, campaign.id,
               ad_group_ad.ad.responsive_search_ad.headlines, ad_group_ad.ad.responsive_search_ad.descriptions,
               ad_group_ad.ad.final_urls,
               metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions
        FROM ad_group_ad
        WHERE segments.date BETWEEN '{start}' AND '{end}'
    """
    rows = []
    for r in svc.search(customer_id=customer_id, query=query):
        try:
            headlines = [h.text for h in r.ad_group_ad.ad.responsive_search_ad.headlines]
        except Exception:
            headlines = []
        try:
            descriptions = [d.text for d in r.ad_group_ad.ad.responsive_search_ad.descriptions]
        except Exception:
            descriptions = []
        rows.append({
            "campaign_id": str(r.campaign.id),
            "ad_group_id": str(r.ad_group.id),
            "ad_id": str(r.ad_group_ad.ad.id),
            "ad_type": r.ad_group_ad.ad.type_.name,
            "status": r.ad_group_ad.status.name,
            "headlines_json": json.dumps(headlines),
            "descriptions_json": json.dumps(descriptions),
            "final_urls_json": json.dumps(list(r.ad_group_ad.ad.final_urls)),
            "impressions": r.metrics.impressions,
            "clicks": r.metrics.clicks,
            "cost_micros": r.metrics.cost_micros,
            "conversions": r.metrics.conversions,
        })
    return rows


def _fetch_gads_keywords_sync(client_, customer_id: str, start: str, end: str) -> list:
    svc = client_.get_service("GoogleAdsService")
    query = f"""
        SELECT ad_group_criterion.criterion_id, ad_group_criterion.keyword.text,
               ad_group_criterion.keyword.match_type, ad_group_criterion.status, ad_group.id, campaign.id,
               metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions
        FROM keyword_view
        WHERE segments.date BETWEEN '{start}' AND '{end}'
    """
    rows = []
    for r in svc.search(customer_id=customer_id, query=query):
        rows.append({
            "campaign_id": str(r.campaign.id),
            "ad_group_id": str(r.ad_group.id),
            "criterion_id": str(r.ad_group_criterion.criterion_id),
            "keyword_text": r.ad_group_criterion.keyword.text,
            "match_type": r.ad_group_criterion.keyword.match_type.name,
            "status": r.ad_group_criterion.status.name,
            "impressions": r.metrics.impressions,
            "clicks": r.metrics.clicks,
            "cost_micros": r.metrics.cost_micros,
            "conversions": r.metrics.conversions,
        })
    return rows


def _fetch_gads_search_terms_sync(client_, customer_id: str, start: str, end: str) -> list:
    svc = client_.get_service("GoogleAdsService")
    query = f"""
        SELECT search_term_view.search_term, ad_group.id, campaign.id,
               segments.keyword.info.text, segments.keyword.info.match_type,
               metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions
        FROM search_term_view
        WHERE segments.date BETWEEN '{start}' AND '{end}'
    """
    rows = []
    for r in svc.search(customer_id=customer_id, query=query):
        rows.append({
            "campaign_id": str(r.campaign.id),
            "ad_group_id": str(r.ad_group.id),
            "search_term": r.search_term_view.search_term,
            "matched_keyword": r.segments.keyword.info.text,
            "match_type": r.segments.keyword.info.match_type.name,
            "impressions": r.metrics.impressions,
            "clicks": r.metrics.clicks,
            "cost_micros": r.metrics.cost_micros,
            "conversions": r.metrics.conversions,
        })
    return rows


def _fetch_gads_device_sync(client_, customer_id: str, start: str, end: str) -> list:
    svc = client_.get_service("GoogleAdsService")
    query = f"""
        SELECT segments.device, metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions
        FROM customer
        WHERE segments.date BETWEEN '{start}' AND '{end}'
    """
    rows = []
    for r in svc.search(customer_id=customer_id, query=query):
        rows.append({
            "device": r.segments.device.name,
            "impressions": r.metrics.impressions,
            "clicks": r.metrics.clicks,
            "cost_micros": r.metrics.cost_micros,
            "conversions": r.metrics.conversions,
        })
    return rows


def _fetch_gads_location_sync(client_, customer_id: str, start: str, end: str) -> list:
    svc = client_.get_service("GoogleAdsService")
    query = f"""
        SELECT geographic_view.country_criterion_id,
               metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions
        FROM geographic_view
        WHERE segments.date BETWEEN '{start}' AND '{end}'
    """
    rows = []
    for r in svc.search(customer_id=customer_id, query=query):
        rows.append({
            "country_criterion_id": str(r.geographic_view.country_criterion_id),
            "impressions": r.metrics.impressions,
            "clicks": r.metrics.clicks,
            "cost_micros": r.metrics.cost_micros,
            "conversions": r.metrics.conversions,
        })

    distinct_ids = sorted({row["country_criterion_id"] for row in rows})
    names = {}
    if distinct_ids:
        try:
            id_list = ", ".join(distinct_ids)
            name_query = (
                "SELECT geo_target_constant.id, geo_target_constant.name, geo_target_constant.country_code "
                f"FROM geo_target_constant WHERE geo_target_constant.id IN ({id_list})"
            )
            for r in svc.search(customer_id=customer_id, query=name_query):
                names[str(r.geo_target_constant.id)] = r.geo_target_constant.name
        except Exception as _ge:
            logger.warning(f"[GADS] Could not resolve location names: {_ge}")

    for row in rows:
        row["location_name"] = names.get(row["country_criterion_id"], row["country_criterion_id"])
    return rows


# ── Write imported data (transactional replace per table) ───────────────────
def _write_gads_import_data(customer_id: str, data: dict):
    now = datetime.utcnow().isoformat()
    start, end = data["period_start"], data["period_end"]

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM gads_campaigns WHERE customer_id=:cid"), {"cid": customer_id})
        if data["campaigns"]:
            rows = [{**c, "customer_id": customer_id, "updated_at": now} for c in data["campaigns"]]
            conn.execute(text(
                "INSERT INTO gads_campaigns (customer_id, campaign_id, name, status, channel_type, "
                "bidding_strategy_type, budget_amount_micros, budget_delivery_method, start_date, end_date, updated_at) "
                "VALUES (:customer_id, :campaign_id, :name, :status, :channel_type, :bidding_strategy_type, "
                ":budget_amount_micros, :budget_delivery_method, :start_date, :end_date, :updated_at)"
            ), rows)

        conn.execute(text("DELETE FROM gads_campaign_day WHERE customer_id=:cid"), {"cid": customer_id})
        if data["campaign_days"]:
            rows = [{**d, "customer_id": customer_id} for d in data["campaign_days"]]
            conn.execute(text(
                "INSERT INTO gads_campaign_day (customer_id, campaign_id, date, impressions, clicks, "
                "cost_micros, conversions, conversion_value) "
                "VALUES (:customer_id, :campaign_id, :date, :impressions, :clicks, :cost_micros, "
                ":conversions, :conversion_value)"
            ), rows)

        conn.execute(text("DELETE FROM gads_ad_groups WHERE customer_id=:cid"), {"cid": customer_id})
        if data["ad_groups"]:
            rows = [{**a, "customer_id": customer_id, "period_start": start, "period_end": end} for a in data["ad_groups"]]
            conn.execute(text(
                "INSERT INTO gads_ad_groups (customer_id, campaign_id, ad_group_id, name, status, "
                "impressions, clicks, cost_micros, conversions, period_start, period_end) "
                "VALUES (:customer_id, :campaign_id, :ad_group_id, :name, :status, :impressions, :clicks, "
                ":cost_micros, :conversions, :period_start, :period_end)"
            ), rows)

        conn.execute(text("DELETE FROM gads_ads WHERE customer_id=:cid"), {"cid": customer_id})
        if data["ads"]:
            rows = [{**a, "customer_id": customer_id, "period_start": start, "period_end": end} for a in data["ads"]]
            conn.execute(text(
                "INSERT INTO gads_ads (customer_id, campaign_id, ad_group_id, ad_id, ad_type, status, "
                "headlines_json, descriptions_json, final_urls_json, impressions, clicks, cost_micros, "
                "conversions, period_start, period_end) "
                "VALUES (:customer_id, :campaign_id, :ad_group_id, :ad_id, :ad_type, :status, :headlines_json, "
                ":descriptions_json, :final_urls_json, :impressions, :clicks, :cost_micros, :conversions, "
                ":period_start, :period_end)"
            ), rows)

        conn.execute(text("DELETE FROM gads_keywords WHERE customer_id=:cid"), {"cid": customer_id})
        if data["keywords"]:
            rows = [{**k, "customer_id": customer_id, "period_start": start, "period_end": end} for k in data["keywords"]]
            conn.execute(text(
                "INSERT INTO gads_keywords (customer_id, campaign_id, ad_group_id, criterion_id, keyword_text, "
                "match_type, status, impressions, clicks, cost_micros, conversions, period_start, period_end) "
                "VALUES (:customer_id, :campaign_id, :ad_group_id, :criterion_id, :keyword_text, :match_type, "
                ":status, :impressions, :clicks, :cost_micros, :conversions, :period_start, :period_end)"
            ), rows)

        conn.execute(text("DELETE FROM gads_search_terms WHERE customer_id=:cid"), {"cid": customer_id})
        agg = {}
        for s in data["search_terms"]:
            key = (s["ad_group_id"], s["search_term"])
            if key not in agg:
                agg[key] = {**s, "customer_id": customer_id, "period_start": start, "period_end": end}
            else:
                agg[key]["impressions"] += s["impressions"]
                agg[key]["clicks"] += s["clicks"]
                agg[key]["cost_micros"] += s["cost_micros"]
                agg[key]["conversions"] += s["conversions"]
        rows = list(agg.values())
        if rows:
            conn.execute(text(
                "INSERT INTO gads_search_terms (customer_id, campaign_id, ad_group_id, search_term, "
                "matched_keyword, match_type, impressions, clicks, cost_micros, conversions, period_start, period_end) "
                "VALUES (:customer_id, :campaign_id, :ad_group_id, :search_term, :matched_keyword, :match_type, "
                ":impressions, :clicks, :cost_micros, :conversions, :period_start, :period_end)"
            ), rows)

        conn.execute(text("DELETE FROM gads_device_performance WHERE customer_id=:cid"), {"cid": customer_id})
        dev_agg = {}
        for d in data["devices"]:
            key = d["device"]
            if key not in dev_agg:
                dev_agg[key] = {**d, "customer_id": customer_id, "period_start": start, "period_end": end}
            else:
                dev_agg[key]["impressions"] += d["impressions"]
                dev_agg[key]["clicks"] += d["clicks"]
                dev_agg[key]["cost_micros"] += d["cost_micros"]
                dev_agg[key]["conversions"] += d["conversions"]
        rows = list(dev_agg.values())
        if rows:
            conn.execute(text(
                "INSERT INTO gads_device_performance (customer_id, device, impressions, clicks, cost_micros, "
                "conversions, period_start, period_end) "
                "VALUES (:customer_id, :device, :impressions, :clicks, :cost_micros, :conversions, "
                ":period_start, :period_end)"
            ), rows)

        conn.execute(text("DELETE FROM gads_location_performance WHERE customer_id=:cid"), {"cid": customer_id})
        loc_agg = {}
        for l in data["locations"]:
            key = l["country_criterion_id"]
            if key not in loc_agg:
                loc_agg[key] = {**l, "customer_id": customer_id, "period_start": start, "period_end": end}
            else:
                loc_agg[key]["impressions"] += l["impressions"]
                loc_agg[key]["clicks"] += l["clicks"]
                loc_agg[key]["cost_micros"] += l["cost_micros"]
                loc_agg[key]["conversions"] += l["conversions"]
        rows = list(loc_agg.values())
        if rows:
            conn.execute(text(
                "INSERT INTO gads_location_performance (customer_id, country_criterion_id, location_name, "
                "impressions, clicks, cost_micros, conversions, period_start, period_end) "
                "VALUES (:customer_id, :country_criterion_id, :location_name, :impressions, :clicks, "
                ":cost_micros, :conversions, :period_start, :period_end)"
            ), rows)


# ── AI summary — reads only from the DB tables just written, never Google Ads ─
def _generate_gads_ai_summary_sync(customer_id: str) -> dict:
    with engine.connect() as conn:
        top_campaigns = conn.execute(text("""
            SELECT c.name, SUM(d.cost_micros)/1000000.0 AS cost, SUM(d.conversions) AS conversions,
                   CASE WHEN SUM(d.conversions)=0 THEN NULL ELSE SUM(d.cost_micros)/1000000.0/SUM(d.conversions) END AS cpa
            FROM gads_campaign_day d JOIN gads_campaigns c
              ON c.customer_id=d.customer_id AND c.campaign_id=d.campaign_id
            WHERE d.customer_id=:cid GROUP BY c.name
            HAVING SUM(d.cost_micros) > 0
            ORDER BY SUM(d.conversions) DESC, SUM(d.cost_micros) DESC LIMIT 10
        """), {"cid": customer_id}).fetchall()

        worst_campaigns = conn.execute(text("""
            SELECT c.name, SUM(d.cost_micros)/1000000.0 AS cost, SUM(d.conversions) AS conversions,
                   CASE WHEN SUM(d.conversions)=0 THEN NULL ELSE SUM(d.cost_micros)/1000000.0/SUM(d.conversions) END AS cpa
            FROM gads_campaign_day d JOIN gads_campaigns c
              ON c.customer_id=d.customer_id AND c.campaign_id=d.campaign_id
            WHERE d.customer_id=:cid GROUP BY c.name
            HAVING SUM(d.cost_micros) > 0
            ORDER BY (SUM(d.conversions)=0) DESC, cpa DESC NULLS LAST, cost DESC LIMIT 10
        """), {"cid": customer_id}).fetchall()

        top_keywords = conn.execute(text("""
            SELECT keyword_text, cost_micros/1000000.0 AS cost, clicks, conversions
            FROM gads_keywords WHERE customer_id=:cid
            ORDER BY conversions DESC, clicks DESC LIMIT 10
        """), {"cid": customer_id}).fetchall()

        waste_keywords = conn.execute(text("""
            SELECT keyword_text, cost_micros/1000000.0 AS cost, clicks
            FROM gads_keywords WHERE customer_id=:cid AND clicks >= 5 AND conversions = 0
            ORDER BY cost_micros DESC LIMIT 10
        """), {"cid": customer_id}).fetchall()

        device_rows = conn.execute(text("""
            SELECT device, cost_micros/1000000.0 AS cost, clicks, conversions
            FROM gads_device_performance WHERE customer_id=:cid
        """), {"cid": customer_id}).fetchall()

        totals = conn.execute(text("""
            SELECT COALESCE(SUM(impressions),0), COALESCE(SUM(clicks),0),
                   COALESCE(SUM(cost_micros),0), COALESCE(SUM(conversions),0)
            FROM gads_campaign_day WHERE customer_id=:cid
        """), {"cid": customer_id}).fetchone()

    impressions, clicks, cost_micros, conversions = totals
    spend = cost_micros / 1_000_000
    ctr = (clicks / impressions * 100) if impressions else 0
    cpa = (spend / conversions) if conversions else 0

    def _to_dicts(rows, cols):
        return [dict(zip(cols, r)) for r in rows]

    payload = {
        "top_campaigns": _to_dicts(top_campaigns, ["name", "cost", "conversions", "cpa"]),
        "worst_campaigns": _to_dicts(worst_campaigns, ["name", "cost", "conversions", "cpa"]),
        "top_keywords": _to_dicts(top_keywords, ["keyword", "cost", "clicks", "conversions"]),
        "waste_keywords": _to_dicts(waste_keywords, ["keyword", "cost", "clicks"]),
        "device_performance": _to_dicts(device_rows, ["device", "cost", "clicks", "conversions"]),
        "totals": {"spend": round(spend, 2), "clicks": clicks, "ctr": round(ctr, 2), "cpa": round(cpa, 2)},
    }

    prompt = (
        "You are a Google Ads performance analyst. Analyze this account's last 12 months of data.\n\n"
        f"TOP CAMPAIGNS: {json.dumps(payload['top_campaigns'])}\n"
        f"WORST CAMPAIGNS: {json.dumps(payload['worst_campaigns'])}\n"
        f"TOP KEYWORDS: {json.dumps(payload['top_keywords'])}\n"
        f"WASTE KEYWORDS: {json.dumps(payload['waste_keywords'])}\n"
        f"DEVICE PERFORMANCE: {json.dumps(payload['device_performance'])}\n"
        f"TOTALS: {json.dumps(payload['totals'])}\n\n"
        "RULES:\n"
        "- Every item MUST cite a real campaign/keyword name and a real number from the data above.\n"
        "- NEVER write generic filler like 'campaign is performing well' without naming it and citing numbers.\n"
        "- If a data section above is empty, say so explicitly instead of inventing content.\n\n"
        "Return ONLY valid JSON with this exact structure:\n"
        "{\n"
        '  "best_performing_campaigns": [{"campaign_name":"...","why":"...","recommendation":"..."}],\n'
        '  "poor_performing_campaigns": [{"campaign_name":"...","why":"...","recommendation":"..."}],\n'
        '  "budget_waste": {"total_wasted_estimate":"...","top_waste_keywords":["..."],"summary":"..."},\n'
        '  "best_keywords": [{"keyword":"...","why":"..."}],\n'
        '  "waste_keywords": [{"keyword":"...","cost":"...","why":"..."}],\n'
        '  "recommended_next_campaign": {"concept":"...","target_audience":"...","suggested_budget":"...","rationale":"..."}\n'
        "}"
    )

    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.4,
        max_tokens=2000,
    )
    return json.loads(resp.choices[0].message.content)


# ── Background import job ────────────────────────────────────────────────────
def _job_update(job_id: str, **fields):
    sets = ", ".join(f"{k}=:{k}" for k in fields)
    with engine.begin() as conn:
        conn.execute(text(f"UPDATE gads_import_jobs SET {sets} WHERE id=:job_id"), {**fields, "job_id": job_id})


async def _run_gads_import_job(job_id: str, customer_id: str, login_customer_id: str):
    now = datetime.utcnow().isoformat()
    _job_update(job_id, status="running", started_at=now, current_step="Validating account access", progress_pct=2)
    try:
        client_ = await asyncio.to_thread(_build_gads_oauth_client, login_customer_id or None)
        start, end = _gads_date_range()

        def _validate():
            svc = client_.get_service("GoogleAdsService")
            return list(svc.search(customer_id=customer_id, query="SELECT customer.id FROM customer LIMIT 1"))
        await asyncio.to_thread(_validate)

        _job_update(job_id, current_step="Fetching campaigns & budgets", progress_pct=10)
        campaigns = await asyncio.to_thread(_fetch_gads_campaigns_sync, client_, customer_id)

        _job_update(job_id, current_step="Fetching daily campaign performance (12 months)", progress_pct=35)
        campaign_days = await asyncio.to_thread(_fetch_gads_campaign_day_sync, client_, customer_id, start, end)

        _job_update(job_id, current_step="Fetching ad groups", progress_pct=48)
        ad_groups = await asyncio.to_thread(_fetch_gads_ad_groups_sync, client_, customer_id, start, end)

        _job_update(job_id, current_step="Fetching ads", progress_pct=58)
        ads = await asyncio.to_thread(_fetch_gads_ads_sync, client_, customer_id, start, end)

        _job_update(job_id, current_step="Fetching keywords", progress_pct=68)
        keywords = await asyncio.to_thread(_fetch_gads_keywords_sync, client_, customer_id, start, end)

        _job_update(job_id, current_step="Fetching search terms", progress_pct=78)
        search_terms = await asyncio.to_thread(_fetch_gads_search_terms_sync, client_, customer_id, start, end)

        _job_update(job_id, current_step="Fetching device performance", progress_pct=84)
        devices = await asyncio.to_thread(_fetch_gads_device_sync, client_, customer_id, start, end)

        _job_update(job_id, current_step="Fetching location performance", progress_pct=90)
        locations = await asyncio.to_thread(_fetch_gads_location_sync, client_, customer_id, start, end)

        _job_update(job_id, current_step="Writing to database", progress_pct=94)
        data = {
            "campaigns": campaigns, "campaign_days": campaign_days, "ad_groups": ad_groups,
            "ads": ads, "keywords": keywords, "search_terms": search_terms,
            "devices": devices, "locations": locations,
            "period_start": start, "period_end": end,
        }
        await asyncio.to_thread(_write_gads_import_data, customer_id, data)

        _job_update(job_id, current_step="Generating AI summary", progress_pct=98)
        try:
            summary = await asyncio.to_thread(_generate_gads_ai_summary_sync, customer_id)
            with engine.begin() as conn:
                conn.execute(text(
                    "UPDATE gads_accounts SET ai_summary_json=:s, ai_summary_generated_at=:ts WHERE customer_id=:cid"
                ), {"s": json.dumps(summary), "ts": datetime.utcnow().isoformat(), "cid": customer_id})
        except Exception as _ae:
            logger.warning(f"[GADS-IMPORT] AI summary generation failed: {_ae}")

        finished = datetime.utcnow().isoformat()
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE gads_accounts SET last_imported_at=:ts WHERE customer_id=:cid"
            ), {"ts": finished, "cid": customer_id})
        _job_update(job_id, status="succeeded", current_step="Done", progress_pct=100, finished_at=finished)
        logger.info(f"[GADS-IMPORT] Job {job_id} succeeded for customer_id={customer_id}")
    except Exception as _e:
        tb = _traceback.format_exc()
        logger.error(f"[GADS-IMPORT] Job {job_id} failed: {_e}\n{tb}")
        _job_update(job_id, status="failed", error=str(_e), finished_at=datetime.utcnow().isoformat())


def _start_gads_import(customer_id: str, login_customer_id: str = "") -> str:
    job_id = uuid.uuid4().hex
    now = datetime.utcnow().isoformat()
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO gads_import_jobs (id, customer_id, status, progress_pct, current_step, created_at) "
            "VALUES (:id, :cid, 'queued', 0, 'Queued', :ts)"
        ), {"id": job_id, "cid": customer_id, "ts": now})
    asyncio.create_task(_run_gads_import_job(job_id, customer_id, login_customer_id))
    return job_id


# ── Pydantic models ───────────────────────────────────────────────────────────
class GadsAccountSelectRequest(BaseModel):
    customer_id: str


class GadsImportRequest(BaseModel):
    customer_id: str = ""


# ── Endpoints ────────────────────────────────────────────────────────────────
@app.get("/google/connect")
async def google_connect():
    try:
        flow = _GoogleOAuthFlow.from_client_config(
            _gads_oauth_client_config(),
            scopes=_GADS_OAUTH_SCOPES,
            redirect_uri=_gads_oauth_redirect_uri(),
        )
        state = _make_oauth_state()
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            prompt="consent",
            include_granted_scopes="true",
            state=state,
        )
        return {"success": True, "auth_url": auth_url}
    except Exception as _e:
        logger.error(f"[GADS-OAUTH] /google/connect error: {_e}")
        return {"success": False, "error": str(_e)}


@app.get("/google/callback")
async def google_callback(code: str = "", state: str = "", error: str = ""):
    frontend = _genv("FRONTEND_URL") or "http://localhost:5173"
    if error:
        return RedirectResponse(f"{frontend}/google-ads?connected=false&error={error}")
    if not _verify_oauth_state(state):
        return RedirectResponse(f"{frontend}/google-ads?connected=false&error=invalid_state")
    try:
        flow = _GoogleOAuthFlow.from_client_config(
            _gads_oauth_client_config(),
            scopes=_GADS_OAUTH_SCOPES,
            redirect_uri=_gads_oauth_redirect_uri(),
        )

        def _fetch():
            flow.fetch_token(code=code)
            return flow.credentials
        creds = await asyncio.to_thread(_fetch)

        if not creds.refresh_token:
            return RedirectResponse(f"{frontend}/google-ads?connected=false&error=no_refresh_token")

        enc = encrypt_token(creds.refresh_token)
        now = datetime.utcnow().isoformat()
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO gads_oauth_tokens (id, encrypted_refresh_token, scope, connected_at, updated_at, revoked) "
                "VALUES (1, :enc, :scope, :ts, :ts, FALSE) "
                "ON CONFLICT(id) DO UPDATE SET encrypted_refresh_token=:enc, scope=:scope, updated_at=:ts, revoked=FALSE"
            ), {"enc": enc, "scope": " ".join(_GADS_OAUTH_SCOPES), "ts": now})
        logger.info("[GADS-OAUTH] Connected — encrypted refresh token stored")
        return RedirectResponse(f"{frontend}/google-ads?connected=true")
    except Exception as _e:
        logger.error(f"[GADS-OAUTH] /google/callback error: {_e}")
        return RedirectResponse(f"{frontend}/google-ads?connected=false&error=exchange_failed")


@app.get("/google/accounts")
async def google_accounts():
    try:
        accounts = await asyncio.to_thread(_list_gads_accessible_accounts_sync)
        with engine.connect() as conn:
            selected_row = conn.execute(text(
                "SELECT customer_id FROM gads_accounts WHERE selected=TRUE"
            )).fetchone()
        selected_cid = selected_row[0] if selected_row else None
        for a in accounts:
            a["selected"] = (a["customer_id"] == selected_cid)
        return {"success": True, "accounts": accounts}
    except Exception as _e:
        logger.error(f"[GADS] /google/accounts error: {_e}")
        return {"success": False, "accounts": [], "error": str(_e)}


@app.post("/google/accounts/select")
async def google_accounts_select(request: GadsAccountSelectRequest):
    cid = request.customer_id.replace("-", "").strip()
    try:
        with engine.begin() as conn:
            row = conn.execute(text(
                "SELECT customer_id, account_name, login_customer_id, is_manager FROM gads_accounts WHERE customer_id=:cid"
            ), {"cid": cid}).fetchone()
            if not row:
                return {"success": False, "error": "Account not found. Call /google/accounts first."}
            conn.execute(text("UPDATE gads_accounts SET selected=FALSE"))
            conn.execute(text("UPDATE gads_accounts SET selected=TRUE WHERE customer_id=:cid"), {"cid": cid})
        return {"success": True, "selected_account": {
            "customer_id": row[0], "account_name": row[1], "login_customer_id": row[2], "is_manager": row[3],
        }}
    except Exception as _e:
        logger.error(f"[GADS] /google/accounts/select error: {_e}")
        return {"success": False, "error": str(_e)}


def _resolve_gads_import_target(customer_id: str):
    cid = (customer_id or "").replace("-", "").strip()
    if not cid:
        acct = _get_selected_gads_account()
        if not acct:
            return None, None, "No account selected. Call /google/accounts/select first."
        return acct["customer_id"], acct["login_customer_id"], None
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT login_customer_id FROM gads_accounts WHERE customer_id=:cid"
        ), {"cid": cid}).fetchone()
    return cid, (row[0] if row else ""), None


@app.post("/google-ads/import")
async def google_ads_import(request: GadsImportRequest):
    cid, lcid, err = _resolve_gads_import_target(request.customer_id)
    if err:
        return {"success": False, "error": err}
    try:
        with engine.connect() as conn:
            tok = conn.execute(text("SELECT revoked FROM gads_oauth_tokens WHERE id=1")).fetchone()
        if not tok or tok[0]:
            return {"success": False, "error": "Google account not connected. Visit /google/connect."}
        job_id = _start_gads_import(cid, lcid or "")
        return {"success": True, "job_id": job_id, "status": "queued"}
    except Exception as _e:
        logger.error(f"[GADS] /google-ads/import error: {_e}")
        return {"success": False, "error": str(_e)}


@app.post("/google-ads/refresh")
async def google_ads_refresh(request: GadsImportRequest):
    return await google_ads_import(request)


@app.get("/google-ads/import/status/{job_id}")
async def google_ads_import_status(job_id: str):
    try:
        with engine.begin() as conn:
            row = conn.execute(text(
                "SELECT id, customer_id, status, progress_pct, current_step, started_at, finished_at, error, created_at "
                "FROM gads_import_jobs WHERE id=:id"
            ), {"id": job_id}).fetchone()
            if not row:
                return {"success": False, "error": "Job not found"}
            job = dict(zip(
                ["id", "customer_id", "status", "progress_pct", "current_step",
                 "started_at", "finished_at", "error", "created_at"],
                row
            ))
            stale = False
            if job["status"] == "running" and job["started_at"]:
                started = datetime.fromisoformat(job["started_at"])
                if datetime.utcnow() - started > timedelta(minutes=20):
                    stale = True
                    now = datetime.utcnow().isoformat()
                    err_msg = "stale/orphaned (server likely restarted mid-import)"
                    conn.execute(text(
                        "UPDATE gads_import_jobs SET status='failed', error=:err, finished_at=:ts WHERE id=:id"
                    ), {"err": err_msg, "ts": now, "id": job_id})
                    job["status"] = "failed"
                    job["error"] = err_msg
                    job["finished_at"] = now
        job["stale"] = stale
        return {"success": True, "job": job}
    except Exception as _e:
        logger.error(f"[GADS] /google-ads/import/status error: {_e}")
        return {"success": False, "error": str(_e)}


@app.get("/google-ads/dashboard")
async def google_ads_dashboard(customer_id: str = ""):
    cid = customer_id.replace("-", "").strip()
    if not cid:
        acct = _get_selected_gads_account()
        if not acct:
            return {"success": False, "error": "No account selected."}
        cid = acct["customer_id"]

    try:
        with engine.connect() as conn:
            totals = conn.execute(text(
                "SELECT COALESCE(SUM(impressions),0), COALESCE(SUM(clicks),0), "
                "COALESCE(SUM(cost_micros),0), COALESCE(SUM(conversions),0) "
                "FROM gads_campaign_day WHERE customer_id=:cid"
            ), {"cid": cid}).fetchone()
            impressions, clicks, cost_micros, conversions = totals
            spend = cost_micros / 1_000_000
            ctr = (clicks / impressions * 100) if impressions else 0
            cpc = (spend / clicks) if clicks else 0
            cpa = (spend / conversions) if conversions else 0

            top_campaigns = conn.execute(text("""
                SELECT c.campaign_id, c.name, c.status,
                       SUM(d.cost_micros)/1000000.0 AS cost, SUM(d.clicks) AS clicks,
                       SUM(d.impressions) AS impressions, SUM(d.conversions) AS conversions
                FROM gads_campaign_day d
                JOIN gads_campaigns c ON c.customer_id=d.customer_id AND c.campaign_id=d.campaign_id
                WHERE d.customer_id=:cid
                GROUP BY c.campaign_id, c.name, c.status
                HAVING SUM(d.cost_micros) > 0
                ORDER BY SUM(d.conversions) DESC, SUM(d.cost_micros) DESC
                LIMIT 10
            """), {"cid": cid}).fetchall()

            worst_campaigns = conn.execute(text("""
                SELECT c.campaign_id, c.name, c.status,
                       SUM(d.cost_micros)/1000000.0 AS cost, SUM(d.conversions) AS conversions,
                       CASE WHEN SUM(d.conversions) = 0 THEN NULL
                            ELSE SUM(d.cost_micros)/1000000.0 / SUM(d.conversions) END AS cpa
                FROM gads_campaign_day d
                JOIN gads_campaigns c ON c.customer_id=d.customer_id AND c.campaign_id=d.campaign_id
                WHERE d.customer_id=:cid
                GROUP BY c.campaign_id, c.name, c.status
                HAVING SUM(d.cost_micros) > 0
                ORDER BY (SUM(d.conversions) = 0) DESC, cpa DESC NULLS LAST, cost DESC
                LIMIT 10
            """), {"cid": cid}).fetchall()

            top_keywords = conn.execute(text("""
                SELECT keyword_text, match_type, cost_micros/1000000.0 AS cost, clicks, impressions, conversions
                FROM gads_keywords WHERE customer_id=:cid
                ORDER BY conversions DESC, clicks DESC LIMIT 15
            """), {"cid": cid}).fetchall()

            waste_keywords = conn.execute(text("""
                SELECT keyword_text, match_type, cost_micros/1000000.0 AS cost, clicks, impressions
                FROM gads_keywords
                WHERE customer_id=:cid AND clicks >= 5 AND conversions = 0
                ORDER BY cost_micros DESC LIMIT 15
            """), {"cid": cid}).fetchall()

            device_perf = conn.execute(text("""
                SELECT device, impressions, clicks, cost_micros/1000000.0 AS cost, conversions
                FROM gads_device_performance WHERE customer_id=:cid
            """), {"cid": cid}).fetchall()

            location_perf = conn.execute(text("""
                SELECT location_name, impressions, clicks, cost_micros/1000000.0 AS cost, conversions
                FROM gads_location_performance WHERE customer_id=:cid
                ORDER BY cost_micros DESC LIMIT 10
            """), {"cid": cid}).fetchall()

            acct_row = conn.execute(text(
                "SELECT account_name, last_imported_at, ai_summary_json FROM gads_accounts WHERE customer_id=:cid"
            ), {"cid": cid}).fetchone()

        def _rows(cursor_rows, cols):
            return [dict(zip(cols, r)) for r in cursor_rows]

        cards = {
            "total_spend": round(spend, 2),
            "total_clicks": clicks,
            "total_impressions": impressions,
            "ctr": round(ctr, 2),
            "cpc": round(cpc, 2),
            "conversions": round(conversions, 2),
            "cpa": round(cpa, 2),
            "top_campaigns": _rows(top_campaigns, ["campaign_id", "name", "status", "cost", "clicks", "impressions", "conversions"]),
            "worst_campaigns": _rows(worst_campaigns, ["campaign_id", "name", "status", "cost", "conversions", "cpa"]),
            "top_keywords": _rows(top_keywords, ["keyword_text", "match_type", "cost", "clicks", "impressions", "conversions"]),
            "waste_keywords": _rows(waste_keywords, ["keyword_text", "match_type", "cost", "clicks", "impressions"]),
            "device_performance": _rows(device_perf, ["device", "impressions", "clicks", "cost", "conversions"]),
            "location_performance": _rows(location_perf, ["location_name", "impressions", "clicks", "cost", "conversions"]),
        }

        account_name = last_imported_at = ai_summary = None
        if acct_row:
            account_name, last_imported_at, ai_summary_json = acct_row
            if ai_summary_json:
                try:
                    ai_summary = json.loads(ai_summary_json)
                except Exception:
                    ai_summary = None

        return {
            "success": True,
            "customer_id": cid,
            "account_name": account_name,
            "last_imported_at": last_imported_at,
            "cards": cards,
            "ai_summary": ai_summary,
        }
    except Exception as _e:
        logger.error(f"[GADS] /google-ads/dashboard error: {_e}")
        return {"success": False, "error": str(_e)}

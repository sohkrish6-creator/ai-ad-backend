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
import traceback as _traceback
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
"""

def _create_memory_tables():
    """
    Create memory tables with TEXT columns for JSON data.
    If tables already exist with JSONB columns (from a prior bad schema),
    drop and recreate them — they are always empty if saves were failing.
    """
    _memory_table_names = ["business_memory", "market_memory", "competitor_memory",
                           "audience_memory", "campaign_memory", "opportunity_memory",
                           "offer_memory", "website_memory", "visibility_memory", "outreach_memory", "kpi_memory", "performance_memory", "optimizer_memory", "result_memory", "growth_memory", "prospect_memory"]
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
    B2B (url + target industry + city):    "sohscape.com::hospitality::jaipur"
    Industry-only mode (no url):           "hospitality::jaipur"

    City always defaults to "jaipur" when blank so keys are ALWAYS complete
    and consistent regardless of whether the frontend sent city="" or city="Jaipur".
    """
    # City default: ALWAYS "jaipur" when blank — prevents key mismatches
    _tc = (city or "").strip().lower() or "jaipur"

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
    1. Try exact derived key (city defaults to 'jaipur' in derive_business_key).
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
    "Game-changer":  "strong tool","game-changer": "strong tool",
    "Game changer":  "strong tool","game changer":  "strong tool",
    "Dive in":       "Start","dive in":            "start",
}

def _clean_banned_words(text: str) -> str:
    """Post-processing: replace banned buzzwords with plain alternatives.
    Logs a warning for each hit so prompt engineers can track leakage.
    """
    for bad, good in _BANNED_WORD_MAP.items():
        if bad in text:
            logger.warning(f"[BANNED-WORD] '{bad.strip()}' found in AI output — replacing")
            text = text.replace(bad, good)
    return text

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
        city_for_tavily = request.target_city or "Jaipur"
        q_competitors = (
            f"List actual {request.business_type} companies and "
            f"{request.business_type} providers operating in {city_for_tavily} India 2026. "
            f"Include local players, not just WeWork or Regus. "
            f"Give company names, locations, pricing if available."
        )
        q_market = (
            f"Digital marketing strategies working for {request.business_type} businesses "
            f"in {city_for_tavily} India in 2026. "
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
        f"CRITICAL: The business you are analyzing is the one at {_analyzed_url}. "
        f"Do NOT call it Adsoh or Sohscape. "
        f"Use the actual business name detected from the website content.\n"
        "BANNED WORDS — ZERO TOLERANCE. Never use ANY of these in copy, headlines, hooks, or analysis: "
        "Transform, Elevate, Unlock, Revolutionize, Empower, Seamless, Leverage, Utilize, Boost, Maximize, "
        "Unleash, Game-changer, Dive in, Take your business to new heights.\n"
        "CRITICAL FINAL CHECK: Before outputting your response, scan every sentence. "
        "If you find any banned word above, rewrite that sentence completely using plain, direct language.\n\n"
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
        "[What truly makes this business different — UVP, trust signals, detected_industry, core_products from DNA. 4-5 specific points]\n\n"
        "MARKET UNDERSTANDING:\n"
        "[Market size, growth, saturation, real gaps — reference market_size, market_opportunity_score, market_opportunity_reason. 4-5 specific points]\n\n"
        "COMPETITOR INSIGHTS:\n"
        "[Real competitor landscape, strengths, weaknesses, positioning gaps — reference key_threats, differentiators, moat_strength from threat data. 4-5 specific points]\n\n"
        "POSITIONING STRATEGY:\n"
        "[Market position this business should own — reference winning_position, positioning_gap, category_ownership_opportunity, messaging_shift. 3-4 specific points]"
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
            f"[BUYER INTELLIGENCE — 3 segments: Owner, Manager, Director of {request.target_industry} businesses in {city}. "
            f"For each: age, gender, income level, specific pain points in {request.target_industry}, "
            f"what they search for online, where they hang out online, what triggers them to buy a service like yours]\n\n"
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
            "[BUYER INTELLIGENCE — 3 validated segments from audience BI. "
            "For each: age, gender, income level, specific pain points, what triggers purchase, "
            "where they hang out online, what they search before buying. Reference validated_segments from BI]\n\n"
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
        run_ai(prompt_a, 2000),
        run_ai(prompt_b, 2000),
        run_ai(prompt_c, 2500),
        run_ai(prompt_guide, 1000),
    )
    section_a = _clean_banned_words(section_a_raw)
    section_b = _clean_banned_words(section_b_raw)
    section_c = _clean_banned_words(section_c_raw)
    ad_guide  = _clean_banned_words(ad_guide_raw)

    # ── Split each grouped output into individual sections ────────────────
    def split_by_headers(text, headers):
        result = {}
        for i, header in enumerate(headers):
            next_header = headers[i + 1] if i + 1 < len(headers) else None
            start_match = re.search(re.escape(header), text, re.I)
            if not start_match:
                result[header] = ""
                continue
            content_start = start_match.end()
            if next_header:
                end_match = re.search(re.escape(next_header), text[content_start:], re.I)
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

    return {
        "success": True,
        "url": request.url,
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

    prompt = (
        f"You are a senior media buyer building a ready-to-paste campaign launch kit.\n"
        f"Business: {biz_label} | City: {city_label} | Total Monthly Budget: Rs {bdgt}\n"
        f"Goal: {request.goal} | Language: {request.language}\n"
        f"CURRENT DATE: {_month_long} — use this for ALL campaign names and date references.\n\n"
        f"MARKETING BRAIN OUTPUT (extract specific details — audience pain points, competitors, positioning — and use them in every asset below):\n"
        f"{sections_summary}\n\n"
        "RULES — READ BEFORE GENERATING:\n"
        "1. ZERO generic copy. Every asset must reference the specific business, industry, or city above.\n"
        "2. Ad Copy formula: Hook (problem/desire specific to this audience) + Body (specific benefit with proof or number) + CTA (one exact action).\n"
        "3. Every CTA must be one of: 'WhatsApp pe FREE AUDIT bhejo', 'Form bharo — free consultation lo', 'Call karo abhi', 'Link mein appointment book karo'.\n"
        "4. Audience targeting: Use EXACT job titles (not 'business owners'), specific interest combinations, behaviors that signal buying intent.\n"
        "5. No markdown bold or bullets. Plain text only. Use --- to separate sub-sections. Use exact rupee amounts.\n"
        "6. BANNED WORDS — ZERO TOLERANCE: Transform, Elevate, Unlock, Revolutionize, Empower, Seamless, Leverage, Utilize, Boost, Maximize, Game-changer, Unleash. If any found, rewrite.\n"
        f"7. Campaign names MUST use current month/year: {_month_short}. NEVER hardcode old dates like Nov2023 or Jun2024.\n\n"
        "=== META ADS LAUNCH KIT ===\n"
        f"Campaign Name: [exact name — format: CityType_Industry_Goal_{_month_short}, e.g. Jaipur_Coaching_Leads_{_month_short}]\n"
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
        "Primary Text: [4-5 lines. Open with a specific result ('Jaipur ke 30+ businesses ne X result paya in 60 days'). No generic claims. Close with exact CTA]\n"
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
        f"Campaign Name: [exact name — format: City_Industry_Search_{_month_short}, e.g. Jaipur_Coaching_Search_{_month_short}]\n"
        "Campaign Type: Search\n"
        f"Daily Budget: Rs {int(google_bdgt / 30)} per day (Rs {google_bdgt}/month — 40% of total)\n"
        "Bid Strategy:\n"
        "  Launch phase (first 2 weeks, before conversion data): Manual CPC or Maximize Clicks\n"
        "  After 30 conversions: Switch to Maximize Conversions or Target CPA\n"
        "  Recommended starting bid: [Rs X-Y per click for this industry — give specific range]\n"
        "  Reason: [one sentence why this strategy fits the goal]\n"
        "---\n"
        "PRIMARY KEYWORD: Before writing anything below, decide ONE primary keyword phrase for "
        f"this business — normally [service/industry] + {city_label}, e.g. 'hospitality marketing jaipur' "
        "or 'hotel booking jaipur'. Reuse this exact phrase (and close variants) in the keyword-match "
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
        "Headline 1: [KEYWORD-MATCH — literally include the PRIMARY KEYWORD phrase + city, e.g. 'Hospitality Marketing Jaipur']\n"
        "Headline 2: [max 30 chars — include city or specific offer]\n"
        "Headline 3: [max 30 chars — CTA or urgency]\n"
        "Description 1: [80-90 chars MANDATORY — specific benefit with number or proof. Last sentence = CTA. Count characters.]\n"
        "Description 2: [80-90 chars MANDATORY — social proof or differentiator. Last sentence = CTA. Count characters.]\n"
        "---\n"
        "AD 2 — PROOF ANGLE (numbers, results, credibility — completely different from Ad 1)\n"
        "Headline 1: [KEYWORD-MATCH VARIANT — different word order/synonym of the PRIMARY KEYWORD + city, e.g. 'Jaipur Hospitality Experts', ALSO include a specific result number]\n"
        "Headline 2: [max 30 chars — who got the result, e.g. 'Jaipur Hotels Trust Us']\n"
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
        "Headline 10: [KEYWORD-MATCH — the PRIMARY KEYWORD phrase + city, plain and direct, e.g. 'Hotel Booking Jaipur Experts']\n"
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
        "FORBIDDEN: Transform, Elevate, Unlock, Revolutionize, Empower, Seamless, Leverage, Utilize, Boost, Maximize, Game-changer, Unleash. "
        "If ANY found — rewrite that sentence completely before returning."
    )

    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
        max_tokens=5500,
    )
    full_text = _clean_banned_words(resp.choices[0].message.content.strip())

    def extract_kit(text, start_marker, end_marker=None):
        start = text.find(start_marker)
        if start == -1:
            return text
        start += len(start_marker)
        if end_marker:
            end = text.find(end_marker, start)
            return text[start:end].strip() if end != -1 else text[start:].strip()
        return text[start:].strip()

    meta_kit        = extract_kit(full_text, "=== META ADS LAUNCH KIT ===",   "=== GOOGLE ADS LAUNCH KIT ===")
    google_kit      = extract_kit(full_text, "=== GOOGLE ADS LAUNCH KIT ===", "=== REMARKETING KIT ===")
    remarketing_kit = extract_kit(full_text, "=== REMARKETING KIT ===",       "=== TRACKING SETUP ===")
    tracking_kit    = extract_kit(full_text, "=== TRACKING SETUP ===",        "=== LANDING PAGE CHECKLIST ===")
    lp_checklist    = extract_kit(full_text, "=== LANDING PAGE CHECKLIST ===")

    # ── Save keywords/headlines/descriptions to campaign_memory ─────────────
    # so /google-ads/create-campaign can pull them back when the user clicks
    # "Push to Google Ads". Must use the SAME key derivation as the lookup.
    campaign_assets = _extract_campaign_kit_assets(google_kit)
    business_key = derive_business_key(request.url, request.industry, request.city)
    logger.info(f"[CAMPAIGN KIT] SAVE key: '{business_key}'")
    save_to_memory("campaign", business_key, {
        "campaign_data": {
            "keywords":     campaign_assets["keywords"],
            "headlines":    campaign_assets["headlines"],
            "descriptions": campaign_assets["descriptions"],
            "sitelinks":    campaign_assets["sitelinks"],
        }
    })

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
        "BANNED WORDS — ZERO TOLERANCE: unleash, elevate, dive in, game-changer, unlock, revolutionize, seamless, empower, transform, leverage, maximize, utilize, boost your.\n"
        "Agar yeh koi bhi word aaye toh sentence dobara likho. Plain aur direct language use karo.\n"
        "LANGUAGE: " + request.language + "\n"
        f"CURRENT DATE: {_current_month_yr}\n\n"
        "BRAND WEBSITE:\n" + site[:1500] + "\n\nPROMOTE: " + request.offer + "\nPLATFORM: " + request.platform + "\nINDUSTRY: " + request.business_type + "\n\n"
        "3 alag ad creative banao — DISTINCT angles (Benefit / Proof / Urgency). Koi asterisk mat use kar.\n\n"
        "CREATIVE 1 — BENEFIT ANGLE (what they get, specific outcome):\nHook Line: []\nPrimary Text: []\nHeadline: []\nCTA Button: []\nImage Concept: []\nText On Image: []\nColor Palette: []\nLayout: []\n\n"
        "CREATIVE 2 — PROOF ANGLE (numbers, results, credibility — no generic claims):\nHook Line: []\nPrimary Text: []\nHeadline: []\nCTA Button: []\nImage Concept: []\nText On Image: []\nColor Palette: []\nLayout: []\n\n"
        "CREATIVE 3 — URGENCY ANGLE (limited time, competitor threat, or seasonal urgency):\nHook Line: []\nPrimary Text: []\nHeadline: []\nCTA Button: []\nImage Concept: []\nText On Image: []\nColor Palette: []\nLayout: []\n\n"
        "CRITICAL FINAL CHECK: Scan every word. If Transform, Elevate, Unlock, Seamless, Empower, Leverage, Boost, Maximize found — rewrite completely."
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
    prompt = f"""You are a senior growth strategist. Analyze this business's stored data and identify the highest-ROI opportunities.

BUSINESS DATA (from Adsoh memory system):
{memory_context}

Your job: decide where this specific business should focus FIRST to get the fastest, highest return.

RULES:
- Be specific to THIS business. Reference actual segments, competitors, and gaps from the data above.
- No generic advice. Every recommendation must cite evidence from the data.
- BANNED WORDS: Elevate, Transform, Unlock, Revolutionize, Empower, Seamless, Game-changer.
- Revenue potential: "High" = can 2x revenue in 3 months, "Medium" = 30-50% lift, "Low" = < 30% lift.
- Priority score 0-100: 90+ = do this week, 70-89 = do this month, below 70 = plan for later.

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

    return {
        "success":      True,
        "memory_used":  True,
        "business_key": norm_key,
        "opportunity":  opportunity,
    }

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
- BANNED WORDS: Elevate, Transform, Unlock, Revolutionize, Empower, Seamless, Game-changer.
- offer_score 0-100: 90+ = near-certain to convert, 70-89 = strong, below 70 = needs tweaking.
- confidence 0-100: how confident you are in this recommendation given the available data.

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

    return {
        "success":      True,
        "memory_used":  True,
        "business_key": norm_key,
        "offer":        offer,
    }


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
    city:          str = "Jaipur"
    target_name:   str = ""
    outreach_goal: str = "get meeting"
    # kept for backward compat but no longer the primary key source
    business_key:  str = ""

@app.post("/outreach-ai")
async def outreach_ai(request: OutreachAIRequest):
    industry = (request.industry or "").strip()
    city     = (request.city     or "Jaipur").strip()
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
- City: {city}
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
- WhatsApp and Instagram must mix Hindi/Hinglish naturally — like a real Jaipur agency owner would write. NOT robotic translation.
- Cold email body must be UNDER 150 words. Count carefully.
- LinkedIn connection_request STRICTLY under 300 characters. Count carefully.
- Instagram opener STRICTLY 3 lines. No more.
- Call script opener must be deliverable in under 10 seconds.
- Every objection response must reference the specific {industry} context.
- BANNED words in all copy: Elevate, Transform, Unlock, Revolutionize, Empower, Seamless, Leverage, Utilize, Game-changer, Dive in.
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
    city:     str = "Jaipur"
    budget:   float = 0.0
    goal:     str = "Lead Generation"

@app.post("/kpi-engine")
async def kpi_engine(request: KPIEngineRequest):
    industry = (request.industry or "").strip()
    city     = (request.city     or "Jaipur").strip()
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
- City: {city}
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

    prompt = f"""You are a performance marketing expert specialising in Indian digital advertising for {industry} businesses in {city}.
Generate precise, data-driven KPI predictions for this campaign. All numbers must reflect real Indian market benchmarks for this specific industry and city.

{context}

Return ONLY a valid JSON object (no markdown, no text outside JSON) with this EXACT schema:
{{
  "primary_kpi": {{
    "metric": "CPL or CPA or ROAS or CTR — pick the ONE most important metric for {goal}",
    "target": "specific target value with unit (e.g. ₹350 CPL, 4.2x ROAS)",
    "why": "one sentence why this is the north-star metric for {goal} in {industry}"
  }},
  "predicted_metrics": {{
    "ctr": {{"value": "X.X%", "range": "X%-X%", "benchmark": "industry avg for {industry} on Google/Meta"}},
    "cpc": {{"value": "₹XX", "range": "₹X-₹X", "benchmark": "avg CPC for {industry} in {city}"}},
    "cpl": {{"value": "₹XXX", "range": "₹X-₹X", "benchmark": "avg CPL for {industry} in {city}"}},
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
- BANNED words: Elevate, Transform, Unlock, Revolutionize, Empower, Seamless.
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

    return {
        "success":      True,
        "memory_used":  True,
        "business_key": norm_key,
        "kpi":          kpi,
    }


# ── Module 20: Performance Intelligence ──────────────────────────────────────

class PerformanceIntelligenceRequest(BaseModel):
    url:        str = ""
    industry:   str = ""
    city:       str = "Jaipur"
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
        city       = (request.city     or "Jaipur").strip()
        date_range = (request.date_range or "30d").strip()
        days       = _parse_days(date_range)
        _bk_src    = (request.url or "").strip()

        logger.info(f"[PERF-INTEL] url={request.url!r} industry={industry!r} city={city!r} date_range={date_range!r} days={days}")

        # ── Memory + Google Ads in parallel ──────────────────────────────────
        memory, norm_key = get_memory_with_city_fallback(_bk_src, industry, city)
        logger.info(f"[PERF-INTEL] key={norm_key!r} memory_tables={list(memory.keys())}")

        perf_data, campaign_rows = await asyncio.gather(
            asyncio.to_thread(_fetch_gads_performance, days),
            asyncio.to_thread(_fetch_gads_campaigns,  days),
        )
        logger.info(f"[PERF-INTEL] gads_perf={perf_data} campaigns={len(campaign_rows)}")

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

        zero_note = (
            "NOTE: Google Ads shows zero spend and zero impressions — account has no active campaigns or date range returned no data."
            if zero_spend else ""
        )

        prompt = (
            "You are a senior Google Ads performance analyst for an Indian digital marketing agency.\n"
            "Analyse the following campaign performance data and return a JSON report.\n\n"
            "BUSINESS: " + _sv(_bm, "business_name") + " | Industry: " + industry + " | City: " + city + "\n"
            "ANALYSIS PERIOD: Last " + str(days) + " days (" + str(perf_data.get("start_date", "N/A")) + " to " + str(perf_data.get("end_date", "N/A")) + ")\n"
            "GOOGLE ADS CONNECTED: " + str(google_ads_connected) + "\n\n"
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
            '    {"metric":"CTR","expected":"' + _exp("ctr") + '","actual":"' + str(ctr) + '%","status":"below/on_track/above","gap":"numeric gap with sign","action":"specific action"},\n'
            '    {"metric":"CPC","expected":"' + _exp("cpc") + '","actual":"RS' + str(cpc) + '","status":"below/on_track/above","gap":"numeric gap","action":"specific action"},\n'
            '    {"metric":"Conversions","expected":"' + _exp("conversions") + '","actual":"' + str(conv) + '","status":"below/on_track/above","gap":"numeric gap","action":"specific action"},\n'
            '    {"metric":"ROAS","expected":"' + _exp("roas") + '","actual":"' + str(roas) + 'x","status":"below/on_track/above","gap":"numeric gap","action":"specific action"},\n'
            '    {"metric":"Cost","expected":"N/A","actual":"RS' + str(cost) + '","status":"on_track","gap":"0","action":"budget pacing assessment"}\n'
            '  ],\n'
            '  "campaign_breakdown": ' + _camp_json + ',\n'
            '  "top_insight": "single most important thing happening now — specific, data-backed",\n'
            '  "biggest_problem": "single most urgent issue to fix — specific, data-backed, no fluff",\n'
            '  "quick_wins": ["action 1 with expected impact","action 2","action 3"],\n'
            '  "ai_analysis": "2-3 paragraphs on overall performance, what is working, what needs fixing. Specific to the numbers above.",\n'
            '  "overall_health": 0,\n'
            '  "trend": "improving/stable/declining"\n'
            '}\n\n'
            "Rules:\n"
            "- Replace overall_health with an integer 0-100 based on CTR, ROAS, conversions relative to benchmarks.\n"
            "- Fill campaign_breakdown performance_rating: good (CTR>2% or conv>0), average (CTR 1-2%), poor (CTR<1% and conv=0).\n"
            "- If zero spend: overall_health=0, trend=stable, top_insight='No active campaigns found — launch a campaign to see performance data'.\n"
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

        save_to_memory("performance", norm_key, {
            "performance_data": performance,
            "date_range":       date_range,
            "overall_health":   float(performance.get("overall_health", 0) or 0),
        })
        logger.info(f"[PERF-INTEL] Done: key={norm_key!r} health={performance.get('overall_health')} connected={google_ads_connected}")

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
    city:     str = "Jaipur"

@app.post("/ai-optimizer")
async def ai_optimizer(request: AIOptimizerRequest):
    try:
        industry = (request.industry or "").strip()
        city     = (request.city     or "Jaipur").strip()
        _bk_src  = (request.url     or "").strip()

        logger.info(f"[AI-OPT] url={request.url!r} industry={industry!r} city={city!r}")

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

        def _jstr(obj):
            try: return json.dumps(obj, ensure_ascii=False)[:400]
            except: return str(obj)[:400]

        context = (
            "BUSINESS: " + _sv(business_data, "business_name") +
            " | Industry: " + (industry or _sv(business_data, "industry")) +
            " | City: " + city + "\n\n"

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

        prompt = (
            "You are a senior Google Ads + Meta Ads optimization strategist for an Indian digital marketing agency.\n"
            "Based on the data below, generate a detailed, actionable optimization plan.\n\n"
            + context + "\n"
            + missing_note + "\n"
            "Return ONLY valid JSON (no markdown, no text outside JSON) matching this EXACT schema:\n"
            "{\n"
            '  "overall_verdict": "campaigns need urgent attention / on track / performing well",\n'
            '  "health_change": "improving / stable / declining",\n'
            '  "pause_recommendations": [\n'
            '    {"what":"campaign or ad type to pause","why":"specific data reason","expected_saving":"RS X/month","urgency":"immediate/this week/monitor"}\n'
            '  ],\n'
            '  "scale_recommendations": [\n'
            '    {"what":"what to scale","why":"specific data reason","how_much":"increase budget by X%","expected_impact":"..."}\n'
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
            '  "confidence": 75\n'
            '}\n\n'
            "Rules:\n"
            "- Every recommendation must cite SPECIFIC numbers from the data above, not generic advice.\n"
            "- Use RS as prefix for Indian Rupees (e.g. RS 5,000).\n"
            "- this_week_actions must be ordered by impact (highest first, priority 1 = most impactful).\n"
            "- If no active campaigns: focus pause_recommendations on pre-launch setup; scale on budget allocation plan.\n"
            "- Minimum 2 items in pause, scale, audience, creative, keyword lists.\n"
            "- Minimum 5 this_week_actions.\n"
            "- BANNED words: Elevate, Transform, Unlock, Revolutionize, Empower, Seamless.\n"
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

        save_to_memory("optimizer", norm_key, {"optimizer_data": optimizer})
        logger.info(f"[AI-OPT] Done: key={norm_key!r} confidence={optimizer.get('confidence')}")

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


# ── Module 22: Result Center ──────────────────────────────────────────────────

class ResultCenterRequest(BaseModel):
    url:      str = ""
    industry: str = ""
    city:     str = "Jaipur"

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

@app.post("/result-center")
async def result_center(request: ResultCenterRequest):
    try:
        industry = (request.industry or "").strip()
        city     = (request.city     or "Jaipur").strip()
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
            " | City: " + city + "\n\n"

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
    city:           str  = "Jaipur"
    url:            str  = ""
    max_prospects:  int  = 15


@app.post("/prospect-discovery")
async def prospect_discovery(request: ProspectDiscoveryRequest):
    try:
        industry       = (request.industry or "").strip()
        city           = (request.city or "Jaipur").strip()
        max_prospects  = max(5, min(request.max_prospects, 20))

        search_terms   = _get_search_terms(industry)
        logger.info(f"[PROSPECT] industry={industry!r} city={city!r} terms={search_terms!r}")

        # 1. Text-search Google Places
        raw_places = await fetch_google_places(search_terms, city, max_results=20)
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
                fetch_tavily(f"{p['name']} {city} Instagram Facebook social media")
                for p in enriched[:6]
            ]
            tasks_ads = [
                fetch_tavily(f"{p['name']} {city} ads marketing campaigns")
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
            "You are a B2B prospect scoring expert for a digital marketing agency in " + city + ".\n"
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
            '  "city": "' + city + '",\n'
            '  "industry": "' + industry + '",\n'
            '  "search_query_used": "' + search_terms + ' in ' + city + '",\n'
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
            '      "suggested_opening_line": "Hi [Name], I noticed [specific observation about their business] — I help [industry] businesses in ' + city + ' get more customers through [service]. Would love to show you what we did for similar businesses here."\n'
            "    }\n"
            "  ]\n"
            "}\n"
            "Return ONLY valid JSON. Score all " + str(len(enriched)) + " businesses. Rank by opportunity_score descending."
        )

        logger.info(f"[PROSPECT] Calling GPT-4o with {len(enriched)} businesses")
        raw_resp = await asyncio.to_thread(
            lambda: client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=3500,
                temperature=0.3,
                response_format={"type": "json_object"},
            ).choices[0].message.content
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


def _create_ad_sync(ad_group_rn: str, headlines: list, descriptions: list,
                    final_url: str, customer_id: str):
    """Synchronous: create a ResponsiveSearchAd in an ad group."""
    client  = get_google_ads_client()
    svc     = client.get_service("AdGroupAdService")
    op      = client.get_type("AdGroupAdOperation")
    aga     = op.create
    aga.ad_group = ad_group_rn
    aga.status   = client.enums.AdGroupAdStatusEnum.ENABLED

    rsa = aga.ad.responsive_search_ad
    for h in headlines[:15]:
        asset = client.get_type("AdTextAsset")
        asset.text = str(h)[:30]
        rsa.headlines.append(asset)
    for d in descriptions[:4]:
        asset = client.get_type("AdTextAsset")
        asset.text = str(d)[:90]
        rsa.descriptions.append(asset)
    aga.ad.final_urls.append(final_url)

    resp   = svc.mutate_ad_group_ads(customer_id=customer_id, operations=[op])
    ad_rn  = resp.results[0].resource_name
    return {"ad_id": ad_rn.split("/")[-1], "resource_name": ad_rn, "status": "ENABLED"}


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

        keywords_added  = []
        ad_created      = None
        sitelinks_added = []
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
            if len(headlines) >= 3 and len(descriptions) >= 2 and final_url:
                try:
                    ad_created = await asyncio.to_thread(
                        _create_ad_sync, result["ad_group_rn"], headlines, descriptions, final_url, customer_id
                    )
                    logger.info(f"[GADS-CREATE] Ad created: {ad_created}")
                except Exception as _ae:
                    logger.warning(f"[GADS-CREATE] Ad creation failed: {_ae}")
            else:
                logger.info(
                    f"[GADS-CREATE] Skipping ad creation — headlines={len(headlines)} "
                    f"descriptions={len(descriptions)} final_url={final_url!r}"
                )

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

        result["keywords_added"] = len(keywords_added)
        result["ad_created"]     = bool(ad_created)
        result["ad"]             = ad_created
        result["sitelinks_added"] = len(sitelinks_added)
        result["location_target"] = {
            "resource_name": location_resource_name,
            "matched_name":  location_matched_name,
            "applied":       location_applied,
        }
        result["google_ads_dashboard"] = f"https://ads.google.com/aw/campaigns?campaignId={result['campaign_id']}"
        logger.info(f"[GADS-CREATE] Done: campaign_id={result['campaign_id']}")
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
        errors = [e.message for e in ex.failure.errors]
        logger.error(f"[GADS-AD] API error: {errors}")
        return {"success": False, "error": "; ".join(errors)}
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
    campaign_name: str
    objective:     str   = "OUTCOME_LEADS"
    daily_budget:  float                   # in ₹
    business_key:  str   = ""              # if set, pull ad copy/city from campaign_memory


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
    Create a Campaign + AdSet + AdCreative + Ad, all PAUSED by default —
    same safe pattern as Google Ads: nothing goes live until a human
    reviews and enables it in Ads Manager.
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

    page_id = _genv("META_PAGE_ID")
    if not page_id:
        return {
            "success": False,
            "error": "META_PAGE_ID not configured — ad creatives require a Facebook "
                     "Page connected to this ad account. Add META_PAGE_ID to env vars.",
        }

    # ── Pull ad copy / city / landing URL from campaign_memory ──────────────
    headline      = "Grow Your Business Today"
    primary_text  = "Reach more customers with a campaign built for results."
    description   = ""
    city          = ""
    final_url     = ""
    final_url_is_placeholder = False

    if request.business_key:
        mem = get_memory(request.business_key)
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
        city = biz.get("city", "") or ""

        website = mem.get("website", {}) if mem else {}
        final_url = (website.get("url") or "").strip()

        if not final_url:
            # business_key is derived as "domain.com::industry::city" for
            # URL-mode businesses — reuse the domain segment as a last resort.
            domain = request.business_key.strip().split("::")[0].strip()
            if domain and "." in domain and " " not in domain:
                final_url = domain if domain.startswith(("http://", "https://")) else f"https://{domain}"

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
                Campaign.Field.name:                 request.campaign_name,
                Campaign.Field.objective:             request.objective,
                Campaign.Field.status:                Campaign.Status.paused,
                Campaign.Field.special_ad_categories: [Campaign.SpecialAdCategory.none],
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
                AdSet.Field.name:              f"{request.campaign_name} — Ad Set 1",
                AdSet.Field.campaign_id:       campaign_id,
                AdSet.Field.daily_budget:      int(round(request.daily_budget * 100)),  # ₹ → paise
                AdSet.Field.billing_event:     AdSet.BillingEvent.impressions,
                AdSet.Field.optimization_goal: optimization_goal,
                AdSet.Field.targeting:         targeting,
                AdSet.Field.status:            AdSet.Status.paused,
            })

        adset = await asyncio.to_thread(_create_adset)
        adset_id = adset.get(AdSet.Field.id)
        created["adset_id"] = adset_id
        logger.info(f"[META ADS CREATE] AdSet created: {adset_id}")

        def _create_creative():
            account = AdAccount(account_id)
            return account.create_ad_creative(params={
                AdCreative.Field.name: f"{request.campaign_name} — Creative",
                AdCreative.Field.object_story_spec: {
                    "page_id": page_id,
                    "link_data": {
                        "message":     primary_text,
                        "link":        final_url,
                        "name":        headline,
                        "description": description,
                        "call_to_action": {"type": "LEARN_MORE", "value": {"link": final_url}},
                    },
                },
            })

        creative = await asyncio.to_thread(_create_creative)
        creative_id = creative.get(AdCreative.Field.id)
        created["creative_id"] = creative_id
        logger.info(f"[META ADS CREATE] AdCreative created: {creative_id}")

        def _create_ad():
            account = AdAccount(account_id)
            return account.create_ad(params={
                Ad.Field.name:     f"{request.campaign_name} — Ad 1",
                Ad.Field.adset_id: adset_id,
                Ad.Field.creative: {"creative_id": creative_id},
                Ad.Field.status:   Ad.Status.paused,
            })

        ad = await asyncio.to_thread(_create_ad)
        ad_id = ad.get(Ad.Field.id)
        created["ad_id"] = ad_id
        logger.info(f"[META ADS CREATE] Ad created: {ad_id}")

        return {
            "success":                 True,
            "campaign_id":             campaign_id,
            "adset_id":                adset_id,
            "creative_id":             creative_id,
            "ad_id":                   ad_id,
            "status":                  "PAUSED",
            "targeting_used":          targeting,
            "final_url":               final_url,
            "final_url_is_placeholder": final_url_is_placeholder,
            "meta_ads_manager_link":   f"https://business.facebook.com/adsmanager/manage/campaigns?act={account_id.replace('act_', '')}",
        }

    except FacebookRequestError as ex:
        details = _log_meta_error("create-campaign", ex)
        return {"success": False, "partial": created, **details}
    except Exception as ex:
        tb = _traceback.format_exc()
        logger.error(f"[META ADS] create-campaign unexpected error: {type(ex).__name__}: {ex}\n{tb}")
        return {"success": False, "partial": created, "error": f"{type(ex).__name__}: {ex}"}


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
class CricketAdsRequest(BaseModel):
    url:           str
    whatsapp_link: str = ""
    city:          str = "India"


# ── Endpoint ─────────────────────────────────────────────────────────────────
@app.post("/cricket-ads-intelligence")
async def cricket_ads_intelligence(request: CricketAdsRequest):
    """
    Standalone Cricket Community Ads intelligence module.
    Crawls the landing page, fetches live cricket context via Tavily,
    then calls GPT-4o once to produce a full campaign intelligence report.
    """
    biz_key = f"cricket::{request.url.strip().rstrip('/').lower()}::{request.city.lower()}"

    # ── 1. Crawl landing page ────────────────────────────────────────────────
    site_content = ""
    if FIRECRAWL_API_KEY and request.url:
        site_content = await fetch_firecrawl(request.url)
    if not site_content:
        # Lightweight httpx fallback
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

    # ── 2. Tavily: live cricket season context ───────────────────────────────
    cricket_context = ""
    if TAVILY_API_KEY:
        _q = (
            f"Current cricket season India {datetime.now().strftime('%B %Y')}: "
            "IPL matches, international series, World Cup schedule, "
            "cricket fan engagement trends, WhatsApp cricket community growth"
        )
        cricket_context = await fetch_tavily(_q)

    # ── 3. Build prompt ──────────────────────────────────────────────────────
    _current_month = datetime.now().strftime("%B %Y")
    prompt = (
        f"You are a Google Display Ads expert specializing in sports community campaigns in India.\n"
        f"Current date: {_current_month}\n"
        f"City/Region: {request.city}\n"
        f"WhatsApp Group Link: {request.whatsapp_link or '(not provided)'}\n\n"
        f"WEBSITE CONTENT:\n{site_content[:2000]}\n\n"
        f"LIVE CRICKET CONTEXT (use this to make audience and timing recommendations specific):\n"
        f"{cricket_context[:1500] if cricket_context else 'No live data — use general cricket season knowledge for India.'}\n\n"
        "TASK: Generate a complete Google Display Ads campaign intelligence report for this cricket community/news WhatsApp group.\n\n"
        "CONTENT RULES:\n"
        "- This is a cricket NEWS and COMMUNITY group — no gambling, no betting, no fantasy earnings, no real-money gaming.\n"
        "- Primary conversion: WhatsApp group join via link click.\n"
        "- Run a compliance_check but expect it to be clean for a pure community/news group.\n"
        "- If compliance_check finds any gambling/betting/money signals in the website content, set risk_level to 'high' and list flags.\n\n"
        "CHARACTER LIMITS — STRICT:\n"
        "- headlines_15: each MUST be under 30 characters (Google Ads limit). Count carefully.\n"
        "- long_headlines_5: each MUST be BETWEEN 70 and 90 characters — count carefully, not just under 90. "
        "A 40-character headline is too short and does not satisfy this field.\n"
        "- descriptions_5: each MUST be BETWEEN 70 and 90 characters and end with a CTA — count carefully, not "
        "just under 90. A 50-character description is too short and does not satisfy this field.\n\n"
        "REQUIRED FIELDS — DO NOT SKIP:\n"
        "- creative_assets.long_headlines_5 and creative_assets.descriptions_5 are REQUIRED. You MUST populate "
        "long_headlines_5 with exactly 5 items (each 70-90 characters) and descriptions_5 with exactly 5 items "
        "(each 70-90 characters). NEVER leave these as empty arrays — an empty array is a failed response.\n"
        "- Keep audience_segments[].reason and placement_recommendations[].why to one concise sentence each — "
        "this budget discipline exists so every creative_assets field below can be fully completed.\n\n"
        "AUDIENCE RULES:\n"
        "- You MUST return AT LEAST 6 distinct audience_segments. Do not return fewer than 6. Each must target a "
        "genuinely different group (e.g. by age, fan type, device habit, language, city-tier, viewing occasion) — "
        "not 6 rewordings of the same segment.\n"
        "- You MUST return AT LEAST 5 distinct placement_recommendations, each a different real Google Display "
        "placement type or property (e.g. specific news/sports apps, YouTube cricket channels, cricket score "
        "widgets, mobile game placements, news aggregator apps) — not 5 rewordings of the same placement.\n"
        "- Read the LIVE CRICKET CONTEXT above and pull out the ACTUAL current tournament name, series name, team "
        "names, or match dates mentioned in it. Every audience_segments[].reason MUST name that specific event or "
        "teams (e.g. 'Timed around India vs Australia T20I series' or 'Asia Cup 2026 knockout stage'). "
        "NEVER write generic filler like 'cricket season is ongoing' or 'cricket is popular in India' — if the "
        "live context names a tournament/match, use its actual name. If LIVE CRICKET CONTEXT is empty, say so "
        "explicitly in one segment's reason rather than inventing a generic sentence.\n"
        "- estimated_cpc and estimated_ctr must be realistic for Google Display in India (CPC typically ₹1-8).\n"
        "- priority_score: 0-100 integer ranking this audience segment.\n\n"
        "CREATIVE RULES — descriptions_5:\n"
        "- Write EXACTLY 5 descriptions_5, one per CTA angle, in this exact order and each clearly using that "
        "angle (do not reuse the same CTA wording across them):\n"
        "  1. Urgency (e.g. limited spots/time-boxed to a live match or series)\n"
        "  2. Benefit (concrete value the user gets by joining)\n"
        "  3. Social proof (community size, activity level, numbers)\n"
        "  4. Curiosity (tease what's inside without giving it away)\n"
        "  5. Direct action (short, imperative, no hype)\n"
        "- The word 'join' (any form, case-insensitive) may appear in AT MOST ONE of the five descriptions_5 — "
        "not zero, not two, not five. Vary the actual verb across the other 4 (e.g. mix verbs like 'Get', 'See', "
        "'Discover', 'Tap in', 'Follow along', 'Unlock', 'Don't miss', 'Catch every ball', 'Be part of' — pick "
        "different ones, this list is illustrative not exhaustive).\n"
        "- SELF-CHECK before finalizing: count how many of your 5 descriptions_5 contain the word 'join' in any "
        "form. If the count is 0 or 2+, rewrite descriptions_5 so exactly 1 contains it. Do this check silently "
        "and only output the final corrected JSON.\n\n"
        "LANDING PAGE AUDIT RULES:\n"
        "- landing_page_audit.issues and .fixes MUST quote or closely paraphrase specific text/headlines/elements "
        "that actually appear in WEBSITE CONTENT above, and explain the specific problem with that specific text "
        "(e.g. \"Headline reads '...' but never mentions WhatsApp or a community, so a visitor from this ad won't "
        "know what they're joining\"). A generic issue with no quoted/paraphrased fragment from WEBSITE CONTENT "
        "is NOT acceptable — NEVER write generic statements like 'looks good', 'no clear CTA', or 'lacks trust "
        "signals' without tying them to something specific you actually read in WEBSITE CONTENT.\n"
        "- If WEBSITE CONTENT above is '(Could not fetch website content)' or empty, say exactly that as the "
        "issue instead of inventing praise or generic advice.\n\n"
        "Return ONLY a valid JSON object with this exact structure (no markdown, no explanation):\n"
        "{\n"
        '  "business_summary": { "offer": "...", "primary_conversion": "WhatsApp Join", "target_user": "..." },\n'
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
        '  "creative_assets": {\n'
        '    "headlines_15": ["...", "...", "...", "...", "...", "...", "...", "...", "...", "...", "...", "...", "...", "...", "..."],\n'
        '    "long_headlines_5": ["...", "...", "...", "...", "..."],\n'
        '    "descriptions_5": ["...", "...", "...", "...", "..."],\n'
        '    "cta": "Join Now",\n'
        '    "image_suggestions": ["...", "...", "..."]\n'
        '  },\n'
        '  "audience_segments": [\n'
        '    { "name": "...", "intent": "high", "estimated_cpc": "₹2-4", "estimated_ctr": "0.5%", "priority_score": 85, "reason": "..." },\n'
        '    { "name": "...", "intent": "...", "estimated_cpc": "...", "estimated_ctr": "...", "priority_score": 0, "reason": "..." },\n'
        '    { "name": "...", "intent": "...", "estimated_cpc": "...", "estimated_ctr": "...", "priority_score": 0, "reason": "..." },\n'
        '    { "name": "...", "intent": "...", "estimated_cpc": "...", "estimated_ctr": "...", "priority_score": 0, "reason": "..." },\n'
        '    { "name": "...", "intent": "...", "estimated_cpc": "...", "estimated_ctr": "...", "priority_score": 0, "reason": "..." },\n'
        '    { "name": "...", "intent": "...", "estimated_cpc": "...", "estimated_ctr": "...", "priority_score": 0, "reason": "..." }\n'
        '  ],\n'
        '  "placement_recommendations": [\n'
        '    { "placement": "...", "why": "...", "estimated_reach": "...", "priority": "high" },\n'
        '    { "placement": "...", "why": "...", "estimated_reach": "...", "priority": "..." },\n'
        '    { "placement": "...", "why": "...", "estimated_reach": "...", "priority": "..." },\n'
        '    { "placement": "...", "why": "...", "estimated_reach": "...", "priority": "..." },\n'
        '    { "placement": "...", "why": "...", "estimated_reach": "...", "priority": "..." }\n'
        '  ],\n'
        '  "landing_page_audit": {\n'
        '    "score": 0,\n'
        '    "issues": [],\n'
        '    "fixes": []\n'
        '  },\n'
        '  "launch_score": { "overall": 0, "audience": 0, "compliance": 0, "creative": 0 }\n'
        "}"
    )

    # ── 4. GPT-4o call ───────────────────────────────────────────────────────
    try:
        def _call_gpt():
            return client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.4,
                max_tokens=4096,
            )
        gpt_resp = await asyncio.to_thread(_call_gpt)
        raw_json = gpt_resp.choices[0].message.content
        result   = json.loads(raw_json)
        logger.info(
            f"[CRICKET] finish_reason={gpt_resp.choices[0].finish_reason!r} "
            f"completion_tokens={gpt_resp.usage.completion_tokens}"
        )
    except Exception as _e:
        logger.error(f"[CRICKET] GPT error: {_e}")
        return {"success": False, "error": str(_e)}

    # ── 4a. Backfill pass: long_headlines_5 / descriptions_5 must never be empty ──
    # These sit right after headlines_15 in creative_assets — if the model runs low
    # on budget generating the (now larger) audience/placement arrays first, they can
    # come back as empty arrays even though the JSON as a whole is valid. Detect that
    # and do one targeted fill-in call rather than silently handing the frontend gaps.
    try:
        ca = result.setdefault("creative_assets", {})
        missing = {
            k: n for k, n in (("long_headlines_5", 5), ("descriptions_5", 5))
            if len(ca.get(k) or []) < n
        }
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
                                    "Write exactly 5 descriptions_5, each 70-90 characters, ending with a CTA. "
                                    "Use 5 different CTA angles (urgency, benefit, social proof, curiosity, direct "
                                    "action) and let 'join' (any form) appear in at most 1 of the 5.\n"
                                )
                                for k in missing
                            )
                            + "Return ONLY a JSON object with exactly these keys: "
                            + json.dumps({k: ["...", "...", "...", "...", "..."] for k in missing})
                        ),
                    }],
                    response_format={"type": "json_object"},
                    temperature=0.4,
                    max_tokens=800,
                )
            fill_resp = await asyncio.to_thread(_call_backfill)
            filled = json.loads(fill_resp.choices[0].message.content)
            for k in missing:
                if isinstance(filled.get(k), list) and len(filled[k]) == 5:
                    ca[k] = filled[k]
            result["creative_assets"] = ca
    except Exception as _be:
        logger.warning(f"[CRICKET] creative_assets backfill skipped: {_be}")

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
        for field, extra_rule in (
            ("long_headlines_5", ""),
            ("descriptions_5", " Keep the word 'join' (any form) in at most 1 of the 5, preserving each item's "
                                "existing CTA angle (urgency, benefit, social proof, curiosity, direct action)."),
        ):
            items = ca.get(field) or []
            if len(items) == 5 and _out_of_range(items):
                targets = [random.randint(74, 88) for _ in items]
                target_lines = "\n".join(
                    f'{i + 1}. "{item}" -> target EXACTLY {t} characters'
                    for i, (item, t) in enumerate(zip(items, targets))
                )

                def _call_length_fix(field=field, target_lines=target_lines, extra_rule=extra_rule):
                    return client.chat.completions.create(
                        model="gpt-4o",
                        messages=[{
                            "role": "user",
                            "content": (
                                f"Rewrite each of these 5 Google Ads {field.replace('_', ' ')} to hit an EXACT "
                                "target character count by adding concrete specific detail (not filler words) — "
                                "expand with real specifics like match/series name, community size, or benefit, "
                                "not padding. Count characters as you write, including spaces and punctuation."
                                f"{extra_rule}\n\n{target_lines}\n\n"
                                f'Return ONLY a JSON object: {{"{field}": ["...", "...", "...", "...", "..."]}}'
                            ),
                        }],
                        response_format={"type": "json_object"},
                        temperature=0.4,
                        max_tokens=600,
                    )
                len_resp = await asyncio.to_thread(_call_length_fix)
                fixed = json.loads(len_resp.choices[0].message.content).get(field)
                if isinstance(fixed, list) and len(fixed) == 5:
                    ca[field] = fixed
                    logger.info(f"[CRICKET] Length-enforced {field} via per-item targets")

            # Deterministic final guarantee: pad/trim anything still out of range.
            items = ca.get(field) or []
            if len(items) == 5 and _out_of_range(items):
                ca[field] = [_pad_to_range(x) for x in items]
                logger.info(f"[CRICKET] Locally padded/trimmed {field} to guarantee 70-90 chars")
        result["creative_assets"] = ca
    except Exception as _le:
        logger.warning(f"[CRICKET] length-enforcement skipped: {_le}")

    # ── 4c. Repair pass: enforce "join" appears in at most 1 of 5 descriptions ──
    # Runs LAST (after backfill and length-enforcement, both of which can touch
    # descriptions_5 wording) so it has the final word on the join-count constraint.
    try:
        descs = (result.get("creative_assets") or {}).get("descriptions_5") or []
        join_count = sum(1 for d in descs if re.search(r"\bjoin\w*\b", d, re.I))
        if len(descs) == 5 and join_count != 1:
            def _call_fix():
                return client.chat.completions.create(
                    model="gpt-4o",
                    messages=[{
                        "role": "user",
                        "content": (
                            "Rewrite these 5 Google Ads descriptions so the word 'join' (any form) appears in "
                            "EXACTLY ONE of them, not zero, not more than one. Keep each description's original "
                            "CTA angle and meaning, just change the wording/verb where needed so they don't all "
                            "lean on 'join'. Each MUST stay between 70 and 90 characters — count carefully. "
                            "Return ONLY a JSON object: "
                            '{"descriptions_5": ["...", "...", "...", "...", "..."]}\n\n'
                            f"Current descriptions:\n{json.dumps(descs)}"
                        ),
                    }],
                    response_format={"type": "json_object"},
                    temperature=0.4,
                    max_tokens=500,
                )
            fix_resp = await asyncio.to_thread(_call_fix)
            fixed = json.loads(fix_resp.choices[0].message.content).get("descriptions_5")
            if isinstance(fixed, list) and len(fixed) == 5:
                if _out_of_range(fixed):
                    fixed = [_pad_to_range(x) for x in fixed]
                result["creative_assets"]["descriptions_5"] = fixed
                logger.info("[CRICKET] Repaired descriptions_5 CTA-verb repetition")
    except Exception as _fe:
        logger.warning(f"[CRICKET] descriptions_5 repair skipped: {_fe}")

    # ── 5. Save to isolated cricket_ads_memory ───────────────────────────────
    try:
        await asyncio.to_thread(save_cricket_memory, biz_key, result)
    except Exception as _se:
        logger.warning(f"[CRICKET] Memory save error: {_se}")

    logger.info(f"[CRICKET] Done for url={request.url!r} city={request.city!r}")
    return {"success": True, "data": result}


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
    return {
        "success":              True,
        "campaign_id":          campaign_id,
        "ad_group_id":          result["ad_group_id"],
        "status":               "PAUSED",
        "google_ads_dashboard": f"https://ads.google.com/aw/campaigns?campaignId={campaign_id}",
        "note":                 "Campaign created PAUSED. Add image assets in Google Ads dashboard before enabling.",
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

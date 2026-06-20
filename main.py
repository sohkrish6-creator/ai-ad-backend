from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import OpenAI
import httpx
from datetime import datetime, date, timedelta
from typing import Optional
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from dotenv import load_dotenv
import os
import logging

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
DATABASE_URL = "sqlite:///./ai_ad_manager.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
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

Base.metadata.create_all(bind=engine)

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
    return {"message": "AI Ad Manager Backend chal raha hai!"}

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
        model="gpt-4o-mini",
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

    ai_response = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}], max_tokens=4000)
    result = ai_response.choices[0].message.content

    analysis = AnalysisModel(url=request.url, business_type=detected, budget=request.budget, goal=request.goal, result=result, created_at=datetime.now().strftime("%d %b %Y, %I:%M %p"))
    db.add(analysis)
    db.commit()

    return {"success": True, "url": request.url, "detected_category": detected, "confidence": confidence, "analysis": result}

class CompetitorRequest(BaseModel):
    my_url: str
    competitor_urls: list[str]
    business_type: str

@app.post("/competitor")
async def competitor(request: CompetitorRequest):
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

    ai_response = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}], max_tokens=2500)
    return {"success": True, "analysis": ai_response.choices[0].message.content}

class AdIntelRequest(BaseModel):
    business_name: str
    business_type: str
    website: str = ""
    country: str = "IN"

@app.post("/ad-intelligence")
async def ad_intelligence(request: AdIntelRequest):
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

    ai_response = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}], max_tokens=1500)
    return {"success": True, "business_name": request.business_name, "meta_ad_library_link": meta_link, "google_ads_link": google_link, "guide": ai_response.choices[0].message.content}

class FullReportRequest(BaseModel):
    url: str
    business_type: str
    budget: int
    goal: str
    competitor_name: str = ""
    competitor_website: str = ""
    language: str = "Hinglish"

@app.post("/full-report")
async def full_report(request: FullReportRequest, db: Session = Depends(get_db)):
    import re, urllib.parse, asyncio

    def extract_clean(html):
        parts = []
        t = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        if t: parts.append("TITLE: " + t.group(1).strip())
        m = re.search(r'<meta[^>]*name=["\']description["\'][^>]*content=["\'](.*?)["\']', html, re.I)
        if m: parts.append("DESCRIPTION: " + m.group(1).strip())
        for tag in ["h1", "h2", "h3"]:
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

    async def run_ai(prompt, max_tokens):
        resp = await asyncio.to_thread(
            lambda: client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens
            )
        )
        return resp.choices[0].message.content

    my_content = await fetch(request.url)
    if not my_content or len(my_content) < 100:
        return {"success": False, "scan_failed": True, "message": "Website scan nahi ho payi."}

    comp_content = ""
    if request.competitor_website:
        comp_content = await fetch(request.competitor_website)

    strategy_prompt = (
        "Tu ek senior digital marketing strategist hai jo Google Ads aur Meta Ads dono ka expert hai.\n"
        "HUMAN WRITING: AI buzzwords (unleash, elevate, game-changer, unlock, dive in) mat use kar. Chhote punchy lines likho.\n"
        "LANGUAGE: " + request.language + "\n\n"
        "URL: " + request.url + "\nBusiness: " + request.business_type + "\nBudget: Rs " + str(request.budget) + "\nGoal: " + request.goal + "\nWebsite:\n" + my_content[:2000] + "\n\n"
        "Koi asterisk mat use kar. Seedha likho:\n\n"
        "BUSINESS SUMMARY:\n[2 lines — website evidence pe based]\n\n"
        "TARGET AUDIENCE:\n[2 lines — specific, generic nahi]\n\n"
        "BUDGET SPLIT:\n"
        "1. Google Search Ads: Rs [amt] ([%]) - [reason]\n"
        "2. Meta Ads (FB+IG): Rs [amt] ([%]) - [reason]\n"
        "3. [Ek aur relevant platform]: Rs [amt] ([%]) - [reason]\n\n"
        "GOOGLE ADS HEADLINES (8, STRICT max 30 characters each — count karke likho):\n"
        "1. []\n2. []\n3. []\n4. []\n5. []\n6. []\n7. []\n8. []\n\n"
        "GOOGLE ADS DESCRIPTIONS (4, max 90 characters each):\n"
        "1. []\n2. []\n3. []\n4. []\n\n"
        "META AD COPY:\n"
        "Primary Text: [2-3 lines, conversational, Indian audience ke liye]\n"
        "Headline: [max 40 chars, punchy]\n"
        "CTA Button: [Shop Now / Book Now / Learn More / Get Quote]\n\n"
        "KPI TARGETS:\n"
        "Google Search CTR: [realistic 3-6%]\n"
        "Meta CTR: [realistic 1-3%]\n"
        "Expected CPL: Rs []\n"
        "Expected ROAS: []\n"
    )

    ad_name = request.competitor_name or request.competitor_website or request.business_type
    encoded = urllib.parse.quote(ad_name)
    meta_link = "https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=IN&q=" + encoded + "&search_type=keyword_unordered"
    if request.competitor_website:
        dom = request.competitor_website.replace("https://", "").replace("http://", "").replace("www.", "").rstrip("/").split("/")[0]
        google_link = "https://adstransparency.google.com/?region=IN&domain=" + dom
    else:
        google_link = "https://adstransparency.google.com/?region=IN"

    ad_prompt = (
        "Tu elite ad intelligence strategist hai.\n\n"
        "Competitor: " + ad_name + "\nIndustry: " + request.business_type + "\n\n"
        "Koi asterisk mat use kar. Seedha likho:\n\n"
        "AD LIBRARY MEIN KYA DEKHO:\n1. []\n2. []\n3. []\n\nWINNING AD KAISE PEHCHANE:\n1. []\n2. []\n3. []\n\nTUMHARA WINNING ANGLE:\n1. []\n2. []\n3. []\n"
    )

    audience_prompt = (
        "Tu ek elite media buyer hai jo Meta aur Google Ads ka expert hai.\n\n"
        "IMPORTANT RULES:\n"
        "1. Yeh Indian market ke liye hai — Indian apps, communities, channels suggest karo.\n"
        "2. Age exclude sirf 45+ karo.\n"
        "3. KABHI betting, gambling, wagering, investment words mat use karo.\n"
        "4. Display Placements mein gambling apps KABHI mat do.\n"
        "5. IDEAL AUDIENCE mein paise kamaana, earn money, win cash KABHI mat likho.\n\n"
        "BUSINESS: " + request.url + "\n"
        "INDUSTRY: " + request.business_type + "\n"
        "WEBSITE CONTENT:\n" + my_content[:1500] + "\n\n"
        "Is business ke liye exact audience batao. Koi asterisk mat use kar. Seedha likho:\n\n"
        "IDEAL AUDIENCE:\n[2-3 line — entertainment, competition, passion pe focus]\n\n"
        "AUDIENCE SEGMENTS:\n"
        "Segment 1 — [naam]: [age, gender, interests, behavior]\n"
        "Segment 2 — [naam]: [age, gender, interests, behavior]\n"
        "Segment 3 — [naam]: [age, gender, interests, behavior]\n\n"
        "WHERE TO FIND THEM:\n"
        "Apps: [5 RELEVANT apps jahan is specific business ki audience time spend karti hai. Fashion=Instagram/Myntra/Pinterest. Food=Zomato/Swiggy. Fitness=HealthifyMe/Cult.fit. KABHI irrelevant apps mat do jaise Zomato fashion ke liye ya UrbanClap food ke liye]\n"
        "Pages/Communities: [3 Facebook pages ya groups]\n"
        "YouTube Channels: [2-3 channels]\n"
        "Influencer Type: [kis type ke influencer]\n\n"
        "META ADS TARGETING:\n"
        "Interests: [5 specific interests]\n"
        "Behaviors: [2-3 behavior]\n"
        "Age/Gender: []\n"
        "Exclude: [sirf 45+]\n\n"
        "GOOGLE ADS TARGETING:\n"
        "In-Market Segments: [Sports & Fitness, Online Games, Mobile Games & Apps jaise actual segments — irrelevant mat do]\n"
        "Custom Segment Keywords: [5 keywords]\n"
        "Search Keywords: [5 high-intent keywords]\n\n"
        "DISPLAY PLACEMENTS (IMPORTANT: specific website/app names do — jaise Vogue India, Femina, LBB, Hauterfly, Sportskeeda, Cricbuzz. Generic categories KABHI mat do):\n"
        "1. []\n2. []\n3. []\n4. []\n5. []\n\n"
        "POLICY SAFETY CHECK:\n"
        "Risk Level: [Low/Medium/High]\n"
        "Avoid These Words: []\n"
        "Certification Needed: []\n"
    )

    async def get_competitor():
        if not request.competitor_website:
            return ""
        comp_prompt = (
            "Tu competitor intelligence analyst hai.\n\n"
            "MERA BUSINESS (" + request.url + "):\n" + my_content[:1200] + "\n\nCOMPETITOR (" + request.competitor_website + "):\n" + comp_content[:1200] + "\n\n"
            "Koi asterisk mat use kar. Seedha likho:\n\n"
            "COMPETITOR POSITIONING:\n[2 lines]\n\nUNKI STRENGTHS:\n1. []\n2. []\n\nUNKI WEAKNESS:\n1. []\n2. []\n\nMARKET GAPS:\n1. []\n2. []\n3. []\n\nTUM KAHAN JEET SAKTE HO:\n1. []\n2. []\n3. []\n"
        )
        return await run_ai(comp_prompt, 1200)

    # Phase 1: strategy, competitor, ad_guide, audience all run in parallel
    strategy, competitor_result, ad_guide, audience_result = await asyncio.gather(
        run_ai(strategy_prompt, 1800),
        get_competitor(),
        run_ai(ad_prompt, 900),
        run_ai(audience_prompt, 1500),
    )

    # Phase 2: smart_creative depends on competitor_result from Phase 1
    creative_prompt = (
        "LANGUAGE: " + request.language + "\n\n"
        "Tu ek award-winning ad creative director hai. Competitor analysis dekh ke alag creative bana.\n"
        "HUMAN WRITING: Yeh AI buzzwords KABHI mat use kar: unleash, elevate, dive in, game-changer, unlock, revolutionize, seamless, empower, transform your.\n\n"
        "MERA BUSINESS: " + request.url + " (" + request.business_type + ")\nPROMOTE: " + request.goal + "\n\nCOMPETITOR ANALYSIS:\n" + (competitor_result or "N/A") + "\n\n"
        "2 ad creative banao jo competitor se alag hon. Koi asterisk mat use kar.\n\n"
        "WHY DIFFERENT:\n[1-2 line]\n\n"
        "CREATIVE 1: [angle]\nHook Line: []\nPrimary Text: []\nHeadline: []\nCTA Button: []\nImage Concept: []\nText On Image: []\n\n"
        "CREATIVE 2: [angle]\nHook Line: []\nPrimary Text: []\nHeadline: []\nCTA Button: []\nImage Concept: []\nText On Image: []\n"
    )
    smart_creative = await run_ai(creative_prompt, 1400)

    return {"success": True, "url": request.url, "strategy": strategy, "competitor": competitor_result, "ad_guide": ad_guide, "smart_creative": smart_creative, "audience": audience_result, "meta_ad_library_link": meta_link, "google_ads_link": google_link}

class AdCreativeRequest(BaseModel):
    url: str
    business_type: str
    offer: str
    platform: str = "Instagram"
    language: str = "Hinglish"

@app.post("/ad-creative")
async def ad_creative(request: AdCreativeRequest):
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

    prompt = (
        "Tu ek award-winning ad creative director hai jo Indian brands ke liye scroll-stopping ads banata hai.\n\n"
        "HUMAN WRITING: Yeh AI buzzwords KABHI mat use kar: unleash, elevate, dive in, game-changer, unlock, revolutionize, seamless, empower, transform your.\n"
        "LANGUAGE: " + request.language + "\n\n"
        "BRAND WEBSITE:\n" + site[:1500] + "\n\nPROMOTE: " + request.offer + "\nPLATFORM: " + request.platform + "\nINDUSTRY: " + request.business_type + "\n\n"
        "3 alag ad creative banao. Koi asterisk mat use kar.\n\n"
        "CREATIVE 1: [angle]\nHook Line: []\nPrimary Text: []\nHeadline: []\nCTA Button: []\nImage Concept: []\nText On Image: []\nColor Palette: []\nLayout: []\n\n"
        "CREATIVE 2: [angle]\nHook Line: []\nPrimary Text: []\nHeadline: []\nCTA Button: []\nImage Concept: []\nText On Image: []\nColor Palette: []\nLayout: []\n\n"
        "CREATIVE 3: [angle]\nHook Line: []\nPrimary Text: []\nHeadline: []\nCTA Button: []\nImage Concept: []\nText On Image: []\nColor Palette: []\nLayout: []\n"
    )

    ai_response = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}], max_tokens=2000)
    return {"success": True, "creative": ai_response.choices[0].message.content}

class AudienceRequest(BaseModel):
    url: str = ""
    niche: str = ""
    business_type: str
    offer: str = ""
    platform: str = "Both"
    language: str = "Hinglish"

@app.post("/audience-finder")
async def audience_finder(request: AudienceRequest):
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

    prompt = (
        "IMPORTANT: Poora response sirf is language mein likho: " + request.language + "\n\n"
        "Tu ek elite media buyer hai jo Meta aur Google Ads dono ka expert hai.\n\n"
        + business_info
        + niche_context
        + "PROMOTE: " + (request.offer or "general business") + "\n"
        + "PLATFORM: " + request.platform + "\n"
        + "INDUSTRY: " + request.business_type + "\n"
        + "IMPORTANT RULES:\n"
        + "1. Indian market ke liye — Dream11, MPL, My11Circle, Paytm First Games jaise Indian apps use karo.\n"
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
        + "In-Market Segments: [actual Google segments — Sports & Fitness, Online Games, Mobile Games & Apps]\n"
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

    ai_response = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}], max_tokens=2000)
    return {"success": True, "url": request.url, "niche": request.niche, "audience": ai_response.choices[0].message.content}

class IntelligenceRequest(BaseModel):
    url: str
    business_type: str = ""
    competitor_urls: list[str] = []

@app.post("/intelligence")
async def intelligence(request: IntelligenceRequest):
    import re, asyncio, json

    # ── helpers ─────────────────────────────────────────────────────────────

    def extract_evidence(html, page_type="homepage"):
        evidence = []

        # ── Standard meta tags ──────────────────────────────────────────────
        t = re.search(r'<title[^>]*>(.*?)</title>', html, re.I | re.S)
        if t:
            evidence.append({"type": "title", "value": t.group(1).strip(), "confidence": 0.95, "page": page_type})
        for name, label, conf in [
            ("description",        "meta_description",    0.90),
            ("keywords",           "keywords",            0.70),
            ("twitter:title",      "twitter_title",       0.82),
            ("twitter:description","twitter_description", 0.80),
        ]:
            m = re.search(r'<meta[^>]*name=["\']' + re.escape(name) + r'["\'][^>]*content=["\'](.*?)["\']', html, re.I)
            if m and m.group(1).strip():
                evidence.append({"type": label, "value": m.group(1).strip(), "confidence": conf, "page": page_type})
        for prop, label, conf in [
            ("og:title",       "og_title",       0.85),
            ("og:description", "og_description", 0.85),
            ("og:site_name",   "og_site_name",   0.80),
            ("og:type",        "og_type",        0.70),
        ]:
            og = re.search(r'<meta[^>]*property=["\']' + re.escape(prop) + r'["\'][^>]*content=["\'](.*?)["\']', html, re.I)
            if og and og.group(1).strip():
                evidence.append({"type": label, "value": og.group(1).strip(), "confidence": conf, "page": page_type})

        # ── Headings ────────────────────────────────────────────────────────
        for tag in ['h1', 'h2', 'h3']:
            conf = 0.85 if tag == 'h1' else (0.75 if tag == 'h2' else 0.65)
            for h in re.findall(r'<' + tag + r'[^>]*>(.*?)</' + tag + r'>', html, re.I | re.S):
                clean = re.sub(r'<[^>]+>', '', h).strip()
                if clean and len(clean) > 3:
                    evidence.append({"type": tag, "value": clean, "confidence": conf, "page": page_type})

        # ── Trust + pricing signals ─────────────────────────────────────────
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

        # ── SPA / Next.js fallbacks ─────────────────────────────────────────
        # __NEXT_DATA__ — deep extraction
        nd = re.search(r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html, re.I | re.S)
        if nd:
            try:
                next_data = json.loads(nd.group(1).strip())
                props = next_data.get("props", {}).get("pageProps", {})

                def flatten_next(obj, depth=0):
                    """Recursively pull key=value strings from __NEXT_DATA__ pageProps."""
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

        # JSON-LD structured data — field-aware extraction
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

        # noscript text
        for ns in re.findall(r'<noscript[^>]*>(.*?)</noscript>', html, re.I | re.S):
            clean = re.sub(r'<[^>]+>', ' ', ns)
            clean = re.sub(r'\s+', ' ', clean).strip()
            if clean and len(clean) > 10:
                evidence.append({"type": "noscript_text", "value": clean[:400], "confidence": 0.65, "page": page_type})

        # ── Body text ───────────────────────────────────────────────────────
        body = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.I | re.S)
        body = re.sub(r'<style[^>]*>.*?</style>', '', body, flags=re.I | re.S)
        body = re.sub(r'<[^>]+>', ' ', body)
        body = re.sub(r'\s+', ' ', body).strip()
        if body and len(body) > 50:
            evidence.append({"type": "body_text", "value": body[:2000], "confidence": 0.60, "page": page_type})

        return evidence

    async def fetch_page(url, page_type="homepage"):
        """Returns (fetched: bool, evidence: list)"""
        try:
            async with httpx.AsyncClient(timeout=12, follow_redirects=True) as c:
                r = await c.get(url, headers={"User-Agent": "Mozilla/5.0"})
                logger.info(f"[CRAWL] {url} → status={r.status_code} final_url={r.url}")
                if r.status_code < 400:
                    ev = extract_evidence(r.text, page_type)
                    logger.info(f"[CRAWL] {url} → {len(ev)} evidence points extracted")
                    return (True, ev)
                else:
                    logger.info(f"[CRAWL] {url} → SKIPPED (status {r.status_code})")
                    return (False, [])
        except Exception as e:
            logger.info(f"[CRAWL] {url} → EXCEPTION: {e}")
        return (False, [])

    async def fetch_sitemap_urls(base):
        """Fetch robots.txt → sitemap.xml, return scored list of crawlable URLs. Returns List[str]."""
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
                return -1  # skip homepage, fetched separately
            return 1

        sitemap_url = base + "/sitemap.xml"
        robots = await try_get(base + "/robots.txt")
        if robots:
            sm = re.search(r'Sitemap:\s*(https?://\S+)', robots, re.I)
            if sm:
                sitemap_url = sm.group(1).strip()
                logger.info(f"[SITEMAP] Found in robots.txt: {sitemap_url}")

        sitemap_xml = await try_get(sitemap_url)
        if not sitemap_xml:
            logger.info(f"[SITEMAP] Not found at {sitemap_url}")
            return []

        # Handle sitemap index → fetch first sub-sitemap
        sub_sitemaps = re.findall(r'<loc>(https?://[^<]*sitemap[^<]*\.xml[^<]*)</loc>', sitemap_xml, re.I)
        if sub_sitemaps:
            logger.info(f"[SITEMAP] Index found, fetching sub-sitemap: {sub_sitemaps[0]}")
            sub = await try_get(sub_sitemaps[0])
            if sub:
                sitemap_xml = sub

        all_urls = re.findall(r'<loc>(https?://[^<]+)</loc>', sitemap_xml, re.I)
        crawlable = [u for u in all_urls if not any(u.lower().endswith(e) for e in skip_ext)]
        selected = sorted(crawlable, key=score_url, reverse=True)
        # Remove homepage, dedupe, take top 6
        selected = list(dict.fromkeys(u for u in selected if u.rstrip('/') != base))[:6]
        logger.info(f"[SITEMAP] {len(all_urls)} total, {len(crawlable)} crawlable, {len(selected)} selected for crawl")
        return selected

    async def run_ai_json(prompt, max_tokens):
        resp = await asyncio.to_thread(
            lambda: client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                response_format={"type": "json_object"}
            )
        )
        try:
            return json.loads(resp.choices[0].message.content)
        except:
            return {}

    # ── PHASE 1: Evidence Collection Engine ─────────────────────────────────

    def classify_page_type(url):
        p = url.lower()
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

    base = request.url.rstrip('/')

    # Phase 1a: homepage + sitemap discovery in parallel
    logger.info(f"[CRAWL] Phase 1a: homepage + sitemap discovery for {base}")
    (hp_fetched, hp_ev), sitemap_urls = await asyncio.gather(
        fetch_page(base, "homepage"),
        fetch_sitemap_urls(base)
    )

    # Phase 1b: crawl real sitemap URLs or fall back to guesses
    if sitemap_urls:
        extra_targets = [(u, classify_page_type(u)) for u in sitemap_urls]
        logger.info(f"[CRAWL] Phase 1b: crawling {len(extra_targets)} sitemap URLs")
    else:
        extra_targets = [
            (base + "/about",    "about"),
            (base + "/about-us", "about"),
            (base + "/products", "products"),
            (base + "/shop",     "shop"),
            (base + "/contact",  "contact"),
        ]
        logger.info(f"[CRAWL] Phase 1b: no sitemap, using {len(extra_targets)} fallback guesses")

    for u, pt in extra_targets:
        logger.info(f"[CRAWL]   → {u} ({pt})")

    extra_results = await asyncio.gather(*[fetch_page(u, pt) for u, pt in extra_targets])

    all_page_results = [(hp_fetched, hp_ev)] + list(extra_results)
    pages_attempted = 1 + len(extra_targets)
    pages_fetched = sum(1 for (fetched, _) in all_page_results if fetched)
    pages_with_evidence = sum(1 for (fetched, ev) in all_page_results if fetched and ev)
    sitemap_crawled = bool(sitemap_urls)

    logger.info(f"[CRAWL] pages_attempted={pages_attempted} pages_fetched={pages_fetched} pages_with_evidence={pages_with_evidence} sitemap_crawled={sitemap_crawled}")

    all_evidence = []
    seen = set()
    for (fetched, page_ev) in all_page_results:
        for e in page_ev:
            key = (e["type"], e["value"][:60])
            if key not in seen:
                seen.add(key)
                all_evidence.append(e)

    if not all_evidence:
        return {"success": False, "scan_failed": True, "message": "Website unreachable. Check URL and try again."}

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

    # ── PHASE 2: Business DNA Engine ────────────────────────────────────────

    dna_prompt = (
        "You are a Business Intelligence DNA engine. Classify this business using ONLY the evidence below.\n"
        "Do NOT hallucinate. If evidence is missing for a field, use \"Unknown\".\n\n"
        f"EVIDENCE:\n{evidence_text}\n\n"
        f"BODY TEXT:\n{body_text[:1500]}\n\n"
        f"USER-STATED CATEGORY (verify against evidence, may be wrong): {request.business_type or 'Not provided'}\n\n"
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

    # ── PHASE 3: Opportunity + Threat + Audience in parallel ─────────────────

    dna_text = json.dumps(dna, indent=2)

    positioning_prompt = (
        "You are a Brand Positioning Strategist. Analyze where this business currently stands and where it should position itself.\n\n"
        f"BUSINESS DNA:\n{dna_text}\n\n"
        f"EVIDENCE:\n{evidence_text[:1500]}\n\n"
        f"COMPETITOR URLS: {request.competitor_urls if request.competitor_urls else 'None provided — use industry knowledge'}\n\n"
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
        '  "category_ownership_opportunity": "the category name they could OWN, e.g. Indias Most Trusted Home Fitness Brand",\n'
        '  "messaging_shift": "what the business currently says vs what it SHOULD say",\n'
        '  "reasoning": "why this positioning works for this business specifically",\n'
        '  "supporting_evidence": ["exact quote from evidence that supports this positioning"],\n'
        '  "confidence_score": 0\n'
        "}\n\n"
        "confidence_score 0-100: based on how much website evidence exists to support the positioning analysis. "
        "If competitor URLs were provided, base competitor positioning on them. Otherwise, use top industry players."
    )

    opportunity_prompt = (
        "You are a Market Opportunity Scoring engine. Score this business's digital advertising opportunity.\n\n"
        f"BUSINESS DNA:\n{dna_text}\n\n"
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
        "All scores 0-100. overall_opportunity_score = (market_opportunity_score * 0.4) + (conversion_potential_score * 0.4) + ((100 - competition_difficulty_score) * 0.2). "
        "High competition_difficulty_score = harder market = penalizes overall score."
    )

    threat_prompt = (
        "You are a Competitive Threat Intelligence engine. Assess the threat landscape for this business.\n\n"
        f"BUSINESS DNA:\n{dna_text}\n\n"
        f"COMPETITOR URLS: {request.competitor_urls if request.competitor_urls else 'None provided'}\n\n"
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
        "competitor_threat_score 0-100: 0=no threat, 100=extreme competition. Base on industry DNA and market saturation signals."
    )

    audience_prompt = (
        "You are an Audience Intelligence 2.0 engine. Generate audience segments ONLY from Business DNA evidence.\n"
        "RULE: Every segment MUST cite specific evidence. Reject any segment you cannot prove from the DNA.\n\n"
        f"BUSINESS DNA:\n{dna_text}\n\n"
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
        "Generate exactly 3 validated_segments and at least 1 rejected_segment to show the filter is working. confidence_score per segment: 0-100. "
        "audience_quality_score 0-100: average confidence of validated segments, penalised if fewer than 2 segments have strong evidence."
    )

    opportunity, threat, audience_intel, positioning = await asyncio.gather(
        run_ai_json(opportunity_prompt, 600),
        run_ai_json(threat_prompt, 600),
        run_ai_json(audience_prompt, 1400),
        run_ai_json(positioning_prompt, 700),
    )
    logger.info(f"[AUDIENCE] audience_quality_score={audience_intel.get('audience_quality_score')} segments={len(audience_intel.get('validated_segments', []))}")

    # ── PHASE 4: Executive Decision Engine ───────────────────────────────────

    exec_prompt = (
        "You are a CMO-level Executive Decision Engine. Based on all intelligence below, output the 5 highest-impact actions.\n\n"
        f"BUSINESS DNA:\n{dna_text}\n\n"
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

    # ── Response ─────────────────────────────────────────────────────────────

    return {
        "success": True,
        "url": request.url,
        "intelligence": {
            "evidence_collection": evidence_collection,
            "business_dna": dna,
            "opportunity_score": opportunity,
            "threat_intelligence": threat,
            "audience_intelligence": audience_intel,
            "positioning": positioning,
            "executive_decisions": executive,
        },
        "scores": {
            "dna_score": dna.get("dna_score", 0),
            "opportunity_score": round(opportunity.get("overall_opportunity_score", 0)),
            "threat_score": threat.get("competitor_threat_score", 0),
            "audience_quality_score": audience_intel.get("audience_quality_score", 0),
            "positioning_score": positioning.get("confidence_score", 0),
            "readiness_score": executive.get("overall_readiness_score", 0),
        },
    }


@app.get("/analyses")
def get_analyses(db: Session = Depends(get_db)):
    analyses = db.query(AnalysisModel).order_by(AnalysisModel.id.desc()).all()
    return {"analyses": [{"id": a.id, "url": a.url, "business_type": a.business_type, "created_at": a.created_at} for a in analyses]}

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

def get_google_ads_client():
    return GoogleAdsClient.load_from_dict({
        "developer_token":   os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN"),
        "client_id":         os.getenv("GOOGLE_ADS_CLIENT_ID"),
        "client_secret":     os.getenv("GOOGLE_ADS_CLIENT_SECRET"),
        "refresh_token":     os.getenv("GOOGLE_ADS_REFRESH_TOKEN"),
        "use_proto_plus":    True,
        "login_customer_id": os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID"),
    })

@app.get("/google-ads/performance")
async def google_ads_performance(days: int = 30):
    customer_id = os.getenv("GOOGLE_ADS_CUSTOMER_ID")
    try:
        client = get_google_ads_client()
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

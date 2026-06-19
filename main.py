from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import OpenAI
import httpx
from datetime import datetime
from typing import Optional
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from dotenv import load_dotenv
import os

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
        "HUMAN WRITING: AI buzzwords mat use kar.\n\n"
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
        "HUMAN WRITING: AI buzzwords mat use kar.\n"
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

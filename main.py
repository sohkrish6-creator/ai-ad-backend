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
# ============================================================

DATABASE_URL = "sqlite:///./ai_ad_manager.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Lead Table
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

# Analysis Table
class AnalysisModel(Base):
    __tablename__ = "analyses"
    id = Column(Integer, primary_key=True, index=True)
    url = Column(String(500))
    business_type = Column(String(100))
    budget = Column(Integer)
    goal = Column(String(100))
    result = Column(Text)
    created_at = Column(String(100))

# Tables banao
Base.metadata.create_all(bind=engine)

# DB session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ============================================================
# MODELS
# ============================================================

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

# ============================================================
# ROUTES
# ============================================================

@app.get("/")
def home():
    return {"message": "AI Ad Manager Backend chal raha hai!"}


@app.post("/analyze")
async def analyze(request: AnalyzeRequest, db: Session = Depends(get_db)):
    import json

    # STEP 1 — Website crawl + clean
    import re
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client_http:
            response = await client_http.get(request.url, headers={"User-Agent": "Mozilla/5.0"})
            raw = response.text
    except:
        raw = ""

    def extract_clean(html):
        parts = []
        # Title
        t = re.search(r'<title[^>]*>(.*?)</title>', html, re.I | re.S)
        if t: parts.append("TITLE: " + t.group(1).strip())
        # Meta description
        m = re.search(r'<meta[^>]*name=["\']description["\'][^>]*content=["\'](.*?)["\']', html, re.I)
        if m: parts.append("DESCRIPTION: " + m.group(1).strip())
        # OG title + description
        ogt = re.search(r'<meta[^>]*property=["\']og:title["\'][^>]*content=["\'](.*?)["\']', html, re.I)
        if ogt: parts.append("OG_TITLE: " + ogt.group(1).strip())
        ogd = re.search(r'<meta[^>]*property=["\']og:description["\'][^>]*content=["\'](.*?)["\']', html, re.I)
        if ogd: parts.append("OG_DESC: " + ogd.group(1).strip())
        # Keywords meta
        kw = re.search(r'<meta[^>]*name=["\']keywords["\'][^>]*content=["\'](.*?)["\']', html, re.I)
        if kw: parts.append("KEYWORDS: " + kw.group(1).strip())
        # Headings
        for tag in ['h1', 'h2', 'h3']:
            for h in re.findall(r'<' + tag + r'[^>]*>(.*?)</' + tag + r'>', html, re.I | re.S):
                clean = re.sub(r'<[^>]+>', '', h).strip()
                if clean and len(clean) > 2:
                    parts.append(f"{tag.upper()}: {clean}")
        # Body text (scripts/styles hata ke)
        body = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.I | re.S)
        body = re.sub(r'<style[^>]*>.*?</style>', '', body, flags=re.I | re.S)
        body = re.sub(r'<[^>]+>', ' ', body)
        body = re.sub(r'\s+', ' ', body).strip()
        parts.append("BODY: " + body[:2000])
        return "\n".join(parts)

    website_text = extract_clean(raw) if raw else ""

    if not website_text or len(website_text) < 100:
        return {
            "success": False,
            "scan_failed": True,
            "message": "Website scan nahi ho payi. URL check karo ya doosra try karo."
        }

    # STEP 2 — Business Classification
    classify_prompt = (
        "You are a strict Business Classification Engine.\n"
        "RULE: Website content (especially TITLE, DESCRIPTION, headings) is the ONLY source of truth.\n"
        "The user-selected category is almost always wrong — IGNORE it unless the website clearly confirms it.\n"
        "If the website TITLE or DESCRIPTION mentions specific products/services that contradict the "
        "selected category, you MUST set category_mismatch to true.\n\n"
        f"User-selected category (likely wrong, treat with suspicion): {request.business_type}\n"
        f"Website URL: {request.url}\n"
        f"Website content:\n{website_text}\n\n"
        "Example: If TITLE says 'Caps, Streetwear & Accessories' but user selected 'Wedding & Events', "
        "then detected = 'Fashion / Apparel / Accessories', category_mismatch = true.\n\n"
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

    # STEP 3 — Mismatch ya low confidence pe rok do
    if not request.force and (mismatch or confidence < 85):
        return {
            "success": False,
            "needs_confirmation": True,
            "classification": class_data
        }

    # STEP 4 — Confirmed, ab strategy banao
    detected = class_data.get("recommended_category") or request.business_type
    services = ", ".join(class_data.get("primary_products_or_services", []))

    prompt = (
        "Tu ek world-class digital marketing strategist hai jo Google Ads aur Meta Ads expert hai.\n\n"
        "IMPORTANT: Saari recommendations website content ke evidence pe based honi chahiye. Generic advice mat de.\n\n"
        "HUMAN WRITING RULE (sabse important — HEADLINES, DESCRIPTIONS, META AD COPY ke liye):\n"
        "Yeh content seedha real ads mein jaayega, isliye yeh ek experienced human copywriter ki tarah likho — AI ki tarah BILKUL nahi.\n"
        "- Yeh AI buzzwords KABHI mat use kar: unleash, elevate, dive in, game-changer, unlock, in today\'s world, look no further, take it to the next level, revolutionize, seamless, empower, discover the magic, your one-stop, transform your.\n"
        "- Chhote, punchy, natural lines likho jaise ek insaan bolta hai. Perfect grammar ki zaroorat nahi.\n"
        "- Real benefit ya emotion pe focus kar, fancy shabdon pe nahi.\n"
        "- Indian audience ke liye ho to thodi local feel de (jahan suit kare).\n"
        "- Har headline alag ho — repeat structure mat kar.\n\n"
        f"Business URL: {request.url}\n"
        f"VERIFIED Business Category: {detected}\n"
        f"Detected Services/Products: {services}\n"
        f"Monthly Budget: Rs {request.budget}\n"
        f"Marketing Goal: {request.goal}\n"
        f"Website Content (source of truth):\n{website_text[:3000]}\n\n"
        "Niche format mein poora analysis de. Koi asterisk mat use kar. Seedha likho:\n\n"
        "BUSINESS SUMMARY:\n[2-3 lines, website evidence ke saath]\n\n"
        "TARGET AUDIENCE:\n[2-3 lines, website ke products/services ke based]\n\n"
        "DEMOGRAPHICS:\n"
        "Age Range: []\nGender: []\nIncome Level: []\nEducation: []\nLocation: []\nLanguage: []\nMarital Status: []\n\n"
        "DEVICE TARGETING:\n"
        "Mobile: [%] - [reason]\nDesktop: [%] - [reason]\nTablet: [%] - [reason]\nBest Device: []\n\n"
        "AD PLACEMENTS:\n"
        "1. Instagram Feed: [suitable/not + reason]\n"
        "2. Instagram Reels: [suitable/not + reason]\n"
        "3. Instagram Stories: [suitable/not + reason]\n"
        "4. Facebook Feed: [suitable/not + reason]\n"
        "5. Facebook Reels: [suitable/not + reason]\n"
        "6. YouTube Pre-roll: [suitable/not + reason]\n"
        "7. Google Search: [suitable/not + reason]\n"
        "8. Google Display: [suitable/not + reason]\n"
        "9. Gmail Ads: [suitable/not + reason]\n\n"
        "TIME TARGETING:\n"
        "Best Days: []\nPeak Hours: []\nAvoid: []\nReason: []\n\n"
        f"BUDGET SPLIT (Total Rs {request.budget}/month):\n"
        "1. [Platform]: Rs [amount] ([%]) - [reason]\n"
        "2. [Platform]: Rs [amount] ([%]) - [reason]\n"
        "3. [Platform]: Rs [amount] ([%]) - [reason]\n\n"
        "CAMPAIGN STRUCTURE:\n"
        "Campaign Name: []\nCampaign Type: []\nBid Strategy: []\n"
        f"Daily Budget: Rs {request.budget // 30}\n"
        "Target CPA: Rs []\nMax CPC: Rs []\nExpected ROAS: []\n\n"
        "AD GROUPS:\n"
        "1. Group: [] | Keywords: [kw1, kw2, kw3, kw4, kw5]\n"
        "2. Group: [] | Keywords: [kw1, kw2, kw3, kw4, kw5]\n"
        "3. Group: [] | Keywords: [kw1, kw2, kw3, kw4, kw5]\n\n"
        "HEADLINES (20, max 30 chars each):\n"
        "1. []\n2. []\n3. []\n4. []\n5. []\n6. []\n7. []\n8. []\n9. []\n10. []\n"
        "11. []\n12. []\n13. []\n14. []\n15. []\n16. []\n17. []\n18. []\n19. []\n20. []\n\n"
        "DESCRIPTIONS (20, max 90 chars each):\n"
        "1. []\n2. []\n3. []\n4. []\n5. []\n6. []\n7. []\n8. []\n9. []\n10. []\n"
        "11. []\n12. []\n13. []\n14. []\n15. []\n16. []\n17. []\n18. []\n19. []\n20. []\n\n"
        "META AD COPY:\n"
        "Primary Text: []\nHeadline: []\nDescription: []\nCTA Button: []\n\n"
        "INTEREST TARGETING:\n1. []\n2. []\n3. []\n4. []\n5. []\n6. []\n\n"
        "REMARKETING STRATEGY:\n1. []\n2. []\n3. []\n\n"
        "KPI TARGETS:\n"
        "Expected CTR: []\nExpected CPL: Rs []\nExpected CPC: Rs []\nExpected ROAS: []\nExpected Conversion Rate: []\n\n"
        "CREATIVE BRIEF:\n"
        "Image Concept: []\nColor Palette: []\nVideo Script (15 sec): []\n"
        "Carousel Slide 1: []\nCarousel Slide 2: []\nCarousel Slide 3: []\n\n"
        "AB TESTING PLAN:\n1. Test 1: [A] vs [B] - [reason]\n2. Test 2: [A] vs [B] - [reason]\n3. Test 3: [A] vs [B] - [reason]\n\n"
        "LANDING PAGE SUGGESTIONS:\n"
        "Hero Headline: []\nSub Headline: []\nCTA Button: []\nTrust Signals: []\n\n"
        "NEGATIVE KEYWORDS:\n1. []\n2. []\n3. []\n4. []\n5. []\n\n"
        "COMMON MISTAKES:\n1. []\n2. []\n3. []\n\n"
        "OPPORTUNITIES:\n1. []\n2. []\n3. []\n"
    )

    ai_response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=4000
    )

    result = ai_response.choices[0].message.content

    analysis = AnalysisModel(
        url=request.url,
        business_type=detected,
        budget=request.budget,
        goal=request.goal,
        result=result,
        created_at=datetime.now().strftime("%d %b %Y, %I:%M %p")
    )
    db.add(analysis)
    db.commit()

    return {
        "success": True,
        "url": request.url,
        "detected_category": detected,
        "confidence": confidence,
        "analysis": result
    }
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

    # My website
    my_content = await fetch(request.my_url)

    # Competitors
    comp_blocks = []
    for i, cu in enumerate(request.competitor_urls):
        if not cu.strip():
            continue
        content = await fetch(cu)
        comp_blocks.append(f"COMPETITOR {i+1} ({cu}):\n{content[:1500]}\n")

    competitors_text = "\n".join(comp_blocks)

    prompt = (
        "Tu ek world-class competitor intelligence analyst hai. "
        "Sab kuch website content ke evidence pe based karo. Generic mat bano.\n\n"
        f"Business Type: {request.business_type}\n\n"
        f"MY BUSINESS ({request.my_url}):\n{my_content[:1500]}\n\n"
        f"COMPETITORS:\n{competitors_text}\n\n"
        "Niche format mein analysis de. Koi asterisk mat use kar. Seedha likho:\n\n"
        "MY POSITIONING:\n[2-3 lines - mera business kaise position hai]\n\n"
        "COMPETITOR ANALYSIS:\n"
        "[Har competitor ke liye alag block:]\n"
        "Competitor 1: [naam/url]\n"
        "Positioning: []\n"
        "Strengths: []\n"
        "Weaknesses: []\n"
        "Messaging Style: []\n\n"
        "[agar aur competitors hain to same format repeat karo]\n\n"
        "MARKET GAPS:\n"
        "1. [konsi cheez koi nahi kar raha - opportunity]\n"
        "2. [underserved audience]\n"
        "3. [overused messaging jahan tum alag ho sakte ho]\n\n"
        "WHERE YOU CAN WIN:\n"
        "1. [specific advantage 1]\n"
        "2. [specific advantage 2]\n"
        "3. [specific advantage 3]\n\n"
        "RECOMMENDED MESSAGING:\n"
        "[Tumhare liye unique messaging jo competitors se alag ho]\n\n"
        "QUICK WINS:\n"
        "1. [abhi kar sakte ho]\n"
        "2. [abhi kar sakte ho]\n"
        "3. [abhi kar sakte ho]\n"
    )

    ai_response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2500
    )

    return {
        "success": True,
        "analysis": ai_response.choices[0].message.content
    }
class AdIntelRequest(BaseModel):
    business_name: str
    business_type: str
    website: str = ""
    country: str = "IN"


@app.post("/ad-intelligence")
async def ad_intelligence(request: AdIntelRequest):
    import urllib.parse

    # Meta Ad Library search link banao
    encoded_name = urllib.parse.quote(request.business_name)
    meta_link = (
        f"https://www.facebook.com/ads/library/?active_status=active"
        f"&ad_type=all&country={request.country}"
        f"&q={encoded_name}&search_type=keyword_unordered"
    )

    # Google Ads Transparency link (domain-based)
    if request.website:
        domain = request.website.replace("https://", "").replace("http://", "").replace("www.", "").rstrip("/").split("/")[0]
        google_link = f"https://adstransparency.google.com/?region={request.country}&domain={domain}"
    else:
        google_link = f"https://adstransparency.google.com/?region={request.country}"

    # AI se analysis guide (MAGIC PROMPT)
    prompt = (
        "Tu ek elite competitor ad intelligence strategist hai jisne 10 saal brands ke liye ad spying ki hai. "
        "Tu sirf woh advice deta hai jo PUBLIC ad tools se actually possible ho — theory ki hawa-hawai baatein nahi.\n\n"
        "CRITICAL REALITY (yeh hamesha yaad rakh):\n"
        "Meta Ad Library aur Google Ads Transparency PUBLIC tools hain. Inme yeh DIKHTA hai:\n"
        "- Ad ka creative (image, video, text copy)\n"
        "- Ad kab se chal raha hai (start date)\n"
        "- Ek brand kitne ads ek saath chala raha hai\n"
        "- Ek ad ke kitne versions hain (A/B testing ka proof)\n"
        "- Konse platforms pe chal raha hai (FB, IG, Search, YouTube, Display)\n"
        "Inme yeh NAHI dikhta — user ko yeh dekhne ke liye KABHI mat bol:\n"
        "- Likes, shares, comments, CTR, conversions, ROAS, budget, spend (yeh private data hai)\n\n"
        f"COMPETITOR: {request.business_name}\n"
        f"WEBSITE: {request.website or 'N/A'}\n"
        f"INDUSTRY: {request.business_type}\n\n"
        "Ab is SPECIFIC competitor aur industry ke liye ekdum sharp, practical guide de. "
        "Generic mat bol — is industry ki asli baatein bol (jaise skincare ho to 'skin-concern angle', fashion ho to 'discount % aur seasonal drops'). "
        "Koi asterisk ya markdown mat use kar. Seedha likho:\n\n"
        "AD LIBRARY MEIN KYA DEKHNA HAI:\n"
        f"[{request.business_type} ke liye 5 SPECIFIC cheezein jo Ad Library mein actually dikhti hain]\n"
        "1. []\n2. []\n3. []\n4. []\n5. []\n\n"
        "WINNING ADS KAISE PEHCHANE:\n"
        "[4 REAL signals jo Ad Library mein dikhte hain — run-date, ad count, versions, creative consistency. CTR/likes NAHI.]\n"
        "1. [jaise: ek ad 3+ mahine se chal raha = woh paisa kama raha, warna band kar dete]\n"
        "2. []\n3. []\n4. []\n\n"
        f"{request.business_name} KE BAARE MEIN YEH PATA KARO:\n"
        "[5 sawaal jo sirf public ad data dekh ke answer ho sakte hain]\n"
        "1. []\n2. []\n3. []\n4. []\n5. []\n\n"
        "UNKI KAMZORI (TUMHARA MAUKA):\n"
        "[3 cheezein — agar competitor ke ads mein yeh dikhe to tumhare liye jeet ka mauka]\n"
        "1. []\n2. []\n3. []\n\n"
        "TUMHARA WINNING ANGLE:\n"
        f"[{request.business_type} ke liye 3 SPECIFIC ad ideas jo competitor se alag hon, har ek ke saath chhota 'kyun kaam karega']\n"
        "1. []\n2. []\n3. []\n\n"
        "ABHI YEH KARO (ACTION STEPS):\n"
        "[Ad Library dekhne ke baad 3 concrete next steps]\n"
        "1. []\n2. []\n3. []\n"
    )

    ai_response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1500
    )

    return {
        "success": True,
        "business_name": request.business_name,
        "meta_ad_library_link": meta_link,
        "google_ads_link": google_link,
        "guide": ai_response.choices[0].message.content
    }
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
    import re, urllib.parse

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

    my_content = await fetch(request.url)
    if not my_content or len(my_content) < 100:
        return {"success": False, "scan_failed": True, "message": "Website scan nahi ho payi."}

    strategy_prompt = (
        "Tu ek senior digital marketing strategist hai. Website evidence pe based, generic nahi.\n\n"
        "HUMAN WRITING: headlines/ad copy ek real copywriter ki tarah likho, AI buzzwords (unleash, elevate, game-changer, unlock, dive in) BILKUL mat use kar.\n"
        "LANGUAGE: saara content is language mein likho: " + request.language + " (Hinglish=Roman Hindi-English mix, English=clean English, Hindi=Devanagari).\n\n"
        "URL: " + request.url + "\nBusiness: " + request.business_type + "\nBudget: Rs " + str(request.budget) + "\nGoal: " + request.goal + "\nWebsite:\n" + my_content[:2000] + "\n\n"
        "Koi asterisk mat use kar. Seedha likho:\n\n"
        "BUSINESS SUMMARY:\n[2 lines]\n\nTARGET AUDIENCE:\n[2 lines]\n\n"
        "BUDGET SPLIT:\n1. [Platform]: Rs [amt] - [reason]\n2. [Platform]: Rs [amt] - [reason]\n3. [Platform]: Rs [amt] - [reason]\n\n"
        "TOP HEADLINES (human-like, 8):\n1. []\n2. []\n3. []\n4. []\n5. []\n6. []\n7. []\n8. []\n\n"
        "META AD COPY:\nPrimary Text: []\nHeadline: []\nCTA: []\n\n"
        "KPI TARGETS:\nExpected CTR: []\nExpected CPL: Rs []\nExpected ROAS: []\n"
    )
    strategy = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": strategy_prompt}], max_tokens=1800).choices[0].message.content

    competitor_result = ""
    if request.competitor_website:
        comp_content = await fetch(request.competitor_website)
        comp_prompt = (
            "Tu competitor intelligence analyst hai. Evidence pe based.\n\n"
            "MERA BUSINESS (" + request.url + "):\n" + my_content[:1200] + "\n\n"
            "COMPETITOR (" + request.competitor_website + "):\n" + comp_content[:1200] + "\n\n"
            "Koi asterisk mat use kar. Seedha likho:\n\n"
            "COMPETITOR POSITIONING:\n[2 lines]\n\nUNKI STRENGTHS:\n1. []\n2. []\n\nUNKI WEAKNESS:\n1. []\n2. []\n\n"
            "MARKET GAPS:\n1. []\n2. []\n3. []\n\nTUM KAHAN JEET SAKTE HO:\n1. []\n2. []\n3. []\n"
        )
        competitor_result = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": comp_prompt}], max_tokens=1200).choices[0].message.content

    ad_name = request.competitor_name or request.competitor_website or request.business_type
    encoded = urllib.parse.quote(ad_name)
    meta_link = "https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=IN&q=" + encoded + "&search_type=keyword_unordered"
    if request.competitor_website:
        dom = request.competitor_website.replace("https://", "").replace("http://", "").replace("www.", "").rstrip("/").split("/")[0]
        google_link = "https://adstransparency.google.com/?region=IN&domain=" + dom
    else:
        google_link = "https://adstransparency.google.com/?region=IN"

    ad_prompt = (
        "Tu elite ad intelligence strategist hai. Ad Library mein dikhta hai: creative, run-date, ad count, versions, platforms. NAHI dikhta: likes, CTR, budget. User ko woh dekhne mat bol.\n\n"
        "Competitor: " + ad_name + "\nIndustry: " + request.business_type + "\n\n"
        "Koi asterisk mat use kar. Seedha likho:\n\n"
        "AD LIBRARY MEIN KYA DEKHO:\n1. []\n2. []\n3. []\n\n"
        "WINNING AD KAISE PEHCHANE:\n1. [3+ mahine se chal raha = paisa kama raha]\n2. []\n3. []\n\n"
        "TUMHARA WINNING ANGLE:\n1. []\n2. []\n3. []\n"
    )
    ad_guide = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": ad_prompt}], max_tokens=900).choices[0].message.content

    # SMART CREATIVE — competitor analysis dekh ke alag/new ad
    creative_prompt = (
        "IMPORTANT: Poora response sirf is language mein likho: " + request.language + ". (English=clean English only, Hinglish=Roman Hindi-English, Hindi=Devanagari). Yeh rule sabse upar hai.\n\n"
        "Tu ek award-winning ad creative director hai. Tujhe poora competitor analysis mila hai. "
        "Ab aisa ad creative bana jo competitor se BILKUL ALAG ho aur market gap ko target kare.\n\n"
        "HUMAN WRITING: real copywriter ki tarah likho. AI buzzwords (unleash, elevate, game-changer, unlock, dive in, transform, seamless, awaits) BILKUL mat use kar.\n"
        "LANGUAGE: saara ad content is language mein: " + request.language + ".\n\n"
        "MERA BUSINESS: " + request.url + " (" + request.business_type + ")\n"
        "PROMOTE: " + request.goal + "\n\n"
        "COMPETITOR ANALYSIS (yeh dekh ke alag bano):\n" + (competitor_result or "N/A") + "\n\n"
        "Upar competitor ki weakness aur market gap dekho. Ab 2 ad creative banao jo woh angle len jo competitor NAHI kar raha. "
        "Koi asterisk mat use kar. Is format mein:\n\n"
        "WHY DIFFERENT:\n[1-2 line — yeh ad competitor se kaise alag hai, konsa gap target kar raha]\n\n"
        "CREATIVE 1: [angle naam]\n"
        "Hook Line: []\n"
        "Primary Text: []\n"
        "Headline: []\n"
        "CTA Button: []\n"
        "Image Concept: []\n"
        "Text On Image: []\n\n"
        "CREATIVE 2: [angle naam]\n"
        "Hook Line: []\n"
        "Primary Text: []\n"
        "Headline: []\n"
        "CTA Button: []\n"
        "Image Concept: []\n"
        "Text On Image: []\n"
    )
    smart_creative = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": creative_prompt}], max_tokens=1400).choices[0].message.content

    return {
        "success": True,
        "url": request.url,
        "strategy": strategy,
        "competitor": competitor_result,
        "ad_guide": ad_guide,
        "smart_creative": smart_creative,
        "meta_ad_library_link": meta_link,
        "google_ads_link": google_link
    }


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
        "HUMAN WRITING: real copywriter ki tarah likho. AI buzzwords (unleash, elevate, game-changer, unlock, dive in, transform, seamless, discover the magic) BILKUL mat use kar. Chhota, punchy, emotional likho.\n\n"
        "LANGUAGE: Saara ad content (hook, copy, headline, text on image) is language mein likho: " + request.language + ". Agar Hinglish hai to Roman script mein Hindi-English mix. Agar English hai to clean simple English. Agar Hindi hai to Devanagari script.\n\n"
        "BRAND WEBSITE:\n" + site[:1500] + "\n\n"
        "PROMOTE KARNA HAI: " + request.offer + "\n"
        "PLATFORM: " + request.platform + "\n"
        "INDUSTRY: " + request.business_type + "\n\n"
        "3 alag-alag ad creative banao — har ek alag angle se (jaise emotional, offer-driven, problem-solution). "
        "Koi asterisk ya markdown mat use kar. Bilkul is format mein de:\n\n"
        "CREATIVE 1: [angle ka naam]\n"
        "Hook Line: [scroll rokne wali pehli line]\n"
        "Primary Text: [2-3 lines ad caption]\n"
        "Headline: [chhota punchy headline]\n"
        "CTA Button: [jaise Shop Now, Book Now]\n"
        "Image Concept: [image mein kya dikhe — scene, mood]\n"
        "Text On Image: [image ke upar kya likha ho — bada bold text]\n"
        "Color Palette: [2-3 colors]\n"
        "Layout: [top mein kya, center mein kya, bottom mein kya]\n\n"
        "CREATIVE 2: [angle ka naam]\n"
        "Hook Line: []\n"
        "Primary Text: []\n"
        "Headline: []\n"
        "CTA Button: []\n"
        "Image Concept: []\n"
        "Text On Image: []\n"
        "Color Palette: []\n"
        "Layout: []\n\n"
        "CREATIVE 3: [angle ka naam]\n"
        "Hook Line: []\n"
        "Primary Text: []\n"
        "Headline: []\n"
        "CTA Button: []\n"
        "Image Concept: []\n"
        "Text On Image: []\n"
        "Color Palette: []\n"
        "Layout: []\n"
    )

    ai_response = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}], max_tokens=2000)

    return {"success": True, "creative": ai_response.choices[0].message.content}


class AudienceRequest(BaseModel):
    url: str
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

    site = await fetch(request.url)
    if not site or len(site) < 80:
        return {"success": False, "scan_failed": True, "message": "Website scan nahi ho payi."}

    prompt = (
        "IMPORTANT: Poora response sirf is language mein likho: " + request.language + " (English=clean English, Hinglish=Roman Hindi-English mix, Hindi=Devanagari).\n\n"
        "Tu ek elite media buyer hai jo Meta aur Google Ads dono ka expert hai aur audience targeting + ad policy ka master hai.\n\n"
        "BUSINESS WEBSITE:\n" + site[:1800] + "\n\n"
        "PROMOTE: " + (request.offer or "general business") + "\n"
        "PLATFORM: " + request.platform + "\n"
        "INDUSTRY: " + request.business_type + "\n\n"
        "Is business ke liye exact audience aur placements batao. Real, specific bano — generic mat. "
        "Koi asterisk ya markdown mat use kar. Seedha is format mein:\n\n"
        "IDEAL AUDIENCE:\n[2-3 line — yeh business kiske liye perfect hai, age, interest, behavior]\n\n"
        "META ADS TARGETING:\n"
        "Interests: [5 specific interests jo Meta mein daalo]\n"
        "Behaviors: [2-3 behavior]\n"
        "Age/Gender: []\n"
        "Exclude: [kisko HATAO — paisa bachao]\n\n"
        "GOOGLE ADS TARGETING:\n"
        "In-Market Segments: [3 segments jo abhi khareedne wale hain]\n"
        "Custom Segment Keywords: [5 keywords]\n"
        "Search Keywords: [5 high-intent keywords]\n\n"
        "DISPLAY PLACEMENTS (Banner ke liye):\n"
        "[5 specific apps/websites jaha is business ka banner lagana chahiye — naam ke saath]\n"
        "1. []\n2. []\n3. []\n4. []\n5. []\n\n"
        "REMARKETING:\n[2 line — jo log aaye unhe phir kaise target karo]\n\n"
        "EXPECTED CTR:\n[is type ke ad ka realistic tap rate, jaise banner 0.5-1%]\n\n"
        "POLICY SAFETY CHECK:\n"
        "Risk Level: [Low/Medium/High — yeh business Google pe reject ho sakta?]\n"
        "Avoid These Words: [konse misleading/trademark words ad mein mat daalo]\n"
        "Landing Page Tip: [ad aur website match ho, kya dhyan rakho]\n"
        "Certification Needed: [agar finance/health/gambling hai to certification, warna None]\n"
    )

    ai_response = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}], max_tokens=1800)

    return {"success": True, "url": request.url, "audience": ai_response.choices[0].message.content}


@app.get("/analyses")
def get_analyses(db: Session = Depends(get_db)):
    analyses = db.query(AnalysisModel).order_by(AnalysisModel.id.desc()).all()
    return {"analyses": [{"id": a.id, "url": a.url, "business_type": a.business_type, "created_at": a.created_at} for a in analyses]}


@app.post("/leads")
def add_lead(lead: LeadCreate, db: Session = Depends(get_db)):
    new_lead = LeadModel(
        name=lead.name,
        phone=lead.phone,
        email=lead.email,
        source=lead.source,
        message=lead.message,
        campaign=lead.campaign,
        status="New",
        created_at=datetime.now().strftime("%d %b %Y, %I:%M %p")
    )
    db.add(new_lead)
    db.commit()
    db.refresh(new_lead)
    return {
        "success": True,
        "lead": {
            "id": new_lead.id,
            "name": new_lead.name,
            "phone": new_lead.phone,
            "email": new_lead.email,
            "source": new_lead.source,
            "message": new_lead.message,
            "status": new_lead.status,
            "created_at": new_lead.created_at
        }
    }


@app.get("/leads")
def get_leads(db: Session = Depends(get_db)):
    leads = db.query(LeadModel).order_by(LeadModel.id.desc()).all()
    return {
        "leads": [
            {
                "id": l.id,
                "name": l.name,
                "phone": l.phone,
                "email": l.email,
                "source": l.source,
                "message": l.message,
                "status": l.status,
                "created_at": l.created_at
            } for l in leads
        ],
        "total": len(leads)
    }


@app.get("/leads/stats")
def get_stats(db: Session = Depends(get_db)):
    leads = db.query(LeadModel).all()
    total = len(leads)
    whatsapp = len([l for l in leads if l.source == "whatsapp"])
    website = len([l for l in leads if l.source == "website"])
    form = len([l for l in leads if l.source == "form"])
    converted = len([l for l in leads if l.status == "Converted"])
    return {
        "total": total,
        "whatsapp": whatsapp,
        "website": website,
        "form": form,
        "new": len([l for l in leads if l.status == "New"]),
        "converted": converted
    }


@app.put("/leads/{lead_id}")
def update_lead(lead_id: int, status: str, db: Session = Depends(get_db)):
    lead = db.query(LeadModel).filter(LeadModel.id == lead_id).first()
    if lead:
        lead.status = status
        db.commit()
        return {"success": True}
    return {"success": False, "message": "Lead nahi mila"}
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

    # AI se analysis guide
    prompt = (
        "Tu ek senior competitor ad intelligence analyst hai. "
        "User ko guide kar raha hai ki competitor ke ads dekhte waqt kya analyze karna hai.\n\n"
        f"Competitor Business: {request.business_name}\n"
        f"Industry: {request.business_type}\n\n"
        "Niche format mein practical guide de. Koi asterisk mat use kar. Seedha likho:\n\n"
        "WHAT TO LOOK FOR:\n"
        "[5 specific cheezein jo is industry ke ads mein dekhni chahiye]\n"
        "1. []\n2. []\n3. []\n4. []\n5. []\n\n"
        "WINNING AD SIGNALS:\n"
        "[Kaise pehchane konsa ad 'kaam kar raha hai'. 4 points]\n"
        "1. []\n2. []\n3. []\n4. []\n\n"
        "QUESTIONS TO ANSWER:\n"
        "[Competitor ke ads dekh ke in sawaalon ke jawab dhoondo. 5 questions]\n"
        "1. []\n2. []\n3. []\n4. []\n5. []\n\n"
        "RED FLAGS (unki kamzori):\n"
        "[Agar competitor ke ads mein yeh dikhe to opportunity hai. 3 points]\n"
        "1. []\n2. []\n3. []\n\n"
        f"YOUR ANGLE:\n"
        f"[{request.business_type} business ke liye, competitor se alag dikhne ke 3 specific ideas]\n"
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
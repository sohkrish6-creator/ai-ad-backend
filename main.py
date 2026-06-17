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
    try:
        async with httpx.AsyncClient(timeout=10) as client_http:
            response = await client_http.get(request.url)
            website_text = response.text[:2000]
    except:
        website_text = "Website content load nahi hua"

    prompt = (
        "Tu ek world-class digital marketing strategist hai jo Google Ads aur Meta Ads expert hai.\n\n"
        f"Business URL: {request.url}\n"
        f"Business Type: {request.business_type}\n"
        f"Monthly Budget: Rs {request.budget}\n"
        f"Marketing Goal: {request.goal}\n"
        f"Website Content: {website_text}\n\n"
        "Niche format mein poora analysis de. Koi asterisk mat use kar. Seedha likho:\n\n"
        "BUSINESS SUMMARY:\n[2-3 lines]\n\n"
        "TARGET AUDIENCE:\n[2-3 lines]\n\n"
        "DEMOGRAPHICS:\n"
        "Age Range: [e.g. 25-44]\n"
        "Gender: [e.g. 65% Female, 35% Male]\n"
        "Income Level: [Low/Middle/Upper-Middle/High]\n"
        "Education: [e.g. Graduate+]\n"
        "Location: [Top cities]\n"
        "Language: [Hindi/English/Both]\n"
        "Marital Status: [Single/Married/Both]\n\n"
        "DEVICE TARGETING:\n"
        "Mobile: [%] - [reason]\n"
        "Desktop: [%] - [reason]\n"
        "Tablet: [%] - [reason]\n"
        "Best Device: [konsa convert karta hai]\n\n"
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
        "Best Days: [days]\n"
        "Peak Hours: [hours]\n"
        "Avoid: [time]\n"
        "Reason: [kyun]\n\n"
        f"BUDGET SPLIT (Total Rs {request.budget}/month):\n"
        "1. [Platform]: Rs [amount] ([%]) - [reason]\n"
        "2. [Platform]: Rs [amount] ([%]) - [reason]\n"
        "3. [Platform]: Rs [amount] ([%]) - [reason]\n\n"
        "CAMPAIGN STRUCTURE:\n"
        "Campaign Name: [naam]\n"
        "Campaign Type: [Search/Display]\n"
        "Bid Strategy: [strategy]\n"
        f"Daily Budget: Rs {request.budget // 30}\n"
        "Target CPA: Rs [amount]\n"
        "Max CPC: Rs [amount]\n"
        "Expected ROAS: [Xx]\n\n"
        "AD GROUPS:\n"
        "1. Group: [naam] | Keywords: [kw1, kw2, kw3, kw4, kw5]\n"
        "2. Group: [naam] | Keywords: [kw1, kw2, kw3, kw4, kw5]\n"
        "3. Group: [naam] | Keywords: [kw1, kw2, kw3, kw4, kw5]\n\n"
        "HEADLINES (20, max 30 chars each):\n"
        "1. [h]\n2. [h]\n3. [h]\n4. [h]\n5. [h]\n"
        "6. [h]\n7. [h]\n8. [h]\n9. [h]\n10. [h]\n"
        "11. [h]\n12. [h]\n13. [h]\n14. [h]\n15. [h]\n"
        "16. [h]\n17. [h]\n18. [h]\n19. [h]\n20. [h]\n\n"
        "DESCRIPTIONS (20, max 90 chars each):\n"
        "1. [d]\n2. [d]\n3. [d]\n4. [d]\n5. [d]\n"
        "6. [d]\n7. [d]\n8. [d]\n9. [d]\n10. [d]\n"
        "11. [d]\n12. [d]\n13. [d]\n14. [d]\n15. [d]\n"
        "16. [d]\n17. [d]\n18. [d]\n19. [d]\n20. [d]\n\n"
        "META AD COPY:\n"
        "Primary Text: [125 chars]\n"
        "Headline: [40 chars]\n"
        "Description: [30 chars]\n"
        "CTA Button: [Learn More/Shop Now/Get Quote]\n\n"
        "INTEREST TARGETING:\n"
        "1. [interest]\n2. [interest]\n3. [interest]\n"
        "4. [interest]\n5. [interest]\n6. [interest]\n\n"
        "REMARKETING STRATEGY:\n"
        "1. [audience 1]\n2. [audience 2]\n3. [audience 3]\n\n"
        "KPI TARGETS:\n"
        "Expected CTR: [X%]\n"
        "Expected CPL: Rs [amount]\n"
        "Expected CPC: Rs [amount]\n"
        "Expected ROAS: [Xx]\n"
        "Expected Conversion Rate: [X%]\n\n"
        "CREATIVE BRIEF:\n"
        "Image Concept: [detailed description]\n"
        "Color Palette: [colors + reason]\n"
        "Video Script (15 sec): [hook, body, CTA]\n"
        "Carousel Slide 1: [kya dikhao]\n"
        "Carousel Slide 2: [kya dikhao]\n"
        "Carousel Slide 3: [kya dikhao]\n\n"
        "AB TESTING PLAN:\n"
        "Test 1: [A] vs [B] - [reason]\n"
        "Test 2: [A] vs [B] - [reason]\n"
        "Test 3: [A] vs [B] - [reason]\n\n"
        "LANDING PAGE SUGGESTIONS:\n"
        "Hero Headline: [headline]\n"
        "Sub Headline: [text]\n"
        "CTA Button: [text]\n"
        "Trust Signals: [what to show]\n\n"
        "NEGATIVE KEYWORDS:\n"
        "1. [kw]\n2. [kw]\n3. [kw]\n4. [kw]\n5. [kw]\n\n"
        "COMMON MISTAKES:\n"
        "1. [mistake + solution]\n"
        "2. [mistake + solution]\n"
        "3. [mistake + solution]\n\n"
        "OPPORTUNITIES:\n"
        "1. [high priority]\n"
        "2. [medium priority]\n"
        "3. [quick win]\n"
    )

    ai_response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=4000
    )

    result = ai_response.choices[0].message.content

    # Database mein save karo
    analysis = AnalysisModel(
        url=request.url,
        business_type=request.business_type,
        budget=request.budget,
        goal=request.goal,
        result=result,
        created_at=datetime.now().strftime("%d %b %Y, %I:%M %p")
    )
    db.add(analysis)
    db.commit()

    return {"success": True, "url": request.url, "analysis": result}


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
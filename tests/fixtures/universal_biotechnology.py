"""
Fixture reconstructing the audited "Universal Biotechnology" report — a real
production report for a reagent/diagnostic-kit distributor, found on audit to
contain every fault this P0 pass fixes. Every P0 validator test imports a
slice of this one dict; per the spec, it must fail every new check.

This is a static snapshot of report *output* (the shape the engine actually
produced), not a request/BI-input fixture — the validators under test all
operate on already-generated report data, not on live GPT calls.
"""

EVIDENCE_TEXT = (
    "Universal Biotechnology is a trusted distribution partner for global "
    "life sciences manufacturers including Thermo Fisher Scientific, Bio-Rad "
    "Laboratories, and Merck Millipore. We supply ELISA kits, antibodies, "
    "PCR reagents, cell culture media, and laboratory consumables to research "
    "institutions and academic labs across India. Our catalogue includes "
    "over 4,000 SKUs of reagents, kits, and disposables sourced from "
    "leading manufacturers, with fast reorder and refill cycles for "
    "regularly-consumed lab supplies."
)

CLIENT_INPUTS = {
    "budget": 20000,
    "target_count": None,
    "target_amount": None,
}

# ── P0.1: numeric claims with no real provenance ────────────────────────────
SECTION_TEXT_WITH_FABRICATED_NUMBERS = """
PRODUCT HIGHLIGHTS:
- ELISA Kits ₹999 Deal! Limited time offer on our most popular research kits.
- Our clients report 40% research efficiency in 3 weeks after switching suppliers.
- We estimate 5-20 competitors serving this category in the region.
- Competitor threat score: 40/100 based on market presence.
- Typical CPC for lab supply keywords ranges ₹15-40, CTR benchmark 2-4%.
"""

# ── P0.2: fabricated case-study/proof claim in an outreach script ──────────
OUTREACH_SCRIPT_WITH_FABRICATED_PROOF = {
    "whatsapp_message_1": "Hi! We noticed your lab sources reagents from multiple vendors — we might be able to simplify that.",
    "whatsapp_message_2_proof": "Hamare ek client ko 40% zyada research efficiency mili bas 3 weeks mein switching to us.",
    "whatsapp_message_3": "Kya kal 10 minute ka call ho sakta hai?",
}

# ── P0.3: the two contradictory media plans, exactly as audited ────────────
SECTION_11_MEDIA_PLAN = {
    "platform_priority": ["LinkedIn", "Google", "Meta"],
    "budget_split": {"LinkedIn": 8000, "Google": 7000, "Meta": 5000},
    "ctr_benchmark": "1.5-2.5%",
    "pause_rule": "CTR <0.5% for 7 days",
}

THIRTEEN_SECTION_MEDIA_PLAN = {
    "platform_priority": ["Google", "LinkedIn", "Remarketing"],
    "budget_split": {"Google": 10000, "LinkedIn": 6000, "Remarketing": 4000},
    "ctr_benchmark": "0.5-1.5%",
    "pause_rule": "CTR <1% for 10 days",
    "min_conversions_per_platform": 50,
    "stated_cpa_range": (200, 500),
    "total_budget": 20000,
}

# ── P0.4: business model misclassification ──────────────────────────────────
WRONG_REVENUE_MODEL = "one_time"  # should be recurring_consumable — reagents/kits/antibodies

# ── P1.1 (not fixed this pass, kept for context/future use): B2C persona on a B2B buyer ──
AUDIENCE_SEGMENTS_DUPLICATED_B2C = [
    {
        "segment_name": "Researchers in Life Sciences",
        "age_range": "25-45",
        "gender": "All",
        "income_level": "Middle",
        "device_usage": "desktop for research, mobile for casual browsing",
    },
    {
        "segment_name": "Academic Institutions and Researchers",
        "age_range": "25-45",
        "gender": "All",
        "income_level": "Middle",
        "device_usage": "desktop for research, mobile for casual browsing",
    },
]

# ── P1.3 (context): suppliers wrongly listed as competitors ────────────────
COMPETITORS_INCLUDING_SUPPLIERS = ["Thermo Fisher Scientific", "Bio-Rad Laboratories", "Merck Millipore"]
SUPPLIERS_NAMED_ON_SITE = ["Thermo Fisher Scientific", "Bio-Rad Laboratories", "Merck Millipore"]

FULL_REPORT_FIXTURE = {
    "url": "https://universalbiotechnology.example.com",
    "business_type": "Reagent & Diagnostic Kit Distribution",
    "evidence_text": EVIDENCE_TEXT,
    "client_inputs": CLIENT_INPUTS,
    "sections": {
        "product_highlights": SECTION_TEXT_WITH_FABRICATED_NUMBERS,
    },
    "outreach_script": OUTREACH_SCRIPT_WITH_FABRICATED_PROOF,
    "media_buying_plan_section_11": SECTION_11_MEDIA_PLAN,
    "media_buying_plan_standalone": THIRTEEN_SECTION_MEDIA_PLAN,
    "revenue_model": WRONG_REVENUE_MODEL,
    "audience_segments": AUDIENCE_SEGMENTS_DUPLICATED_B2C,
    "competitors": COMPETITORS_INCLUDING_SUPPLIERS,
    "suppliers": SUPPLIERS_NAMED_ON_SITE,
    "language": "Hinglish",
}

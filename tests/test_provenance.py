import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from report_validators import extract_numeric_claims, classify_provenance, strip_unproven_claims
from tests.fixtures.universal_biotechnology import (
    SECTION_TEXT_WITH_FABRICATED_NUMBERS, EVIDENCE_TEXT, CLIENT_INPUTS,
)

EMPTY_BENCHMARKS: list[dict] = []
REALISTIC_BENCHMARKS = [
    {"id": "bio-cpc", "industry": "life sciences distribution", "metric": "cpc", "low": 15, "high": 40},
    {"id": "bio-ctr", "industry": "life sciences distribution", "metric": "ctr", "low": 0.5, "high": 3.0},
]


def test_extracts_every_fabricated_number_in_the_fixture():
    claims = extract_numeric_claims(SECTION_TEXT_WITH_FABRICATED_NUMBERS)
    texts = [c.text.replace(" ", "") for c in claims]
    assert any("₹999" in t for t in texts)
    assert any("40%" in t for t in texts)
    assert any("5-20" in t for t in texts)
    assert any("40/100" in t for t in texts)


def test_price_claim_is_blocking_tier():
    claims = extract_numeric_claims(SECTION_TEXT_WITH_FABRICATED_NUMBERS)
    price_claim = next(c for c in claims if "999" in c.text)
    assert price_claim.tier == "BLOCKING"


def test_outcome_percent_claim_is_blocking_tier():
    claims = extract_numeric_claims(SECTION_TEXT_WITH_FABRICATED_NUMBERS)
    outcome_claim = next(c for c in claims if c.kind == "percent" and "40" in c.text)
    assert outcome_claim.tier == "BLOCKING"


def test_competitor_count_is_blocking_tier():
    claims = extract_numeric_claims(SECTION_TEXT_WITH_FABRICATED_NUMBERS)
    count_claim = next(c for c in claims if c.kind == "range" and "5" in c.text)
    assert count_claim.tier == "BLOCKING"


def test_score_is_warn_tier_even_though_sentence_says_competitor():
    """Regression guard for the 'Competitor threat score: 40/100' collision
    — the word 'competitor' appears in the sentence, but the number itself
    is a score (WARN), not a competitor count (BLOCKING)."""
    claims = extract_numeric_claims(SECTION_TEXT_WITH_FABRICATED_NUMBERS)
    score_claim = next(c for c in claims if c.kind == "score")
    assert score_claim.tier == "WARN"


def test_cpc_range_in_rupees_is_warn_not_blocking():
    claims = extract_numeric_claims(SECTION_TEXT_WITH_FABRICATED_NUMBERS)
    cpc_claim = next(c for c in claims if c.kind == "currency" and "15" in c.text)
    assert cpc_claim.tier == "WARN"


def test_none_of_the_fixture_numbers_have_real_provenance():
    """The fixture's own docstring: this must fail every new check."""
    claims = extract_numeric_claims(SECTION_TEXT_WITH_FABRICATED_NUMBERS)
    for claim in claims:
        tag = classify_provenance(claim, EVIDENCE_TEXT, CLIENT_INPUTS, EMPTY_BENCHMARKS)
        assert tag is None, f"claim {claim.text!r} unexpectedly matched provenance {tag}"


def test_a_real_benchmark_row_grants_benchmark_provenance():
    claims = extract_numeric_claims(SECTION_TEXT_WITH_FABRICATED_NUMBERS)
    cpc_claim = next(c for c in claims if c.kind == "currency" and "15" in c.text)
    tag = classify_provenance(cpc_claim, EVIDENCE_TEXT, CLIENT_INPUTS, REALISTIC_BENCHMARKS)
    assert tag is not None
    assert tag.type == "benchmark"


def test_client_input_budget_is_recognized_as_provenance():
    claims = extract_numeric_claims(f"We recommend a budget of ₹{CLIENT_INPUTS['budget']}.")
    budget_claim = next(c for c in claims if c.kind == "currency")
    tag = classify_provenance(budget_claim, EVIDENCE_TEXT, CLIENT_INPUTS, EMPTY_BENCHMARKS)
    assert tag is not None
    assert tag.type == "client_input"
    assert tag.source == "budget"


def test_scraped_evidence_text_is_recognized_as_provenance():
    evidence = "Standard delivery in 2-3 business days, cash-on-delivery available."
    claims = extract_numeric_claims("We deliver in 2-3 business days as a competitive edge.")
    delivery_claim = next(c for c in claims if c.kind == "range")
    tag = classify_provenance(delivery_claim, evidence, CLIENT_INPUTS, EMPTY_BENCHMARKS)
    assert tag is not None
    assert tag.type == "scraped"


def test_strip_removes_blocking_claims_and_leaves_no_placeholder():
    cleaned, removed = strip_unproven_claims(
        "product_highlights", SECTION_TEXT_WITH_FABRICATED_NUMBERS, EVIDENCE_TEXT, CLIENT_INPUTS, EMPTY_BENCHMARKS,
    )
    assert "₹999" not in cleaned
    assert "40% research efficiency" not in cleaned
    assert "5-20 competitors" not in cleaned
    assert "[insert" not in cleaned.lower()
    assert "placeholder" not in cleaned.lower()
    blocking_removed = [r for r in removed if r.tier == "BLOCKING"]
    assert len(blocking_removed) >= 3


def test_strip_keeps_warn_claims_but_adds_caveat():
    cleaned, removed = strip_unproven_claims(
        "product_highlights", SECTION_TEXT_WITH_FABRICATED_NUMBERS, EVIDENCE_TEXT, CLIENT_INPUTS, EMPTY_BENCHMARKS,
    )
    assert "40/100" in cleaned  # WARN-tier score claim survives
    assert "⚠️" in cleaned  # caveat line present
    warn_removed = [r for r in removed if r.tier == "WARN"]
    assert len(warn_removed) >= 1


def test_strip_with_real_benchmarks_leaves_cpc_ctr_unflagged():
    cleaned, removed = strip_unproven_claims(
        "product_highlights", SECTION_TEXT_WITH_FABRICATED_NUMBERS, EVIDENCE_TEXT, CLIENT_INPUTS, REALISTIC_BENCHMARKS,
    )
    assert "₹15-40" in cleaned
    assert "2-4%" in cleaned  # CTR range within the seeded benchmark's ballpark stays, un-caveated for this claim
    cpc_removed = [r for r in removed if "15" in r.original_text and r.tier == "WARN"]
    assert cpc_removed == []

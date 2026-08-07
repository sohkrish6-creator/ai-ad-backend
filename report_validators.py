"""
Pure-logic validators for the report generation engine (P0 correctness pass).

Every function in this module is synchronous, side-effect-free (no DB, no
network, no GPT calls) and operates on plain dicts/strings the caller already
has in hand. Kept out of main.py deliberately so it can be unit-tested in
isolation, without triggering main.py's module-level DB-connection side
effects on import.

main.py imports from this module; it never imports the other way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, Optional

# ─────────────────────────────────────────────────────────────────────────
# P0.1 — Numeric provenance enforcement
# ─────────────────────────────────────────────────────────────────────────

ProvenanceType = Literal["scraped", "client_input", "benchmark"]


@dataclass
class ProvenanceTag:
    type: ProvenanceType
    source: str  # scraped: source URL; client_input: field key; benchmark: benchmark table row id


@dataclass
class NumericClaim:
    text: str            # the exact matched numeric substring, e.g. "₹999", "40%", "5-20"
    sentence: str         # the containing sentence/bullet, for removal
    kind: str             # "currency" | "percent" | "range" | "score"
    tier: Literal["BLOCKING", "WARN"]
    provenance: Optional[ProvenanceTag] = None


# Risk-tier classification is `kind`-first, context-second. `kind` alone
# resolves most cases correctly (a "X/100" is always a score, WARN tier, no
# matter what surrounds it); only "percent" and "range" are genuinely
# ambiguous by shape alone and need the surrounding words to disambiguate.
# Keyword bag-of-words on the whole sentence is deliberately NOT used as the
# first signal — "Competitor threat score: 40/100" contains the substring
# "competitor" (a BLOCKING-tier count word) even though the number itself is
# a score (WARN tier); `kind="score"` short-circuits that collision.
_OUTCOME_CONTEXT = re.compile(
    r"\b(efficiency|result|results|more leads|increase|growth|improvement)\b",
    re.IGNORECASE,
)
_RATE_CONTEXT = re.compile(r"\b(cpc|ctr|cpl|cpa|conversion rate)\b", re.IGNORECASE)
_COMPETITOR_COUNT_CONTEXT = re.compile(r"\bcompetitors?\b", re.IGNORECASE)

_NUMERIC_PATTERNS = [
    ("currency", re.compile(r"₹\s?[\d,]+(?:\.\d+)?(?:\s?-\s?₹?\s?[\d,]+(?:\.\d+)?)?")),
    ("percent", re.compile(r"\d+(?:\.\d+)?\s?%(?:\s?-\s?\d+(?:\.\d+)?\s?%)?|\d+(?:\.\d+)?\s?-\s?\d+(?:\.\d+)?\s?%")),
    ("range", re.compile(r"\b\d+\s?-\s?\d+\b(?!\s?%)")),
    ("score", re.compile(r"\b\d+\s?/\s?100\b")),
]

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?\n])\s+|\n")


def _tier_for(kind: str, sentence: str) -> Literal["BLOCKING", "WARN"]:
    if kind == "score":
        return "WARN"  # scores are explicitly WARN tier regardless of context
    if kind == "currency":
        if _RATE_CONTEXT.search(sentence):
            return "WARN"  # a ₹ CPC/CPL/CPA range, not a product price
        return "BLOCKING"  # a rupee amount elsewhere is a price/budget claim
    if kind == "percent":
        return "BLOCKING" if _OUTCOME_CONTEXT.search(sentence) else "WARN"
    if kind == "range":
        if _COMPETITOR_COUNT_CONTEXT.search(sentence):
            return "BLOCKING"  # "5-20 competitors" — a count claim
        if _RATE_CONTEXT.search(sentence):
            return "WARN"  # a CPC/CTR/CPL/CPA range
        return "BLOCKING"  # unexplained bare range (e.g. market size) — safe default
    return "BLOCKING"


def extract_numeric_claims(text: str) -> list[NumericClaim]:
    """Walk `text`, extract every numeric token, and classify its risk tier.
    Does not assign provenance — that's a separate step (classify_provenance)
    since it needs external data (evidence text, client inputs, benchmarks)
    the extractor doesn't have."""
    if not text:
        return []
    claims: list[NumericClaim] = []
    sentences = [s for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    for sentence in sentences:
        seen_spans: set[tuple[int, int]] = set()
        for kind, pattern in _NUMERIC_PATTERNS:
            for m in pattern.finditer(sentence):
                span = m.span()
                if any(span[0] < e and span[1] > s for s, e in seen_spans):
                    continue  # overlapping match (e.g. a range inside a percent range) — keep the more specific one already found
                seen_spans.add(span)
                tier = _tier_for(kind, sentence)
                claims.append(NumericClaim(text=m.group(0), sentence=sentence.strip(), kind=kind, tier=tier))
    return claims


def classify_provenance(
    claim: NumericClaim,
    evidence_text: str,
    client_inputs: dict,
    benchmarks: list[dict],
) -> Optional[ProvenanceTag]:
    """Independently verify a claim against real data — never trust the
    generator's own say-so. Returns None if no real source backs it."""
    normalized_claim = re.sub(r"\s+", "", claim.text)

    # scraped: does this exact substring appear in the client's own crawled evidence?
    if evidence_text and normalized_claim and normalized_claim in re.sub(r"\s+", "", evidence_text):
        return ProvenanceTag(type="scraped", source="client_site_evidence")

    # client_input: does it match a real field the caller supplied (budget, target_count, etc.)?
    for key, value in (client_inputs or {}).items():
        if value is None:
            continue
        value_str = str(value)
        if value_str and (value_str in claim.text or normalized_claim == re.sub(r"\s+", "", f"₹{value_str}")):
            return ProvenanceTag(type="client_input", source=key)

    # benchmark: does it match a real row in the benchmarks table (low-high range overlap)?
    numbers = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", claim.text)]
    for row in benchmarks or []:
        low, high = row.get("low"), row.get("high")
        if low is None or high is None:
            continue
        if any(low <= n <= high for n in numbers):
            return ProvenanceTag(type="benchmark", source=str(row.get("id", row.get("industry", "unknown"))))

    return None


@dataclass
class SuppressedClaim:
    section: str
    original_text: str
    reason: str
    tier: Literal["BLOCKING", "WARN"]


def strip_unproven_claims(
    section_name: str,
    section_text: str,
    evidence_text: str,
    client_inputs: dict,
    benchmarks: list[dict],
) -> tuple[str, list[SuppressedClaim]]:
    """BLOCKING claims with no provenance: the containing sentence/bullet is
    removed outright (the whole bullet, if stripping the sentence would
    leave it empty/malformed — never a placeholder). WARN claims with no
    provenance: the section keeps the claim but gets an explicit caveat
    line prepended and its confidence is understood to be downgraded by
    the caller (this function reports which claims triggered that via the
    returned list; it does not compute a confidence score itself)."""
    if not section_text:
        return section_text, []

    claims = extract_numeric_claims(section_text)
    removed: list[SuppressedClaim] = []
    warned_sentences: set[str] = set()
    sentences_to_drop: set[str] = set()

    for claim in claims:
        tag = classify_provenance(claim, evidence_text, client_inputs, benchmarks)
        if tag is not None:
            continue
        if claim.tier == "BLOCKING":
            sentences_to_drop.add(claim.sentence)
            removed.append(SuppressedClaim(
                section=section_name, original_text=claim.sentence,
                reason=f"no provenance for {claim.kind} claim {claim.text!r}", tier="BLOCKING",
            ))
        else:
            warned_sentences.add(claim.sentence)
            removed.append(SuppressedClaim(
                section=section_name, original_text=claim.sentence,
                reason=f"no provenance for {claim.kind} claim {claim.text!r}", tier="WARN",
            ))

    if not sentences_to_drop and not warned_sentences:
        return section_text, []

    lines = section_text.split("\n")
    cleaned_lines = []
    for line in lines:
        stripped_line = line
        for sentence in sentences_to_drop:
            if sentence and sentence in stripped_line:
                stripped_line = stripped_line.replace(sentence, "").strip()
        # Drop the whole bullet if removal left it empty or just punctuation/bullet markers.
        residual = re.sub(r"^[\s\-•*]+$", "", stripped_line)
        if line.strip() and not residual.strip():
            continue
        cleaned_lines.append(stripped_line)
    cleaned_text = "\n".join(l for l in cleaned_lines if l.strip() or not l)

    if warned_sentences:
        caveat = "⚠️ Some figures in this section (CPC/CTR/CPL/CPA ranges or scores) could not be verified against real data — treat as directional, not exact."
        cleaned_text = caveat + "\n" + cleaned_text

    return cleaned_text.strip(), removed

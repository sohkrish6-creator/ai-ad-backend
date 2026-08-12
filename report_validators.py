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
# Stem-based, not exact-word — "increased"/"increasing" must match just as
# "increase" does. A live production run surfaced "We increased order volume
# by 25%..." surviving unflagged because an earlier exact-word-only version
# of this regex matched \bincrease\b but not "increased" (no word boundary
# between "increase" and its own "-d" suffix). Every verb here is a stem
# with the suffix made optional, not a closed list of exact forms.
_OUTCOME_CONTEXT = re.compile(
    r"\b(efficien(?:cy|t)|results?|more leads|increas(?:e|ed|es|ing)|growth|grow(?:s|ing)?|grew|grown|"
    r"improv(?:e|ed|es|ing|ement)|boost(?:s|ed|ing)?|gain(?:s|ed|ing)?|"
    r"driv(?:e|es|ing)|drove|driven|deliver(?:s|ed|ing)?|achiev(?:e|es|ed|ing)|"
    r"expand(?:s|ed|ing)?|doubl(?:e|ed|es|ing)|tripl(?:e|ed|es|ing)|multipl(?:y|ied|ies|ying)|"
    r"generat(?:e|ed|es|ing))\b",
    re.IGNORECASE,
)
_RATE_CONTEXT = re.compile(r"\b(cpc|ctr|cpl|cpa|conversion rate)\b", re.IGNORECASE)
_COMPETITOR_COUNT_CONTEXT = re.compile(r"\bcompetitors?\b", re.IGNORECASE)

_NUMERIC_PATTERNS = [
    ("currency", re.compile(r"₹\s?[\d,]+(?:\.\d+)?(?:\s?-\s?₹?\s?[\d,]+(?:\.\d+)?)?")),
    ("percent", re.compile(r"\d+(?:\.\d+)?\s?%(?:\s?-\s?\d+(?:\.\d+)?\s?%)?|\d+(?:\.\d+)?\s?-\s?\d+(?:\.\d+)?\s?%")),
    ("range", re.compile(r"\b\d+\s?-\s?\d+\b(?!\s?%)")),
    ("score", re.compile(r"\b\d+\s?/\s?100\b")),
    # "300+ institutional orders", "50+ clients" — a bare inflated-round-number
    # count claim. Deliberately narrow (requires the literal "+") so ordinary
    # numbers ("30 days", "10 minute call", a year) aren't false-positived.
    ("count_plus", re.compile(r"\b\d+\+")),
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

    # benchmark: does it match a real row in the benchmarks table? Requires
    # BOTH a real industry-scoped row (callers pass [] when no industry
    # matched — see main.py's _get_industry_benchmarks) AND the claim's own
    # shape to be compatible with that row's metric unit, not just a
    # coincidental numeric-range overlap. A live smoke test caught a
    # fabricated "30% research efficiency" claim getting waved through
    # against an unrelated CPC row that happened to span 10-45 — without
    # this shape check, a percent claim could match a rupee-denominated
    # metric purely by luck.
    _kind_to_metrics = {"percent": {"ctr"}, "currency": {"cpc", "cpl"}}
    _acceptable_metrics = _kind_to_metrics.get(claim.kind)
    if _acceptable_metrics:
        numbers = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", claim.text)]
        for row in benchmarks or []:
            if row.get("metric") not in _acceptable_metrics:
                continue
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


# ─────────────────────────────────────────────────────────────────────────
# P0.4 — Business model taxonomy fix
# ─────────────────────────────────────────────────────────────────────────

PurchaseType = Literal["one_time", "recurring_consumable", "subscription", "project_based"]

# Deliberately conservative and specific to consumable/reagent-style
# goods — mirrors _VOICE_CHAIN_BRAND_KEYWORDS's "small, targeted keyword
# net as a secondary signal" style. A distributor selling reagents/kits
# is a repeat-order consumables business regardless of what a one-shot
# GPT classification call decides; this override always wins on a match.
RECURRING_KEYWORDS = (
    "reagent", "kit", "consumable", "refill", "cartridge", "supplies",
    "disposables", "media", "buffer", "antibody",
)


def check_purchase_type_override(evidence_text: str) -> Optional[str]:
    """Pure keyword check over the client's own scraped evidence text.
    Returns "recurring_consumable" on any match, else None — never
    guesses any other purchase type from keywords alone, since only the
    recurring-consumable misclassification was the confirmed, systemic
    fault (equipment/SaaS/services still need the model's judgment)."""
    if not evidence_text:
        return None
    text_lower = evidence_text.lower()
    matched = [kw for kw in RECURRING_KEYWORDS if kw in text_lower]
    return "recurring_consumable" if matched else None


# Maps the model's free-text revenue_model output onto the fixed enum, in
# case the prompt's own instruction isn't followed exactly (defense in
# depth, not the primary mechanism — the prompt itself asks for the new
# enum directly).
_REVENUE_MODEL_MAP = {
    "one-time": "one_time", "one_time": "one_time", "onetime": "one_time",
    "subscription": "subscription", "freemium": "subscription",
    "commission": "project_based", "project-based": "project_based",
    "project_based": "project_based", "service": "project_based",
    "recurring": "recurring_consumable", "recurring_consumable": "recurring_consumable",
    "recurring consumable": "recurring_consumable",
}


def normalize_purchase_type(raw_value: str) -> PurchaseType:
    """Best-effort mapping of a freeform model output to the fixed enum.
    "Mixed" and anything unrecognized default to project_based — the
    least presumptive bucket (no retention module is force-activated on
    an ambiguous guess; recurring_consumable/subscription only ever get
    set by an explicit model answer or the keyword override)."""
    key = (raw_value or "").strip().lower()
    return _REVENUE_MODEL_MAP.get(key, "project_based")


def assert_retention_budget(media_plan: dict, revenue_model: str) -> Optional[str]:
    """A recurring-revenue business (recurring_consumable/subscription)
    whose media plan allocates 100% to acquisition — no existing-customer/
    retention budget line at all — fails validation. Returns an error
    string on failure, None if the plan is fine (either the business isn't
    recurring, or a retention line is genuinely present)."""
    if revenue_model not in ("recurring_consumable", "subscription"):
        return None
    budget_text = (media_plan or {}).get("budget_allocation", "") or ""
    if not budget_text.strip():
        return "recurring-revenue business but budget_allocation is empty — no retention line possible"
    if not re.search(r"\b(retention|reactivation|existing customer|repeat order|reorder|win-back|winback)\b",
                      budget_text, re.IGNORECASE):
        return "recurring-revenue business but budget_allocation has no existing-customer/retention line — 100% acquisition"
    return None

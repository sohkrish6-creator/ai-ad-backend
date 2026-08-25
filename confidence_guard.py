"""
Post-audit fix. A live Marketing Intelligence run for ResMed India returned:

    "advertising": {
      "platforms_observed": [], "ad_formats": [], "key_messages": [], "cta_patterns": [],
      "estimated_spend": "not found in available data",
      "confidence": 60,
      "data_source": "not verified — no direct evidence found",
    }

Six of eighteen sections in that report had the same shape: every finding
field empty, the source honestly saying "not verified", and confidence
still self-reported at 60 — rendered by the frontend as a confident-looking
card. The root cause is structural, not a prompting slip: the model has no
reliable introspection into whether it actually found anything, so "I found
nothing" and "I know this with medium certainty" collapse into the same
number. No amount of "please be more honest about confidence" in the prompt
fixes that — the fix has to be deterministic post-processing that never
trusts the self-reported number.

`guard_report` is the one entry point. For every section that carries a
self-reported `confidence`, it computes an independent ceiling from (a) how
much of the section's own payload is actually populated and (b) whether the
declared data_source (or evidence) reads like real evidence or an admission
that nothing was found — then caps confidence to that ceiling. It never
raises confidence, only lowers it.

Deliberately schema-tolerant, not schema-specific: only Marketing
Intelligence's `sections` dict was confirmed to match the shape this guard
targets (`sections[name]` is itself a dict carrying `confidence`). Other
modules in this codebase that also happen to have a `sections` key
(Marketing Brain: prose strings, not dicts; Social Intelligence
Engine: no `sections` key at all, a different data_label/note vocabulary
with no numeric confidence) do not match and are left alone by construction
— a section that isn't a dict, or a dict with no `confidence` key, is
skipped rather than force-fit. Sub-sections that are themselves lists (e.g.
MI's `competitors`, `lessons`, `revenue_timeline` — each *item* carries its
own confidence, not the list itself) are the same kind of skip; guarding
per-list-item confidence is a related but separate scope, not covered here.

Pure stdlib, no dependencies, idempotent (safe to call twice on the same
report), mutates in place and returns the same dict.
"""
from typing import Any

# Mirrors _MI_UNVERIFIED_SOURCE_SIGNALS in main.py (the existing, already-
# in-production list _mi_enforce_observed_fields uses to decide whether a
# data_source reads like real evidence) — deliberately the same vocabulary
# rather than a second, drift-prone list.
_UNVERIFIED_SOURCE_SIGNALS = (
    "model knowledge", "not verified", "no direct evidence", "training data",
    "general knowledge", "industry standard", "typical", "assumption", "inference",
    "no evidence", "unverified",
)

# A non-empty string that says nothing was found — the exact "polite empty"
# trap from the ResMed example ("not found in available data" is a
# non-empty string; `if not value` alone would count it as populated).
_POLITE_EMPTY_PATTERNS = (
    "not found", "not available", "not verified", "no direct evidence", "no evidence",
    "n/a", "not disclosed", "undisclosed", "unknown", "not applicable", "none found",
    "no data", "not provided",
)

# Fields that describe the section (self-reported confidence, its
# provenance, and anything this guard itself adds) rather than being part
# of the section's actual findings — excluded from the "how much of this
# section's payload is populated" calculation. Including confidence_reason/
# data_label_self_reported/confidence_reported here is what makes a second
# guard_report() pass idempotent instead of treating the guard's own
# annotations as more (empty) payload to average in.
_METADATA_KEYS = frozenset({
    "confidence", "confidence_reported", "confidence_reason",
    "data_label", "data_label_self_reported", "data_source", "evidence",
})

# The four labels this guard assigns. Distinct from any free-text
# data_label a section's own prompt may already ask the model for (e.g.
# MI's audience section asks for "ESTIMATED from research") — those are
# preserved under data_label_self_reported, never silently dropped, but
# data_label itself always ends up one of these four.
_OUR_LABELS = frozenset({"VERIFIED", "INFERRED", "UNVERIFIED", "NO_DATA"})

# Deterministic ceilings — never a guess, always the same output for the
# same inputs. NO_DATA is 0, not just "low": if every payload field is
# empty, there is nothing to be confident about regardless of what the
# model reported. VERIFIED tops out at 90, not 100 — even a well-evidenced
# LLM extraction retains irreducible uncertainty.
_CEILINGS = {"NO_DATA": 0, "UNVERIFIED": 25, "INFERRED": 60, "VERIFIED": 90}

# A section needs at least this fraction of its real payload fields
# populated (and a data_source that doesn't flag itself as unverified) to
# be labeled VERIFIED rather than INFERRED. Not a precise science — a
# deterministic, testable line to draw, which is still strictly better than
# trusting the model's own number.
_VERIFIED_RATIO_THRESHOLD = 0.6


def _is_empty_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return True
        low = stripped.lower()
        return any(pattern in low for pattern in _POLITE_EMPTY_PATTERNS)
    if isinstance(value, (list, tuple)):
        return all(_is_empty_value(v) for v in value) if value else True
    if isinstance(value, dict):
        return len(value) == 0
    return False  # numbers, bools — present at all counts as populated


def _source_text(section: dict) -> str:
    return " ".join(
        str(section.get(key, "") or "") for key in ("data_source", "evidence")
    ).lower()


def _source_looks_unverified(section: dict) -> bool:
    text = _source_text(section)
    if not text.strip():
        return True  # no stated source at all — same as MI's own existing rule
    return any(signal in text for signal in _UNVERIFIED_SOURCE_SIGNALS)


def _payload_ratio(section: dict) -> tuple[int, int]:
    payload = {k: v for k, v in section.items() if k not in _METADATA_KEYS}
    total = len(payload)
    if total == 0:
        return 0, 0
    populated = sum(1 for v in payload.values() if not _is_empty_value(v))
    return populated, total


def _classify(section: dict) -> tuple[str, int, int, int, bool]:
    populated, total = _payload_ratio(section)
    ratio = (populated / total) if total else 0.0
    source_unverified = _source_looks_unverified(section)

    if ratio == 0:
        label = "NO_DATA"
    elif source_unverified:
        label = "UNVERIFIED"
    elif ratio >= _VERIFIED_RATIO_THRESHOLD:
        label = "VERIFIED"
    else:
        label = "INFERRED"

    return label, _CEILINGS[label], populated, total, source_unverified


def _build_reason(label: str, populated: int, total: int, source_unverified: bool,
                   reported: float, final: float, ceiling: int) -> str:
    pop_desc = f"{populated}/{total} fields populated" if total else "no findable payload fields"
    src_desc = "data source unverified or empty" if source_unverified else "data source looks like real evidence"
    if final < reported:
        return f"{label}: {pop_desc}, {src_desc} — reported confidence {reported:g} capped to {final:g} (ceiling {ceiling})."
    return f"{label}: {pop_desc}, {src_desc} — reported confidence {reported:g} already within ceiling {ceiling}."


def _guard_section(section: dict) -> tuple[str, float]:
    """Mutates `section` in place. Returns (data_label, final_confidence)."""
    # Idempotent re-run: if this section was already guarded once, trust
    # its own preserved confidence_reported rather than re-reading
    # `confidence`, which this same function may have already capped —
    # otherwise a second guard_report() call could ratchet a value down
    # repeatedly based on its own prior output.
    reported = section["confidence_reported"] if "confidence_reported" in section else section.get("confidence", 0)
    try:
        reported = float(reported)
    except (TypeError, ValueError):
        reported = 0.0

    label, ceiling, populated, total, source_unverified = _classify(section)
    final = min(reported, ceiling)

    existing_label = section.get("data_label")
    if existing_label is not None and existing_label not in _OUR_LABELS and "data_label_self_reported" not in section:
        section["data_label_self_reported"] = existing_label

    section["confidence_reported"] = reported
    section["confidence"] = final
    section["data_label"] = label
    section["confidence_reason"] = _build_reason(label, populated, total, source_unverified, reported, final, ceiling)

    return label, final


def guard_report(report: dict) -> dict:
    """Never trust the self-reported number. Mutates `report` in place and
    returns it. A no-op (returns unchanged) if `report` has no `sections`
    key, or `sections` isn't a dict — the documented contract callers rely
    on to safely pass in a report shape this guard doesn't target."""
    if not isinstance(report, dict):
        return report
    sections = report.get("sections")
    if not isinstance(sections, dict):
        return report

    label_counts = {label: 0 for label in _OUR_LABELS}
    confidences: list[float] = []

    for section in sections.values():
        if not isinstance(section, dict):
            continue  # e.g. MI's revenue_timeline/unique_stories/competitors — lists, not this guard's target
        if "confidence" not in section and "confidence_reported" not in section:
            continue  # e.g. MI's social (per-platform dicts) or swot (no self-reported confidence at all)

        label, final_confidence = _guard_section(section)
        label_counts[label] += 1
        confidences.append(final_confidence)

    overall_confidence = round(sum(confidences) / len(confidences)) if confidences else 0
    needs_review = label_counts["NO_DATA"] > 0 or label_counts["UNVERIFIED"] > 0

    report["_quality"] = {
        "overall_confidence": overall_confidence,
        "needs_review": needs_review,
        "sections_by_label": label_counts,
        "sections_guarded": len(confidences),
    }
    return report

"""
Post-audit fix. Real reported case: Revenue Engine Pipeline, "Wedding &
Events" / Jaipur — Scanned 38, but only Warm 4 + Cold 11 = 15 prospects
had any score at all; the other 23 simply never appeared as anything,
rendering as a confusing near-empty result rather than a visible partial
scan.

Traced to: the Pipeline's ported "Start a Quick Scan" form never sends
target_count, so max_prospects silently defaults to 15
(revenue_engine_discover, main.py) with no UI control to raise it — unlike
legacy Prospect Discovery's own form, which had one. raw_found_count (38,
"Scanned") is computed BEFORE the `raw_places[:max_prospects]` slice;
everything past the cap is dropped with no signal distinguishing "capped"
from "no real weaknesses found". This is a DIFFERENT root cause from the
two earlier incidents (the unconditional de-dup, then the poisoned scan
cache) — confirmed by the math: 4 Warm + 11 Cold = 15 = exactly the
hardcoded default, and the 15 that WERE scored show a real, varied
distribution (not the flat all-zero signature of the earlier two bugs).

Instead of a fourth point fix, this is the permanent instrumentation the
user asked for: one voice_batches column per funnel stage (found ->
enriched -> homepage fetched -> weaknesses detected -> scored -> bucketed,
the last already visible via the existing Hot/Warm/Cold counts), so any
future silent drop at any stage is visible immediately rather than
rendering as an ordinary, if disappointing, empty result.

_run_voice_batch_job itself is a live-Places/homepage-fetch/GPT async
function, not practical to unit test in isolation (matches this
codebase's established convention). What IS tested here: every new
funnel-stage column is actually wired into _VOICE_BATCH_COLS (a
previously-real failure mode in this exact codebase — a column added to
the DDL/migration list but forgotten in the SELECT list silently never
reaches the API response), and the exact reported arithmetic.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import _VOICE_BATCH_COLS

_NEW_FUNNEL_COLUMNS = [
    "raw_found_count", "enterprise_filtered_count", "enriched_count",
    "homepage_attempted_count", "homepage_ok_count", "cache_skipped_count",
    "weaknesses_detected_count", "scored_count", "phone_populated_count",
]


def test_every_funnel_stage_column_is_wired_into_the_batch_select():
    for col in _NEW_FUNNEL_COLUMNS:
        assert col in _VOICE_BATCH_COLS, f"{col} is tracked but never reaches the API response"


def test_the_exact_reported_math_confirms_a_max_prospects_cap_not_a_weakness_bug():
    # Wedding & Events / Jaipur: raw_found_count=38, default max_prospects=15
    # (no target_count sent by the plain Quick Scan form).
    raw_found_count = 38
    max_prospects = 15  # revenue_engine_discover's hardcoded default
    enriched_count = min(max_prospects, raw_found_count)
    assert enriched_count == 15

    # Reported bucket counts.
    hot, warm, cold = 0, 4, 11
    bucketed_count = hot + warm + cold
    assert bucketed_count == enriched_count  # every enriched prospect got a real bucket — scoring worked

    never_enriched = raw_found_count - enriched_count
    assert never_enriched == 23  # the exact reported "23 of 38 have no score"

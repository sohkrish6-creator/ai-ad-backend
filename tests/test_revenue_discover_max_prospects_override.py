"""
Post-audit fix. The plain "Start a Quick Scan" form had no way to raise the
15-prospect default at all — real case (Wedding & Events / Jaipur):
raw_found_count=38, only the first 15 ever enriched/scored, no UI control to
ask for more (unlike legacy Prospect Discovery's own form). target_count
already means something else for goal_type='revenue' (desired client count,
padded 3x into a scan size) — max_prospects is a new, separate, explicit
"scan exactly this many" override that takes priority over that padding math
so a plain segment scan doesn't have to reverse-engineer it.

revenue_engine_discover's sizing logic is inline in an async endpoint (not a
standalone function) — this test replicates its exact precedence/bounds math,
matching this codebase's precedent (test_scan_funnel_diagnostics.py) for
testing arithmetic that lives inside a live-API async handler.
"""


def _resolve_max_prospects(max_prospects=None, target_count=None, target_amount=None,
                            avg_deal_micros=20_000_000_000, goal_type="segment"):
    resolved = 15
    if max_prospects:
        resolved = min(50, max(5, max_prospects))
    elif target_count:
        resolved = min(50, max(5, target_count * 3))
    elif goal_type == "revenue" and target_amount:
        target_micros = target_amount * 1_000_000
        required_clients = -(-int(target_micros) // int(avg_deal_micros))
        resolved = min(50, max(15, required_clients * 5))
    return resolved


def test_plain_quick_scan_with_no_overrides_still_defaults_to_15():
    assert _resolve_max_prospects() == 15


def test_explicit_max_prospects_override_is_honored():
    assert _resolve_max_prospects(max_prospects=40) == 40


def test_explicit_max_prospects_is_bounded_to_50():
    assert _resolve_max_prospects(max_prospects=999) == 50


def test_explicit_max_prospects_is_floored_to_5():
    assert _resolve_max_prospects(max_prospects=1) == 5


def test_explicit_max_prospects_takes_priority_over_target_count_padding():
    # target_count=10 alone would resolve to 30 (10*3) — an explicit
    # max_prospects must win outright, not be overridden by it.
    assert _resolve_max_prospects(max_prospects=45, target_count=10) == 45


def test_target_count_padding_still_works_when_no_override_given():
    assert _resolve_max_prospects(target_count=10) == 30

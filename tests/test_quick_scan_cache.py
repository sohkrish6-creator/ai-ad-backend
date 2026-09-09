"""
Post-audit fix. _quick_scan_cache_lookup (Revenue Engine's "skip re-scanning
a business already scanned within 7 days" cache) had no filter excluding
enterprise/chain-filtered rows or otherwise-incomplete rows. Filtered rows
never go through weakness detection at all (filtering happens before it),
so weaknesses_json is NULL on them.

Live-reported symptom this caused: a real batch (Food & Beverage / Jaipur)
showed every prospect as "none detected" / Opportunity 0 / Need 0 /
recommendation IGNORE — the same businesses legacy Prospect Discovery
scores 88-92 with a real "No Website" gap. Root cause: an EARLIER, unrelated
scan (Google Places Text Search terms overlap between industries — e.g.
"hotels" and "food & beverage" both search "restaurants"/"cafes") had
enterprise-filtered one of these same place_ids, and the cache lookup
picked up that filtered row as the "most recent", read its NULL
weaknesses_json back as "[]", and skipped fresh detection entirely — every
downstream number then legitimately computed to zero from a real (but
wrong) absence of weaknesses.

Real DB-touching test — the bug was a missing WHERE clause in real SQL,
so this is the correct level to guard it at.
"""
import sys
import os
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from main import engine, _quick_scan_cache_lookup

TEST_UID = "test-uid-quick-scan-cache"

# _QUICK_SCAN_CACHE_DAYS is a 7-day rolling window from "now" — a fixed
# calendar date (e.g. "2026-01-01") would silently fall outside that
# window depending on when the suite runs. RECENT/OLDER are both inside
# it; OLDER is still earlier than RECENT for the "most recent wins" tests.
RECENT = datetime.utcnow().isoformat()
OLDER = (datetime.utcnow() - timedelta(days=1)).isoformat()


def setup_function():
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM voice_prospects WHERE user_id=:uid"), {"uid": TEST_UID})


def teardown_function():
    setup_function()


def _insert_filtered_row(place_id, ts):
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO voice_prospects (batch_id, user_id, place_id, business_name, approval_status, "
            "filter_reason, created_at) "
            "VALUES ('other-batch', :uid, :pid, 'Some Chain Hotel', 'filtered', 'enterprise_filtered: test', :ts)"
        ), {"uid": TEST_UID, "pid": place_id, "ts": ts})


def _insert_scored_row(place_id, weaknesses_json, ts, opportunity_score=88):
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO voice_prospects (batch_id, user_id, place_id, business_name, approval_status, "
            "weaknesses_json, evidence_json, opportunity_score, priority, created_at, scanned_at) "
            "VALUES ('other-batch', :uid, :pid, 'Baker in town', 'pending', :wj, '[]', :score, 'high', :ts, :ts)"
        ), {"uid": TEST_UID, "pid": place_id, "wj": weaknesses_json, "score": opportunity_score, "ts": ts})


def test_a_filtered_row_never_poisons_the_cache():
    _insert_filtered_row("place-baker", RECENT)
    result = _quick_scan_cache_lookup(TEST_UID, ["place-baker"])
    assert "place-baker" not in result


def test_the_exact_reported_scenario_filtered_row_from_an_unrelated_earlier_scan():
    # "Baker in town"-equivalent: enterprise-filtered in an EARLIER,
    # unrelated search (e.g. "hotels in Jaipur", whose search terms
    # overlap with "Food & Beverage"'s "restaurants"/"cafes") — a later,
    # completely different scan for this place_id must not be told "this
    # was already scanned, skip it" using that filtered row's NULL
    # weaknesses.
    _insert_filtered_row("place-baker", RECENT)
    result = _quick_scan_cache_lookup(TEST_UID, ["place-baker"])
    assert result == {}  # not a cache hit — the caller will run fresh detection


def test_a_real_scored_row_still_hits_the_cache_normally():
    _insert_scored_row("place-real", '["no_website"]', RECENT)
    result = _quick_scan_cache_lookup(TEST_UID, ["place-real"])
    assert "place-real" in result
    assert result["place-real"]["weaknesses_json"] == '["no_website"]'
    assert result["place-real"]["opportunity_score"] == 88


def test_a_filtered_row_and_a_real_row_for_different_places_dont_interfere():
    _insert_filtered_row("place-filtered", RECENT)
    _insert_scored_row("place-real", '["no_website"]', RECENT)
    result = _quick_scan_cache_lookup(TEST_UID, ["place-filtered", "place-real"])
    assert "place-filtered" not in result
    assert "place-real" in result


def test_a_newer_filtered_row_does_not_shadow_an_older_real_row():
    # The WHERE clause excludes filtered/incomplete rows entirely (not just
    # de-prioritizes them) — so if a business was scored for real once,
    # then later filtered in an unrelated scan, the older real row still
    # qualifies and wins, rather than the newer filtered row blanking out
    # real, still-valid data within the cache window.
    _insert_scored_row("place-x", '["no_website"]', OLDER)
    _insert_filtered_row("place-x", RECENT)
    result = _quick_scan_cache_lookup(TEST_UID, ["place-x"])
    assert "place-x" in result
    assert result["place-x"]["weaknesses_json"] == '["no_website"]'

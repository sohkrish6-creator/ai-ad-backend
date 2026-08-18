"""
Post-audit fix. Prospect Discovery had two gaps:
  1. Only the single most recent scan was cached (prospect_memory is
     UNIQUE(business_key), upserted) — an earlier scan was gone the moment
     a new one ran, with no way to browse history.
  2. Re-running the same query returned identical businesses every time,
     because Google Places' ranking for a fixed query+location is stable
     — nothing tracked what had already been surfaced, so there was no
     way to tell "new" from "seen before".

_prospect_get_previously_discovered_place_ids and _prospect_save_scan_history
are DB-coupled by design (real SQL, matching this codebase's established
"pure logic gets pure tests, DB-touching logic gets a real local-SQLite
integration test" split — see test_case_study_guard.py's own docstring on
why). Tested here against the real local SQLite fallback DB.
"""
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from main import (
    _prospect_get_previously_discovered_place_ids,
    _prospect_save_scan_history,
    engine,
)

TEST_KEY = "test-uid::dentist::jaipur"
OTHER_KEY = "test-uid::dentist::mumbai"


def _cleanup():
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM prospect_scan_history WHERE business_key IN (:a, :b)"), {"a": TEST_KEY, "b": OTHER_KEY})


def setup_function():
    _cleanup()


def teardown_function():
    _cleanup()


def test_empty_history_returns_empty_set():
    assert _prospect_get_previously_discovered_place_ids(TEST_KEY) == set()


def test_saved_scan_is_returned_on_next_lookup():
    _prospect_save_scan_history(
        TEST_KEY, "dentist", "Jaipur", "dentist in Jaipur", 15,
        10, 10, 0, ["place_a", "place_b", "place_c"], {"prospects": []},
    )
    seen = _prospect_get_previously_discovered_place_ids(TEST_KEY)
    assert seen == {"place_a", "place_b", "place_c"}


def test_multiple_scans_union_their_place_ids():
    _prospect_save_scan_history(TEST_KEY, "dentist", "Jaipur", "q1", 15, 5, 5, 0, ["p1", "p2"], {})
    _prospect_save_scan_history(TEST_KEY, "dentist", "Jaipur", "q2", 15, 5, 5, 0, ["p2", "p3"], {})
    seen = _prospect_get_previously_discovered_place_ids(TEST_KEY)
    assert seen == {"p1", "p2", "p3"}  # p2 seen in both scans, still just one entry


def test_different_business_key_does_not_leak_across():
    _prospect_save_scan_history(TEST_KEY, "dentist", "Jaipur", "q", 15, 3, 3, 0, ["only_in_jaipur"], {})
    _prospect_save_scan_history(OTHER_KEY, "dentist", "Mumbai", "q", 15, 3, 3, 0, ["only_in_mumbai"], {})
    assert _prospect_get_previously_discovered_place_ids(TEST_KEY) == {"only_in_jaipur"}
    assert _prospect_get_previously_discovered_place_ids(OTHER_KEY) == {"only_in_mumbai"}


def test_result_data_round_trips_through_json():
    payload = {"prospects": [{"name": "Acme Dental", "opportunity_score": 88}], "total_found": 1}
    _prospect_save_scan_history(TEST_KEY, "dentist", "Jaipur", "q", 15, 1, 1, 0, ["p1"], payload)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT result_data FROM prospect_scan_history WHERE business_key = :bk"),
            {"bk": TEST_KEY},
        ).first()
    assert json.loads(row[0]) == payload


def test_malformed_place_ids_row_is_skipped_not_fatal():
    # Simulates a corrupted/legacy row — the union must not blow up on one
    # bad row and lose every other scan's real data.
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO prospect_scan_history (business_key, place_ids, created_at) "
            "VALUES (:bk, :pids, :ca)"
        ), {"bk": TEST_KEY, "pids": "not valid json", "ca": "2026-01-01"})
    _prospect_save_scan_history(TEST_KEY, "dentist", "Jaipur", "q", 15, 1, 1, 0, ["good_one"], {})
    assert _prospect_get_previously_discovered_place_ids(TEST_KEY) == {"good_one"}

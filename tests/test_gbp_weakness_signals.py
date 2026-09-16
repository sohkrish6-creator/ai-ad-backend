"""
Post-audit fix. Real reported case: Salon & Beauty / Jaipur (and Healthcare
& Clinics / Jaipur before it) — the overwhelming majority of small Indian
local businesses (clinics, salons, cafes, preschools) have no website at
all. Before this fix, a websiteless business had exactly ONE detectable
weakness (no_website) and nothing else — every downstream signal in
_detect_voice_weaknesses lived inside `if website:`, so a website-less
business could never accumulate enough real evidence to look like a strong
opportunity, no matter how many other real gaps it had.

_detect_voice_weaknesses now reads Google Business Profile signals that
exist for every business regardless of website: rating/review count vs.
this scan's own peer average, photo count, presence of hours, presence of
a description. All from data already fetched via Place Details for every
business — real, not inferred, never requiring a website. Peer thresholds
only engage once the batch has _VOICE_PEER_MIN_SAMPLE+ businesses (a
tiny batch's own average is too noisy to be a fair baseline).

Pure function, no I/O — real unit tests, no mocking, per this codebase's
convention.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import _detect_voice_weaknesses, _voice_compute_peer_stats, _compute_need_score

_EMPTY_FETCH = {"html": "", "fetch_status": "ok", "detail": ""}


def _prospect(**overrides):
    base = {
        "website": "", "google_rating": 4.5, "total_reviews": 50,
        "business_status": "OPERATIONAL", "photo_count": 10,
        "has_hours": True, "has_description": True,
    }
    base.update(overrides)
    return base


# ── websiteless businesses get real signals beyond no_website ───────────────

def test_websiteless_business_with_no_other_gaps_still_only_shows_no_website():
    weaknesses, _ = _detect_voice_weaknesses(_prospect(), _EMPTY_FETCH)
    assert weaknesses == ["no_website"]


def test_websiteless_business_with_few_photos_gets_a_second_real_signal():
    weaknesses, _ = _detect_voice_weaknesses(_prospect(photo_count=1), _EMPTY_FETCH)
    assert "no_website" in weaknesses
    assert "few_photos" in weaknesses


def test_websiteless_business_missing_hours_and_description_stacks_signals():
    weaknesses, _ = _detect_voice_weaknesses(
        _prospect(has_hours=False, has_description=False), _EMPTY_FETCH,
    )
    assert set(weaknesses) == {"no_website", "missing_hours", "missing_description"}


def test_a_business_can_now_reach_several_real_signals_with_no_website_at_all():
    # The exact shape the reported case needs: a websiteless business with
    # real, independently-detected gaps across every GBP dimension.
    weaknesses, evidence = _detect_voice_weaknesses(
        _prospect(photo_count=0, has_hours=False, has_description=False, total_reviews=3),
        _EMPTY_FETCH,
    )
    assert set(weaknesses) == {
        "no_website", "few_photos", "missing_hours", "missing_description", "low_review_count",
    }
    assert len(evidence) == len(weaknesses)
    need = _compute_need_score(weaknesses, evidence)
    # Was capped near ~29 (no_website alone) before this fix; now several
    # independent real signals should push it well past that.
    assert need > 60


# ── field-absent (no I/O ever ran) must never be treated as a weakness ──────

def test_photo_count_absent_entirely_is_not_treated_as_few_photos():
    p = _prospect()
    del p["photo_count"]
    weaknesses, _ = _detect_voice_weaknesses(p, _EMPTY_FETCH)
    assert "few_photos" not in weaknesses


def test_has_hours_absent_entirely_is_not_treated_as_missing_hours():
    p = _prospect()
    del p["has_hours"]
    weaknesses, _ = _detect_voice_weaknesses(p, _EMPTY_FETCH)
    assert "missing_hours" not in weaknesses


def test_has_description_absent_entirely_is_not_treated_as_missing():
    p = _prospect()
    del p["has_description"]
    weaknesses, _ = _detect_voice_weaknesses(p, _EMPTY_FETCH)
    assert "missing_description" not in weaknesses


# ── peer comparison ───────────────────────────────────────────────────────

def test_peer_stats_needs_minimum_sample_before_engaging():
    tiny_batch = [{"google_rating": 4.9, "total_reviews": 500}] * 2
    stats = _voice_compute_peer_stats(tiny_batch)
    weaknesses, _ = _detect_voice_weaknesses(
        _prospect(google_rating=3.0, total_reviews=1), _EMPTY_FETCH, stats,
    )
    assert "below_peer_rating" not in weaknesses
    assert "below_peer_review_count" not in weaknesses


def test_below_peer_rating_and_review_count_fire_with_a_real_sample():
    peers = [{"google_rating": 4.8, "total_reviews": 200} for _ in range(6)]
    stats = _voice_compute_peer_stats(peers)
    weaknesses, evidence = _detect_voice_weaknesses(
        _prospect(google_rating=3.9, total_reviews=30), _EMPTY_FETCH, stats,
    )
    assert "below_peer_rating" in weaknesses
    assert "below_peer_review_count" in weaknesses
    ev = {e["type"]: e for e in evidence}
    assert "4.8" in ev["below_peer_rating"]["value"]
    assert "200" in ev["below_peer_review_count"]["value"]


def test_at_or_above_peer_average_does_not_fire():
    peers = [{"google_rating": 4.0, "total_reviews": 40} for _ in range(6)]
    stats = _voice_compute_peer_stats(peers)
    weaknesses, _ = _detect_voice_weaknesses(
        _prospect(google_rating=4.0, total_reviews=40), _EMPTY_FETCH, stats,
    )
    assert "below_peer_rating" not in weaknesses
    assert "below_peer_review_count" not in weaknesses


def test_peer_stats_ignores_businesses_missing_rating_or_reviews():
    peers = [{"google_rating": None, "total_reviews": None}, {"google_rating": 4.5, "total_reviews": 100}]
    stats = _voice_compute_peer_stats(peers)
    assert stats["avg_rating"] == 4.5
    assert stats["avg_reviews"] == 100


def test_peer_stats_on_empty_batch_is_safe():
    stats = _voice_compute_peer_stats([])
    assert stats == {"avg_rating": None, "avg_reviews": None, "sample_size": 0}


# ── no regression for businesses that DO have a website ─────────────────────

def test_a_business_with_a_real_website_is_unaffected_by_the_new_signals():
    weaknesses, _ = _detect_voice_weaknesses(
        _prospect(website="https://example.com", photo_count=20, has_hours=True, has_description=True),
        {"html": "<html><head><title>Example</title></head><body>gtag(</body></html>", "fetch_status": "ok", "detail": ""},
    )
    assert "no_website" not in weaknesses
    assert "few_photos" not in weaknesses
    assert "missing_hours" not in weaknesses
    assert "missing_description" not in weaknesses

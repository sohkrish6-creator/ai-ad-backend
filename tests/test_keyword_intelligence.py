"""
Keyword Intelligence Module — provenance discipline is the whole point of
this module ("must never present model-generated keyword guesses as search
data"), so the tests here focus hardest on the deterministic guards: the
clustering fidelity guard (GPT must never be trusted alone to have only
echoed real keyword text back), the cache layer (mandatory per spec, not
optional), the GSC-blend "no volume = 'not available'" rule, and the
Campaign Launch Kit integration's "only ever push REAL_VOLUME rows" rule.

Pure-logic pieces get real unit tests, no mocking. GPT-calling pieces are
tested by monkeypatching main.client.chat.completions.create — same
pattern as test_gpt_json_retry.py — specifically so the fidelity-guard
tests can DELIBERATELY feed a fabricated keyword through a fake GPT
response and assert it gets dropped; that behavior must be deterministic
and reproducible, not left to whether a live model happens to hallucinate
on a given run. Google Ads API calls (_kw_generate_ideas_sync etc.) are not
unit tested here — live/blocking SDK calls, not practical to unit test in
isolation, matching this codebase's established convention for
_run_voice_batch_job/_run_gads_import_job. The real protobuf message shapes
those functions rely on were read directly from the installed SDK source
during development, not guessed from memory.
"""
import sys
import os
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import text

import main
from main import (
    engine, _kw_cache_key, _kw_cache_lookup, _kw_cache_store, _kw_blend_gsc_queries,
    _kw_compute_bid_shortlist, _kw_research_to_campaign_keywords, _kw_cluster_keywords,
    _KW_LABEL_VERIFIED, _KW_LABEL_REAL_VOLUME, _KW_LABEL_OBSERVED, _KW_CACHE_DAYS,
)


def setup_function():
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM keyword_research_cache WHERE cache_key LIKE 'test:%'"))


def teardown_function():
    setup_function()


# ── cache key determinism ────────────────────────────────────────────────────

def test_same_inputs_produce_the_same_cache_key():
    k1 = _kw_cache_key("ideas", ["skin clinic", "laser hair removal"], "geoTargetConstants/1007787",
                        ["languageConstants/1000", "languageConstants/1023"], "GOOGLE_SEARCH")
    k2 = _kw_cache_key("ideas", ["laser hair removal", "skin clinic"], "geoTargetConstants/1007787",
                        ["languageConstants/1023", "languageConstants/1000"], "GOOGLE_SEARCH")
    assert k1 == k2  # term/language order must not matter — same real request either way


def test_different_geo_produces_a_different_cache_key():
    k1 = _kw_cache_key("ideas", ["skin clinic"], "geoTargetConstants/1007787", ["languageConstants/1000"], "GOOGLE_SEARCH")
    k2 = _kw_cache_key("ideas", ["skin clinic"], "geoTargetConstants/9999", ["languageConstants/1000"], "GOOGLE_SEARCH")
    assert k1 != k2


def test_different_page_url_produces_a_different_cache_key():
    # Real bug caught during development: a KeywordAndUrlSeed call returns
    # different ideas than a plain KeywordSeed call for identical terms —
    # omitting page_url from the key would wrongly conflate the two.
    k1 = _kw_cache_key("ideas", ["skin clinic"], "geoTargetConstants/1", ["languageConstants/1000"], "GOOGLE_SEARCH", "")
    k2 = _kw_cache_key("ideas", ["skin clinic"], "geoTargetConstants/1", ["languageConstants/1000"], "GOOGLE_SEARCH", "https://competitor.com")
    assert k1 != k2


def test_ideas_prefix_and_hist_prefix_never_collide_for_the_same_terms():
    k1 = _kw_cache_key("ideas", ["skin clinic"], "geoTargetConstants/1", ["languageConstants/1000"], "GOOGLE_SEARCH")
    k2 = _kw_cache_key("hist", ["skin clinic"], "geoTargetConstants/1", ["languageConstants/1000"], "GOOGLE_SEARCH")
    assert k1 != k2


# ── cache lookup/store — real DB, real freshness window ──────────────────────

def test_cache_miss_on_empty_table():
    assert _kw_cache_lookup("test:nonexistent") is None


def test_cache_store_then_lookup_round_trips():
    key = "test:round-trip"
    payload = [{"keyword": "skin clinic jaipur", "avg_monthly_searches": 880}]
    _kw_cache_store(key, ["skin clinic"], "geoTargetConstants/1", "languageConstants/1000", "GOOGLE_SEARCH", payload)
    result = _kw_cache_lookup(key)
    assert result is not None
    assert result["payload"] == payload


def test_a_fresh_cache_entry_is_reused_showing_the_cache_date():
    key = "test:fresh"
    _kw_cache_store(key, ["x"], "geoTargetConstants/1", "languageConstants/1000", "GOOGLE_SEARCH", [{"keyword": "x"}])
    result = _kw_cache_lookup(key)
    assert result is not None
    assert result["fetched_at"]  # the cache date the user must be shown


def test_a_stale_cache_entry_past_30_days_is_not_reused():
    key = "test:stale"
    stale_ts = (datetime.utcnow() - timedelta(days=_KW_CACHE_DAYS + 1)).isoformat()
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO keyword_research_cache (cache_key, seed_terms_json, geo, language, network, ideas_json, fetched_at) "
            "VALUES (:k, '[]', 'geo', 'lang', 'net', '[]', :ts)"
        ), {"k": key, "ts": stale_ts})
    assert _kw_cache_lookup(key) is None


def test_a_cache_entry_29_days_old_is_still_reused():
    key = "test:almost-stale"
    recent_ts = (datetime.utcnow() - timedelta(days=_KW_CACHE_DAYS - 1)).isoformat()
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO keyword_research_cache (cache_key, seed_terms_json, geo, language, network, ideas_json, fetched_at) "
            "VALUES (:k, '[]', 'geo', 'lang', 'net', '[\"still fresh\"]', :ts)"
        ), {"k": key, "ts": recent_ts})
    result = _kw_cache_lookup(key)
    assert result is not None
    assert result["payload"] == ["still fresh"]


def test_storing_again_updates_the_same_cache_key_not_a_duplicate_row():
    key = "test:upsert"
    _kw_cache_store(key, ["x"], "geo", "lang", "net", [{"v": 1}])
    _kw_cache_store(key, ["x"], "geo", "lang", "net", [{"v": 2}])
    with engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM keyword_research_cache WHERE cache_key=:k"), {"k": key}).scalar()
    assert count == 1
    assert _kw_cache_lookup(key)["payload"] == [{"v": 2}]


# ── GSC blending — "no Ads volume = not available", never estimated ────────

def test_gsc_only_query_becomes_verified_with_no_volume():
    ads_rows = {}
    gsc_queries = [{"query_text": "best dermatologist jaipur", "clicks": 12, "impressions": 340, "ctr": 0.035, "avg_position": 4.2}]
    result = _kw_blend_gsc_queries(ads_rows, gsc_queries)
    row = result["best dermatologist jaipur"]
    assert row["label"] == _KW_LABEL_VERIFIED
    assert row["avg_monthly_searches"] is None  # hard rule: never an estimate
    assert row["gsc_clicks"] == 12


def test_a_query_present_in_both_ads_and_gsc_keeps_its_real_volume_and_gains_gsc_fields():
    ads_rows = {"laser hair removal jaipur": {
        "keyword": "laser hair removal jaipur", "avg_monthly_searches": 590, "competition": "MEDIUM",
        "competition_index": 55, "low_bid_micros": 8_000_000, "high_bid_micros": 22_000_000,
        "data_source": "google_ads_api", "label": _KW_LABEL_REAL_VOLUME,
    }}
    gsc_queries = [{"query_text": "laser hair removal jaipur", "clicks": 8, "impressions": 150, "ctr": 0.053, "avg_position": 6.1}]
    result = _kw_blend_gsc_queries(ads_rows, gsc_queries)
    row = result["laser hair removal jaipur"]
    assert row["avg_monthly_searches"] == 590  # real Ads volume preserved
    assert row["gsc_clicks"] == 8               # real GSC data also present
    assert row["label"] == _KW_LABEL_VERIFIED    # observed real query outranks estimate-only


def test_blank_query_text_is_ignored_not_a_crash():
    result = _kw_blend_gsc_queries({}, [{"query_text": "", "clicks": 1}])
    assert result == {}


# ── deterministic bid shortlist — never GPT-driven ──────────────────────────

def _row(keyword, volume, competition_index, low_bid, high_bid):
    return {"keyword": keyword, "avg_monthly_searches": volume, "competition": "MEDIUM",
            "competition_index": competition_index, "low_bid_micros": low_bid, "high_bid_micros": high_bid,
            "data_source": "google_ads_api", "label": _KW_LABEL_REAL_VOLUME}


def test_shortlist_excludes_rows_with_no_real_volume():
    rows = [
        _row("real one", 500, 40, 5_000_000, 15_000_000),
        {"keyword": "no volume", "avg_monthly_searches": None, "competition_index": 20,
         "low_bid_micros": 1_000_000, "high_bid_micros": 2_000_000, "label": _KW_LABEL_OBSERVED},
    ]
    shortlist = _kw_compute_bid_shortlist(rows, budget_micros=50_000 * 1_000_000)
    texts = [r["keyword"] for r in shortlist]
    assert "real one" in texts
    assert "no volume" not in texts


def test_shortlist_excludes_rows_with_no_bid_data():
    rows = [_row("has bid", 500, 40, 5_000_000, 15_000_000),
            {"keyword": "no bid data", "avg_monthly_searches": 900, "competition_index": 30,
             "low_bid_micros": None, "high_bid_micros": None, "label": _KW_LABEL_REAL_VOLUME}]
    shortlist = _kw_compute_bid_shortlist(rows, budget_micros=50_000 * 1_000_000)
    texts = [r["keyword"] for r in shortlist]
    assert "has bid" in texts
    assert "no bid data" not in texts


def test_shortlist_is_capped_at_max_items():
    rows = [_row(f"kw{i}", 1000 - i, 30, 3_000_000, 9_000_000) for i in range(30)]
    shortlist = _kw_compute_bid_shortlist(rows, budget_micros=100_000 * 1_000_000, max_items=10)
    assert len(shortlist) == 10


def test_shortlist_prefers_higher_volume_lower_competition():
    rows = [
        _row("high volume low competition", 2000, 20, 4_000_000, 10_000_000),
        _row("low volume high competition", 200, 90, 4_000_000, 10_000_000),
    ]
    shortlist = _kw_compute_bid_shortlist(rows, budget_micros=100_000 * 1_000_000)
    assert shortlist[0]["keyword"] == "high volume low competition"


def test_no_budget_returns_empty_shortlist_not_a_guess():
    rows = [_row("x", 500, 40, 5_000_000, 15_000_000)]
    assert _kw_compute_bid_shortlist(rows, budget_micros=0) == []
    assert _kw_compute_bid_shortlist(rows, budget_micros=None) == []


def test_informational_intent_keywords_are_excluded_from_the_bid_shortlist():
    # Real case caught during development: a purely informational "what
    # causes X" query can have the highest volume and lowest competition of
    # the whole batch, which would otherwise dominate a purely numeric
    # ranking — but per spec, informational queries are content/GEO-AEO
    # targets, never ad targets.
    rows = [
        _row("what causes pigmentation on face", 4400, 15, 3_000_000, 9_000_000),
        _row("laser hair removal jaipur", 590, 78, 20_000_000, 55_000_000),
    ]
    shortlist = _kw_compute_bid_shortlist(
        rows, budget_micros=100_000 * 1_000_000, exclude_keywords={"what causes pigmentation on face"},
    )
    texts = [r["keyword"] for r in shortlist]
    assert "what causes pigmentation on face" not in texts
    assert "laser hair removal jaipur" in texts


def test_no_exclude_keywords_behaves_exactly_as_before():
    rows = [_row("x", 500, 40, 5_000_000, 15_000_000)]
    assert _kw_compute_bid_shortlist(rows, budget_micros=100_000 * 1_000_000, exclude_keywords=None) == \
        _kw_compute_bid_shortlist(rows, budget_micros=100_000 * 1_000_000, exclude_keywords=set())


def test_a_keyword_whose_low_bid_alone_would_dominate_the_daily_budget_is_excluded():
    # ~ Rs.100/day budget; a keyword with a Rs.10,000 low-bid floor is not
    # realistically biddable at this budget — the affordability filter
    # must exclude it rather than shortlist something unbiddable.
    rows = [_row("too expensive", 5000, 20, 10_000 * 1_000_000, 20_000 * 1_000_000)]
    shortlist = _kw_compute_bid_shortlist(rows, budget_micros=3_000 * 1_000_000)  # Rs.3000/month = Rs.100/day
    assert shortlist == []


# ── Campaign Launch Kit integration — only ever push REAL_VOLUME rows ───────

def test_no_research_data_returns_empty_falls_back_to_gpt():
    assert _kw_research_to_campaign_keywords(None) == []
    assert _kw_research_to_campaign_keywords({}) == []


def test_bid_shortlist_is_preferred_when_present():
    research = {
        "bid_shortlist": [{"keyword": "book dermatologist jaipur", "avg_monthly_searches": 300}],
        "top_searches": [{"keyword": "irrelevant", "avg_monthly_searches": 9999, "label": _KW_LABEL_REAL_VOLUME}],
    }
    result = _kw_research_to_campaign_keywords(research)
    assert len(result) == 1
    assert result[0]["text"] == "book dermatologist jaipur"


def test_falls_back_to_top_searches_real_volume_rows_when_no_shortlist():
    research = {
        "bid_shortlist": [],
        "top_searches": [
            {"keyword": "real volume kw", "avg_monthly_searches": 500, "label": _KW_LABEL_REAL_VOLUME},
            {"keyword": "verified only kw", "avg_monthly_searches": None, "label": _KW_LABEL_VERIFIED},
            {"keyword": "observed only kw", "avg_monthly_searches": None, "label": _KW_LABEL_OBSERVED},
        ],
    }
    result = _kw_research_to_campaign_keywords(research)
    texts = [r["text"] for r in result]
    assert texts == ["real volume kw"]  # VERIFIED/OBSERVED rows with no real volume never pushed as paid keywords


def test_top_5_by_volume_get_exact_match_rest_get_phrase():
    rows = [{"keyword": f"kw{i}", "avg_monthly_searches": 1000 - i, "label": _KW_LABEL_REAL_VOLUME} for i in range(8)]
    result = _kw_research_to_campaign_keywords({"bid_shortlist": rows})
    assert [r["match_type"] for r in result[:5]] == ["EXACT"] * 5
    assert [r["match_type"] for r in result[5:]] == ["PHRASE"] * 3


def test_result_is_capped_at_max_keywords():
    rows = [{"keyword": f"kw{i}", "avg_monthly_searches": 1000 - i, "label": _KW_LABEL_REAL_VOLUME} for i in range(40)]
    result = _kw_research_to_campaign_keywords({"bid_shortlist": rows}, max_keywords=20)
    assert len(result) == 20


# ── clustering fidelity guard — the core "never invent a keyword" guarantee ──

class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


def _fake_gpt(response_json_str):
    def _create(model, messages, max_tokens, temperature, response_format, **kwargs):
        return _FakeResponse(response_json_str)
    return _create


REAL_ROWS = [
    {"keyword": "laser hair removal jaipur", "avg_monthly_searches": 590, "competition": "MEDIUM",
     "label": _KW_LABEL_REAL_VOLUME},
    {"keyword": "acne treatment cost jaipur", "avg_monthly_searches": 320, "competition": "LOW",
     "label": _KW_LABEL_REAL_VOLUME},
]


@pytest.mark.asyncio
async def test_a_fabricated_keyword_gpt_invents_is_dropped_not_shown(monkeypatch):
    # The core guarantee under test: GPT's response includes a real keyword
    # AND a fabricated one that was never in the input — the fabricated one
    # must never reach the returned clusters, regardless of the prompt
    # instruction telling it not to.
    fake_response = (
        '{"clusters": [{"intent": "transactional", "label": "Transactional", '
        '"why_it_matters": "high intent", "recommendation": "bid", '
        '"keywords": ["laser hair removal jaipur", "totally invented keyword nobody searched"]}], '
        '"hinglish_patterns": []}'
    )
    monkeypatch.setattr(main.client.chat.completions, "create", _fake_gpt(fake_response))
    result = await _kw_cluster_keywords(REAL_ROWS, "skin clinic", "Jaipur")
    all_shown_keywords = [kw for c in result["clusters"] for kw in c["keywords"]]
    assert "laser hair removal jaipur" in all_shown_keywords
    assert "totally invented keyword nobody searched" not in all_shown_keywords
    assert result["dropped_count"] == 1


@pytest.mark.asyncio
async def test_a_cluster_made_entirely_of_fabricated_keywords_is_dropped_entirely(monkeypatch):
    fake_response = (
        '{"clusters": [{"intent": "commercial", "label": "Fake Cluster", "why_it_matters": "x", '
        '"recommendation": "content", "keywords": ["fake one", "fake two"]}], "hinglish_patterns": []}'
    )
    monkeypatch.setattr(main.client.chat.completions, "create", _fake_gpt(fake_response))
    result = await _kw_cluster_keywords(REAL_ROWS, "skin clinic", "Jaipur")
    assert result["clusters"] == []
    assert result["dropped_count"] == 2


@pytest.mark.asyncio
async def test_keyword_matching_is_case_insensitive_but_exact_text_is_preserved(monkeypatch):
    fake_response = (
        '{"clusters": [{"intent": "transactional", "label": "T", "why_it_matters": "x", '
        '"recommendation": "bid", "keywords": ["LASER HAIR REMOVAL JAIPUR"]}], "hinglish_patterns": []}'
    )
    monkeypatch.setattr(main.client.chat.completions, "create", _fake_gpt(fake_response))
    result = await _kw_cluster_keywords(REAL_ROWS, "skin clinic", "Jaipur")
    # Matched despite case difference, but the ORIGINAL real casing is what's shown, not GPT's.
    assert result["clusters"][0]["keywords"] == ["laser hair removal jaipur"]


@pytest.mark.asyncio
async def test_an_unrecognized_intent_label_defaults_safely_rather_than_being_dropped(monkeypatch):
    fake_response = (
        '{"clusters": [{"intent": "made_up_intent_type", "label": "T", "why_it_matters": "x", '
        '"recommendation": "bid", "keywords": ["laser hair removal jaipur"]}], "hinglish_patterns": []}'
    )
    monkeypatch.setattr(main.client.chat.completions, "create", _fake_gpt(fake_response))
    result = await _kw_cluster_keywords(REAL_ROWS, "skin clinic", "Jaipur")
    assert len(result["clusters"]) == 1
    assert result["clusters"][0]["intent"] == "commercial"  # safe default, not dropped


@pytest.mark.asyncio
async def test_empty_keyword_rows_never_calls_gpt_at_all(monkeypatch):
    called = {"n": 0}

    def _create(*a, **kw):
        called["n"] += 1
        return _FakeResponse('{"clusters": [], "hinglish_patterns": []}')
    monkeypatch.setattr(main.client.chat.completions, "create", _create)
    result = await _kw_cluster_keywords([], "skin clinic", "Jaipur")
    assert result == {"clusters": [], "hinglish_patterns": [], "dropped_count": 0}
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_gpt_failure_returns_empty_clusters_not_a_crash(monkeypatch):
    def _create(*a, **kw):
        raise RuntimeError("simulated GPT outage")
    monkeypatch.setattr(main.client.chat.completions, "create", _create)
    result = await _kw_cluster_keywords(REAL_ROWS, "skin clinic", "Jaipur")
    assert result == {"clusters": [], "hinglish_patterns": [], "dropped_count": 0}

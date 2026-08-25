"""
Post-audit fix #2. A live Marketing Intelligence run (no TAVILY_API_KEY
configured) showed overview.data_source = "Tavily research" and
business_dna.data_source = "Tavily research" while citing specific facts.
confidence_guard.py's text-matching against data_source can't catch a
claim that reads exactly like real evidence; only the caller (this
pipeline) knows the real, verifiable ground truth — which of THIS call's
actual research inputs were non-empty. _mi_apply_research_ground_truth is
that fix, applied before confidence_guard.py ever runs.

First version of this fix treated "did research run" as one bool per
section-generator call — wrong for overview/business_dna specifically,
since wikipedia_raw comes from a direct public REST call (_mi_fetch_
wikipedia) that needs no API key and succeeds independently of Tavily.
Real research DID run there, just not Tavily — so the fix now attributes
which SPECIFIC real sources contributed, not just whether "any" did, and
rebuilds data_source from that fact unconditionally rather than only
correcting claims a naive text scan happens to catch.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import _mi_apply_research_ground_truth, _mi_real_sources_used


def test_no_sources_available_forces_the_no_research_label():
    research = {"overview_raw": "", "marketing_raw": ""}
    result = {"overview": {"confidence": 75, "data_source": "Tavily research"}}
    _mi_apply_research_ground_truth(result, ("overview",), research, ("overview_raw", "marketing_raw"))
    section = result["overview"]
    assert section["research_ran"] is False
    assert "tavily" not in section["data_source"].lower()
    assert "not verified" in section["data_source"].lower()


def test_wikipedia_only_is_attributed_as_wikipedia_not_tavily():
    # The exact real-world case: no Tavily key configured, but
    # _mi_fetch_wikipedia (a direct public API call) genuinely succeeded.
    research = {"wikipedia_raw": "ResMed is a medical device company...", "overview_raw": "", "news_raw": ""}
    result = {"overview": {"confidence": 75, "data_source": "Tavily research"}}
    _mi_apply_research_ground_truth(result, ("overview",), research, ("wikipedia_raw", "overview_raw", "news_raw"))
    section = result["overview"]
    assert section["research_ran"] is True  # real research DID happen — must not be suppressed
    assert section["data_source"] == "Wikipedia"
    assert "tavily" not in section["data_source"].lower()  # the false attribution is gone


def test_tavily_and_wikipedia_both_present_are_both_named():
    research = {"wikipedia_raw": "facts...", "overview_raw": "more facts from a real Tavily query"}
    result = {"overview": {"confidence": 75}}
    _mi_apply_research_ground_truth(result, ("overview",), research, ("wikipedia_raw", "overview_raw"))
    assert result["overview"]["data_source"] == "Wikipedia + Tavily research"


def test_decade_raw_group_expands_dynamic_keys():
    research = {"decade_1990s_raw": "", "decade_2000s_raw": "real content here", "overview_raw": ""}
    result = {"overview": {"confidence": 75}}
    _mi_apply_research_ground_truth(result, ("overview",), research, ("overview_raw", "decade_raw_group"))
    assert result["overview"]["research_ran"] is True
    assert result["overview"]["data_source"] == "Tavily research"


def test_website_content_is_attributed_to_firecrawl_not_tavily():
    research = {"website_content": "homepage text", "marketing_raw": ""}
    result = {"advertising": {"confidence": 60}}
    _mi_apply_research_ground_truth(result, ("advertising",), research, ("website_content", "marketing_raw"))
    assert result["advertising"]["data_source"] == "website content (Firecrawl)"


def test_data_source_is_always_rebuilt_even_when_the_original_claim_was_accurate():
    # Unconditional rebuild, not "only fix it if it looks wrong" — removes
    # any chance of a text-scan gap letting a false claim slip through.
    research = {"marketing_raw": "real data"}
    result = {"channels": {"confidence": 65, "data_source": "Tavily research"}}
    _mi_apply_research_ground_truth(result, ("channels",), research, ("marketing_raw",))
    assert result["channels"]["data_source"] == "Tavily research"  # same value, but computed, not trusted


def test_only_named_section_keys_are_touched():
    research = {}
    result = {
        "overview": {"confidence": 75, "data_source": "Tavily research"},
        "untouched": {"confidence": 50, "data_source": "Tavily research"},
    }
    _mi_apply_research_ground_truth(result, ("overview",), research, ("overview_raw",))
    assert result["overview"]["research_ran"] is False
    assert "research_ran" not in result["untouched"]
    assert result["untouched"]["data_source"] == "Tavily research"  # unmodified, wasn't in section_keys


def test_non_dict_result_is_a_safe_noop():
    assert _mi_apply_research_ground_truth(None, ("overview",), {}, ("overview_raw",)) is None


def test_missing_section_key_is_skipped_not_fatal():
    result = {"overview": {"confidence": 75, "data_source": "x"}}
    # "advertising" isn't in result at all — e.g. GPT call failed and
    # returned {} for that key — must not raise.
    _mi_apply_research_ground_truth(result, ("overview", "advertising"), {}, ("overview_raw",))
    assert result["overview"]["research_ran"] is False


def test_no_research_label_matches_the_existing_unverified_signal_list():
    from main import _MI_UNVERIFIED_SOURCE_SIGNALS
    result = {"advertising": {"confidence": 60, "data_source": "Tavily research"}}
    _mi_apply_research_ground_truth(result, ("advertising",), {}, ("overview_raw",))
    corrected = result["advertising"]["data_source"].lower()
    assert any(sig in corrected for sig in _MI_UNVERIFIED_SOURCE_SIGNALS)


def test_mi_real_sources_used_directly():
    research = {"wikipedia_raw": "x", "website_content": "", "overview_raw": "y", "decade_1980s_raw": "z"}
    sources = _mi_real_sources_used(research, ("wikipedia_raw", "website_content", "overview_raw", "decade_raw_group"))
    assert sources == ["Wikipedia", "Tavily research"]  # order preserved, de-duped, website_content excluded (empty)

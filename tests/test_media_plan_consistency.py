"""
P0.3 tests. The core claim ("one canonical generator, both endpoints read
the same cached object") was verified live via two temporary local
integration scripts (not pytest-collected, deleted after use) — a real
/full-report call followed by a real /media-buying-plan call for the same
business returned all 13 fields byte-identical, response time ~0s
("cached": True); a business that never had /full-report run still got a
real, correctly-shaped plan from /media-buying-plan's fallback path
("cached": False). That's an integration property (two HTTP endpoints,
report_snapshot, real GPT calls) that doesn't reduce to a pure unit test.

_parse_media_plan_sections IS pure, and is the one piece of new parsing
logic P0.3 introduces — it gets a real unit test here, using the same
loose-header-matching fixture style as the fixture module.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import _parse_media_plan_sections, _MEDIA_PLAN_SECTION_HEADERS

SAMPLE_PLAN_TEXT = """
1. CAMPAIGN OBJECTIVE:
Lead generation for a B2B distributor.

2. PLATFORM RECOMMENDATIONS:
1st: Google Search — highest intent
2nd: LinkedIn — reaches decision makers

3. BUDGET ALLOCATION:
Google 60%, LinkedIn 40%.

4. BID STRATEGY:
Maximize Conversions.

5. LAUNCH PLAN:
Launch Monday, set up conversion tracking first.

6. LEARNING PHASE:
7-14 days minimum.

7. SCALING PLAN:
Scale 20% weekly if CPA is stable.

8. PAUSE RULES:
Pause if CTR below 1% after 7 days.

9. STOP RULES:
Stop if no conversions after 30 days.

10. OPTIMIZATION PLAN:
Test 3 creative variants weekly.

11. RISK ANALYSIS:
Medium risk — competitive category.

12. MEDIA BUYER PLAYBOOK:
Day 1: launch. Day 7: review. Day 14: scale or pause.

13. INDUSTRY BENCHMARKS:
CTR 1-3%, CPC ₹20-50.
"""


def test_parses_all_13_sections():
    sections = _parse_media_plan_sections(SAMPLE_PLAN_TEXT)
    assert len(sections) == 13
    assert len(_MEDIA_PLAN_SECTION_HEADERS) == 13


def test_each_section_gets_its_own_content_not_the_next_ones():
    sections = _parse_media_plan_sections(SAMPLE_PLAN_TEXT)
    assert "Lead generation" in sections["campaign_objective"]
    assert "PLATFORM RECOMMENDATIONS" not in sections["campaign_objective"]
    assert "Google Search" in sections["platform_recommendations"]
    assert "BUDGET ALLOCATION" not in sections["platform_recommendations"]


def test_last_section_captures_to_end_of_text():
    sections = _parse_media_plan_sections(SAMPLE_PLAN_TEXT)
    assert "CTR 1-3%" in sections["industry_benchmarks"]


def test_handles_markdown_bold_headers():
    text = SAMPLE_PLAN_TEXT.replace("1. CAMPAIGN OBJECTIVE:", "**1. CAMPAIGN OBJECTIVE:**")
    sections = _parse_media_plan_sections(text)
    assert "Lead generation" in sections["campaign_objective"]


def test_missing_section_returns_empty_string_not_a_crash():
    text_missing_one = SAMPLE_PLAN_TEXT.replace("7. SCALING PLAN:\nScale 20% weekly if CPA is stable.\n\n", "")
    sections = _parse_media_plan_sections(text_missing_one)
    assert sections["scaling_plan"] == ""
    assert len(sections) == 13  # still every key present, just empty

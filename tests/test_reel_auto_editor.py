"""
AI Reel Auto-Editor (Creative Studio) — pure-logic coverage for the
deterministic pieces of the pipeline: SRT generation and Claude segment-
JSON validation. Both are real unit tests, no mocking, per this codebase's
convention — the surrounding pipeline (faster-whisper transcription, the
live Claude call, ffmpeg execution) is live I/O-bound work not practical to
unit test in isolation (matches the established precedent for
_run_voice_batch_job etc.), and was instead verified with a real local
end-to-end run during development: a genuine 9.4s test video (synthesized
via macOS `say` + ffmpeg) was transcribed with the real faster-whisper
"small" model, built into a real SRT, and burned into a real 1080x1920
output via ffmpeg's subtitles filter — confirmed visually correct.

That local run also surfaced a real, load-bearing finding: the default
Homebrew ffmpeg build has no libass compiled in at all, so the `subtitles`
filter this feature depends on doesn't exist even though `ffmpeg` itself
runs fine — see _system_capabilities' ffmpeg_subtitles_supported check
(added directly because of this), never assumed from ffmpeg_installed
alone.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from main import (
    _reel_build_srt, _reel_format_srt_timestamp, _reel_validate_claude_segment,
    _REEL_MIN_SEGMENT_SECONDS, _REEL_MAX_SEGMENT_SECONDS,
)


# ── SRT timestamp formatting ─────────────────────────────────────────────────

def test_zero_seconds_formats_correctly():
    assert _reel_format_srt_timestamp(0) == "00:00:00,000"


def test_fractional_seconds_round_to_milliseconds():
    assert _reel_format_srt_timestamp(65.4321) == "00:01:05,432"


def test_over_an_hour_carries_into_the_hours_field():
    assert _reel_format_srt_timestamp(3661.5) == "01:01:01,500"


def test_negative_seconds_clamped_to_zero_not_a_crash():
    assert _reel_format_srt_timestamp(-5) == "00:00:00,000"


# ── SRT building — the clip-relative re-zeroing is the load-bearing part ────

def test_a_segment_fully_inside_the_window_is_shifted_relative_to_clip_start():
    segments = [{"start": 40.0, "end": 43.0, "text": "hello there"}]
    srt = _reel_build_srt(segments, clip_start=35.0, clip_end=65.0)
    assert "1\n00:00:05,000 --> 00:00:08,000\nhello there" in srt


def test_a_segment_starting_before_the_window_is_clipped_to_the_window_start():
    # ffmpeg's -ss re-zeros the OUTPUT timeline to clip_start — a segment
    # that started earlier must never be shown as if it started before 0.
    segments = [{"start": 30.0, "end": 38.0, "text": "spans the boundary"}]
    srt = _reel_build_srt(segments, clip_start=35.0, clip_end=65.0)
    assert "00:00:00,000 --> 00:00:03,000" in srt


def test_a_segment_ending_after_the_window_is_clipped_to_the_window_end():
    segments = [{"start": 60.0, "end": 70.0, "text": "spans the other boundary"}]
    srt = _reel_build_srt(segments, clip_start=35.0, clip_end=65.0)
    assert "00:00:25,000 --> 00:00:30,000" in srt


def test_a_segment_entirely_outside_the_window_is_dropped():
    segments = [{"start": 0.0, "end": 5.0, "text": "way before the clip"}]
    srt = _reel_build_srt(segments, clip_start=35.0, clip_end=65.0)
    assert srt == ""


def test_a_segment_touching_the_boundary_exactly_is_dropped_not_zero_length():
    segments = [{"start": 20.0, "end": 35.0, "text": "ends exactly at clip_start"}]
    srt = _reel_build_srt(segments, clip_start=35.0, clip_end=65.0)
    assert srt == ""


def test_multiple_segments_get_sequential_srt_indices():
    segments = [
        {"start": 36.0, "end": 38.0, "text": "first"},
        {"start": 40.0, "end": 42.0, "text": "second"},
        {"start": 44.0, "end": 46.0, "text": "third"},
    ]
    srt = _reel_build_srt(segments, clip_start=35.0, clip_end=65.0)
    lines = srt.split("\n")
    assert lines[0] == "1" and lines[4] == "2" and lines[8] == "3"


def test_a_blank_text_segment_is_skipped():
    segments = [{"start": 36.0, "end": 38.0, "text": "   "}]
    srt = _reel_build_srt(segments, clip_start=35.0, clip_end=65.0)
    assert srt == ""


def test_empty_segment_list_produces_empty_srt():
    assert _reel_build_srt([], clip_start=0.0, clip_end=30.0) == ""


# ── Claude segment JSON validation — never trust the model's own claim to
#    have honored the schema/bounds, same discipline as every GPT-JSON call
#    elsewhere in this codebase ─────────────────────────────────────────────

def test_a_valid_segment_passes_through():
    result = _reel_validate_claude_segment({"start": 10, "end": 45, "hook": "Great hook"}, video_duration=90.0)
    assert result == {"start": 10.0, "end": 45.0, "hook": "Great hook"}


def test_not_a_dict_is_rejected():
    with pytest.raises(ValueError, match="JSON object"):
        _reel_validate_claude_segment(["not", "a", "dict"], video_duration=90.0)


def test_missing_start_is_rejected():
    with pytest.raises(ValueError, match="start/end"):
        _reel_validate_claude_segment({"end": 45, "hook": "x"}, video_duration=90.0)


def test_non_numeric_start_is_rejected():
    with pytest.raises(ValueError, match="start/end"):
        _reel_validate_claude_segment({"start": "not a number", "end": 45, "hook": "x"}, video_duration=90.0)


def test_empty_hook_is_rejected():
    with pytest.raises(ValueError, match="hook"):
        _reel_validate_claude_segment({"start": 10, "end": 45, "hook": ""}, video_duration=90.0)


def test_missing_hook_is_rejected():
    with pytest.raises(ValueError, match="hook"):
        _reel_validate_claude_segment({"start": 10, "end": 45}, video_duration=90.0)


def test_end_before_start_is_rejected():
    with pytest.raises(ValueError, match="non-positive-length"):
        _reel_validate_claude_segment({"start": 45, "end": 10, "hook": "x"}, video_duration=90.0)


def test_negative_start_is_rejected():
    with pytest.raises(ValueError, match="non-positive-length"):
        _reel_validate_claude_segment({"start": -5, "end": 30, "hook": "x"}, video_duration=90.0)


def test_start_past_the_real_video_duration_is_rejected():
    # The exact hallucination class this guards against: a plausible-
    # looking but out-of-bounds timestamp that would make ffmpeg fail or
    # silently clip to nothing.
    with pytest.raises(ValueError, match="past the video's actual duration"):
        _reel_validate_claude_segment({"start": 200, "end": 240, "hook": "x"}, video_duration=90.0)


def test_end_past_duration_is_clamped_not_rejected():
    # start is legitimately inside the video; end overshoots slightly
    # (rounding, or Claude being a bit generous) — clamp rather than fail
    # the whole job over a few seconds of overshoot.
    result = _reel_validate_claude_segment({"start": 60, "end": 95, "hook": "x"}, video_duration=90.0)
    assert result["end"] == 90.0


def test_segment_shorter_than_the_minimum_is_rejected():
    with pytest.raises(ValueError, match=f"{_REEL_MIN_SEGMENT_SECONDS}-{_REEL_MAX_SEGMENT_SECONDS}"):
        _reel_validate_claude_segment({"start": 0, "end": 9, "hook": "x"}, video_duration=90.0)


def test_segment_longer_than_the_maximum_is_rejected():
    with pytest.raises(ValueError, match=f"{_REEL_MIN_SEGMENT_SECONDS}-{_REEL_MAX_SEGMENT_SECONDS}"):
        _reel_validate_claude_segment({"start": 0, "end": 120, "hook": "x"}, video_duration=150.0)


def test_segment_exactly_at_the_minimum_boundary_is_accepted():
    result = _reel_validate_claude_segment(
        {"start": 0, "end": _REEL_MIN_SEGMENT_SECONDS, "hook": "x"}, video_duration=90.0,
    )
    assert result["end"] - result["start"] == _REEL_MIN_SEGMENT_SECONDS


def test_segment_exactly_at_the_maximum_boundary_is_accepted():
    result = _reel_validate_claude_segment(
        {"start": 0, "end": _REEL_MAX_SEGMENT_SECONDS, "hook": "x"}, video_duration=90.0,
    )
    assert result["end"] - result["start"] == _REEL_MAX_SEGMENT_SECONDS

"""
Post-audit fix. Real reported case: Revenue Engine Pipeline, Healthcare &
Clinics / Jaipur, batch f90e1e733d0d411cba39a05e1f0037b8 — phone_populated_
count was 40% (6/15) against the SAME Google Place Details data legacy
Prospect Discovery reads reliably for the same businesses (both call the
identical fetch_place_details/formatted_phone_number field).

Traced to: _normalize_phone_e164 required the ENTIRE raw string to parse as
exactly one phone number. A clinic listing commonly returns more than one
contact number in that one field (e.g. a reception line and a separate
emergency/alternate line) as "9876543210, 9123456789" or similar —
phonenumbers.parse() on the combined string either raises
NumberParseException or fails is_valid_number, so a business Google gave a
perfectly real, dialable number for was silently counted as having none.
Legacy shows formatted_phone_number raw/unvalidated (never hits this parse
at all), which is part of why it read as more phone-reliable for the exact
same businesses.

Fix: use phonenumbers.PhoneNumberMatcher to find the first genuinely valid
number embedded in the raw text, rather than requiring the whole string to
parse as one number. Pure function, no I/O — real unit tests, no mocking,
per this codebase's convention.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import _normalize_phone_e164


def test_a_single_clean_mobile_number_still_normalizes():
    assert _normalize_phone_e164("+91 98765 43210") == "+919876543210"


def test_a_bare_ten_digit_mobile_still_normalizes():
    assert _normalize_phone_e164("9876543210") == "+919876543210"


def test_a_landline_with_std_code_still_normalizes():
    assert _normalize_phone_e164("0141-2345678") == "+911412345678"


def test_the_exact_reported_bug_two_numbers_comma_separated():
    # Reception + emergency line, a real, common clinic listing shape.
    assert _normalize_phone_e164("9876543210, 9123456789") == "+919876543210"


def test_two_numbers_slash_separated():
    assert _normalize_phone_e164("+91 98765 43210 / 0141 2345678") == "+919876543210"


def test_two_numbers_with_or_and_labels():
    assert _normalize_phone_e164("Reception: 9876543210, Emergency: 9123456789") == "+919876543210"


def test_a_genuinely_invalid_short_number_still_returns_none():
    # Old-style 6-digit local number — not a real, dialable number either
    # way; must not be fabricated into something valid.
    assert _normalize_phone_e164("0141-234567") is None


def test_empty_and_none_still_return_none():
    assert _normalize_phone_e164("") is None
    assert _normalize_phone_e164(None) is None


def test_garbage_text_with_no_embedded_number_returns_none():
    assert _normalize_phone_e164("call reception during business hours") is None

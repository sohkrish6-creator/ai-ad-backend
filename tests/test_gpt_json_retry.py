"""
Regression coverage for _call_gpt_json_with_retry, the shared helper that
fixes the "Unterminated string starting at: line N column M" class of bug —
a truncated GPT JSON response reaching the user as a raw parser error
instead of triggering a self-correction retry.

/prospect-discovery's "Find Prospects" scoring call predates this helper
and used to do a bare json.loads() with zero retry — that gap (and ~20
other call sites bypassing the helper across the codebase, found via an
AST-based audit) is what this pass fixed. These tests exercise the helper
itself directly, monkeypatching main.client.chat.completions.create so the
retry/give-up behavior is deterministic and network-free.
"""
import sys
import os
import json as _json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import main


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


def _make_fake_create(responses):
    """Returns a fake client.chat.completions.create that yields each item
    in `responses` in order (as raw response text), recording every call's
    messages for inspection."""
    calls = []

    def _fake_create(model, messages, max_tokens, temperature, response_format):
        calls.append(messages)
        idx = len(calls) - 1
        content = responses[min(idx, len(responses) - 1)]
        return _FakeResponse(content)

    _fake_create.calls = calls
    return _fake_create


@pytest.mark.asyncio
async def test_succeeds_first_try_no_retry(monkeypatch):
    fake = _make_fake_create(['{"prospects": [{"name": "Acme"}]}'])
    monkeypatch.setattr(main.client.chat.completions, "create", fake)

    result = await main._call_gpt_json_with_retry(
        lambda correction: [{"role": "user", "content": "score these"}],
        label="test",
    )
    assert result == {"prospects": [{"name": "Acme"}]}
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_recovers_after_one_truncated_response(monkeypatch):
    truncated = '{"prospects": [{"name": "Acme", "reason": "no website presen'  # cut off mid-string
    valid = '{"prospects": [{"name": "Acme", "reason": "no website"}]}'
    fake = _make_fake_create([truncated, valid])
    monkeypatch.setattr(main.client.chat.completions, "create", fake)

    result = await main._call_gpt_json_with_retry(
        lambda correction: [{"role": "user", "content": "score these"}] + (
            [{"role": "user", "content": correction}] if correction else []
        ),
        label="test", retries=1,
    )
    assert result == {"prospects": [{"name": "Acme", "reason": "no website"}]}
    assert len(fake.calls) == 2
    # The second call must have been told its previous response was invalid —
    # this is the actual self-correction mechanism, not just a blind retry.
    second_call_messages = fake.calls[1]
    assert any("not valid JSON" in m["content"] for m in second_call_messages)


@pytest.mark.asyncio
async def test_exhausts_retries_and_raises_decode_error(monkeypatch):
    truncated = '{"prospects": [{"name": "Acme", "reason": "no website presen'
    fake = _make_fake_create([truncated, truncated])
    monkeypatch.setattr(main.client.chat.completions, "create", fake)

    with pytest.raises(_json.JSONDecodeError):
        await main._call_gpt_json_with_retry(
            lambda correction: [{"role": "user", "content": "score these"}],
            label="test", retries=1,
        )
    assert len(fake.calls) == 2  # 1 initial + 1 retry, then gives up


@pytest.mark.asyncio
async def test_decode_error_carries_original_response_length(monkeypatch):
    """The caught exception must retain the raw text (via .doc) so callers
    can log response_length without re-plumbing the raw string through."""
    truncated = '{"prospects": [{"name": "Acme", "reason": "no website presen'
    fake = _make_fake_create([truncated])
    monkeypatch.setattr(main.client.chat.completions, "create", fake)

    with pytest.raises(_json.JSONDecodeError) as exc_info:
        await main._call_gpt_json_with_retry(
            lambda correction: [{"role": "user", "content": "score these"}],
            label="test", retries=0,
        )
    assert len(exc_info.value.doc) == len(truncated)

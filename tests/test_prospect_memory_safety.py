"""
Post-incident fix. A production Render instance (512MB free-tier memory
limit) hit its memory limit and auto-restarted during a 58-prospect
Prospect Discovery scan, causing a user-facing "could not connect to
backend" failure. Root cause: _fetch_homepage_html_safe called
`resp.text`, which decodes an ENTIRE response body into memory before any
truncation ever ran — one unexpectedly large real-world homepage had no
actual cap during the fetch itself. Measured live (see
tests/manual_memory_repro*.py, not part of this suite): pre-fix, a
58-business scan against an adversarial 30-80MB page mix peaked at 1024MB
RSS (200% of the 512MB budget); post-fix, the same scan peaked at 184MB
(36%). This file covers the three deterministic pieces of that fix:
bounded homepage fetch, the max_prospects safety cap, and _COMMAND_TASKS
eviction (a second, unrelated unbounded-growth bug found during the same
audit — every full_report/campaign_launch_kit command's task entry was
kept in memory for the life of the process, with no eviction despite
GET /command/status/{task_id} already claiming a task "may have expired").
"""
import sys
import os
import time
import threading
import http.server
import socketserver

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main
from main import (
    _fetch_homepage_html_safe, _HOMEPAGE_ANALYSIS_MAX_CHARS, _HOMEPAGE_FETCH_MAX_BYTES,
    _COMMAND_TASKS, _evict_stale_command_tasks, _COMMAND_TASK_TTL_SECONDS,
    prospect_discovery, ProspectDiscoveryRequest,
)


# ── _fetch_homepage_html_safe: bounded read ─────────────────────────────────

class _HugePageHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        try:
            self.wfile.write(b"<html><head><title>t</title></head><body>")
            # 20MB, well past both the fetch cap and the analysis cap —
            # written incrementally so the test server itself doesn't
            # balloon in memory just to serve it.
            chunk = b"x" * 65536
            for _ in range(320):
                self.wfile.write(chunk)
            self.wfile.write(b"</body></html>")
        except (BrokenPipeError, ConnectionResetError):
            pass  # expected once the client aborts after hitting its cap


@pytest.fixture(scope="module")
def huge_page_server():
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _HugePageHandler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}/"
    httpd.shutdown()


@pytest.mark.asyncio
async def test_homepage_fetch_caps_a_huge_page_instead_of_loading_it_whole(huge_page_server):
    result = await _fetch_homepage_html_safe(huge_page_server)
    assert result["fetch_status"] == "ok"
    # The whole point of the fix: capped regardless of how large the real
    # page actually is, not just "usually small enough in practice".
    assert len(result["html"]) <= _HOMEPAGE_ANALYSIS_MAX_CHARS
    assert len(result["html"]) < 20 * 1024 * 1024  # nowhere near the real 20MB page


@pytest.mark.asyncio
async def test_homepage_fetch_empty_url_is_a_noop():
    result = await _fetch_homepage_html_safe("")
    assert result == {"html": "", "fetch_status": "ok", "detail": ""}


def test_fetch_byte_cap_is_meaningfully_smaller_than_a_real_huge_page():
    # Guards against a future edit accidentally widening the cap back to
    # "unbounded" without anyone noticing — the cap only does its job if
    # it's actually well below what a real oversized page can be.
    assert _HOMEPAGE_FETCH_MAX_BYTES <= 5 * 1024 * 1024


# ── max_prospects safety cap ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_max_prospects_over_cap_is_rejected_with_a_clear_message():
    # No mocking needed — this check runs before any Places/GPT/DB call.
    result = await prospect_discovery(ProspectDiscoveryRequest(industry="dentist", city="Jaipur", max_prospects=200))
    assert result["success"] is False
    assert "60" in result["error"]
    assert "capped" in result["error"].lower()


@pytest.mark.asyncio
async def test_max_prospects_at_exactly_the_cap_is_not_rejected_by_the_guard():
    # Should pass the cap check and proceed to the next step (Places API,
    # which returns [] with no key configured) rather than being rejected
    # for being "too large" — 60 itself is a valid, allowed value.
    result = await prospect_discovery(ProspectDiscoveryRequest(industry="dentist", city="Jaipur", max_prospects=60))
    assert not (result.get("success") is False and "capped" in (result.get("error") or "").lower())


# ── _COMMAND_TASKS eviction ──────────────────────────────────────────────────

def _reset_command_tasks():
    _COMMAND_TASKS.clear()


def test_stale_terminal_tasks_are_evicted():
    _reset_command_tasks()
    old = time.time() - _COMMAND_TASK_TTL_SECONDS - 60
    _COMMAND_TASKS["old_done"] = {"status": "done", "created_at": old}
    _COMMAND_TASKS["old_error"] = {"status": "error", "created_at": old}
    _evict_stale_command_tasks()
    assert "old_done" not in _COMMAND_TASKS
    assert "old_error" not in _COMMAND_TASKS
    _reset_command_tasks()


def test_recent_terminal_tasks_are_kept():
    _reset_command_tasks()
    _COMMAND_TASKS["recent_done"] = {"status": "done", "created_at": time.time()}
    _evict_stale_command_tasks()
    assert "recent_done" in _COMMAND_TASKS
    _reset_command_tasks()


def test_old_but_still_running_tasks_are_never_evicted():
    # A task must never be evicted out from under a still-in-progress
    # command just because it's been running a while.
    _reset_command_tasks()
    old = time.time() - _COMMAND_TASK_TTL_SECONDS - 60
    _COMMAND_TASKS["long_running"] = {"status": "running", "created_at": old}
    _evict_stale_command_tasks()
    assert "long_running" in _COMMAND_TASKS
    _reset_command_tasks()


def test_eviction_is_safe_on_an_empty_or_untouched_store():
    _reset_command_tasks()
    _evict_stale_command_tasks()  # must not raise
    assert _COMMAND_TASKS == {}

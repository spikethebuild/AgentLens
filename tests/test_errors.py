"""Failure headlines, active/idle session state, and plugin+settings dedup (0.1.1 fixes)."""

from __future__ import annotations

import json

import pytest

from agentlens import errors, hook, ingest, render

# Shapes taken from real PostToolUseFailure payloads (contents shortened).
CASES = [
    ("Exit code 1\n{\"message\":\"Not Found\",\"status\":\"404\"}gh: Not Found (HTTP 404)\n"
     "{\"message\":\"Not Found\",\"status\":\"404\"}gh: Not Found (HTTP 404)",
     "gh: Not Found (HTTP 404)"),
    ("Exit code 1\nERROR: Package 'agentlens-local' requires a different Python: 3.9.6 not in '>=3.10'\n"
     "WARNING: You are using pip version 21.2.4; however, version 26.0.1 is available.\n"
     "You should consider upgrading via the '/tmp/venv/bin/python -m pip install --upgrade pip' command.",
     "ERROR: Package 'agentlens-local' requires a different Python: 3.9.6 not in '>=3.10'"),
    ("Exit code 1\nTraceback (most recent call last):\n  File \"x.py\", line 1, in <module>\n"
     "    import nope\nModuleNotFoundError: No module named 'nope'",
     "ModuleNotFoundError: No module named 'nope'"),
    ("Exit code 1\nactivate\nactivate.bat\n/Users/me/.venv/bin/python: No module named pip",
     "/Users/me/.venv/bin/python: No module named pip"),
    ("Exit code 2\n[build-system]\nrequires = [\"setuptools>=68\"]\nname = \"x\"",
     "name = \"x\""),  # no error-like line: last line
    ("File does not exist.", "File does not exist."),
    ("Exit code 1", ""),
    ("", ""),
]


@pytest.mark.parametrize("text, want", CASES)
def test_headline(text, want):
    assert errors.headline(text) == want


def test_headline_is_idempotent():
    for text, _ in CASES:
        h = errors.headline(text)
        assert errors.headline(h) == h


def test_exit_code():
    assert errors.exit_code("Exit code 127\nsh: x: command not found") == 127
    assert errors.exit_code("File does not exist.") is None


def _row(**kw):
    base = {"session_id": "s", "ended_at": None, "last_seen": 1000.0, "end_reason": None,
            "started_at": 900.0}
    base.update(kw)
    return base


def test_session_state_active_idle_ended():
    now = 1000.0
    assert render.session_state(_row(), now) == "active"
    assert render.session_state(_row(), now + render.IDLE_AFTER_S) == "active"
    assert render.session_state(_row(), now + render.IDLE_AFTER_S + 1) == "idle"
    assert render.session_state(_row(ended_at=1000.0, end_reason="other"), now + 99_999) == "ended (other)"
    # events after a SessionEnd (resumed session): not ended
    assert render.session_state(_row(ended_at=950.0), now) == "active"


def test_idle_duration_does_not_claim_open():
    text, tag = render.session_duration(_row())
    assert text.startswith("≥ ") and "no SessionEnd" in text and tag == render.CALC


def test_error_message_legacy_and_new():
    legacy = {"type": "tool.failure", "data": json.dumps({"exit_code": 1, "error": CASES[0][0]})}
    assert render.error_message(legacy) == "exit 1 · gh: Not Found (HTTP 404)"
    turn = {"type": "turn.failure", "data": json.dumps({"error": "rate_limit"})}
    assert render.error_message(turn) == "rate_limit"
    assert render.error_message({"type": "tool.failure", "data": "{}"}) == render.UNAVAILABLE


@pytest.mark.parametrize("name", ["stop", "user_prompt_submit", "stop_failure", "notification"])
def test_plugin_and_settings_copies_dedupe_across_seconds(fixture, monkeypatch, name):
    payload = fixture(name)
    if "prompt_id" not in payload:
        payload["prompt_id"] = "p-1"
    raw = json.dumps(payload).encode()
    monkeypatch.delenv("AGENTLENS_VIA", raising=False)
    hook.handle("claude-code", raw, 1_763_000_000.9)
    monkeypatch.setenv("AGENTLENS_VIA", "plugin")
    hook.handle("claude-code", raw, 1_763_000_001.2)  # next second
    r = ingest.ingest()
    assert (r.inserted, r.duplicates) == (1, 1)


def test_events_without_prompt_id_keep_real_repeats(fixture):
    payload = fixture("session_start")
    assert "prompt_id" not in payload
    raw = json.dumps(payload).encode()
    hook.handle("claude-code", raw, 1_763_000_000.0)
    hook.handle("claude-code", raw, 1_763_000_600.0)  # a later, real repeat
    r = ingest.ingest()
    assert r.inserted == 2


def test_status_line_refresh_does_not_keep_a_session_active(fixture):
    import time

    from agentlens import spool, store
    from agentlens.adapters import claude_code
    from agentlens.events import Category, Event, Provenance, TsSource, payload_event_id

    now = time.time()
    stop = fixture("stop")
    spool.write_events(claude_code.normalize(stop, now - 3 * 3600, {}))
    spool.write_events([Event(
        event_id=payload_event_id("claude-code", {"k": "plan"}, now), provider="claude-code",
        session_id=stop["session_id"], project=None, category=Category.USAGE, type="plan.usage",
        ts=now, ts_source=TsSource.COLLECTOR, source="statusline", provenance=Provenance.OBSERVED,
    )])
    ingest.ingest()
    conn = store.connect()
    try:
        s = store.list_sessions(conn, 1)[0]
    finally:
        conn.close()
    assert s["last_seen"] == pytest.approx(now)
    assert render.session_state(s, now) == "idle"

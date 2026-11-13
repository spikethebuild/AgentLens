"""Data for the AgentLens pane inside Claude Code (``agentlens pane-data``).

One compact, read-only JSON snapshot of what AgentLens recorded for one
session: token totals, tool calls and failures, recent events, and the last
plan-usage snapshot from the opt-in status line. Every number carries its
provenance (``obs``/``calc``/``clk``). Values come from the database, which only
ever holds redacted data; nothing here adds content.
"""

from __future__ import annotations

import sqlite3
import time

from agentlens import render, statusline, store

SCHEMA = 1
FAILURES = 5
EVENTS = 10


def _session(conn: sqlite3.Connection, session_id: str | None) -> sqlite3.Row | None:
    if session_id:
        try:
            return store.find_session(conn, session_id)
        except store.AmbiguousSession:
            return None
    rows = store.list_sessions(conn, 1)
    return rows[0] if rows else None


def snapshot(conn: sqlite3.Connection, session_id: str | None, now: float | None = None) -> dict:
    from agentlens import cache

    now = time.time() if now is None else now
    out: dict = {"schema": SCHEMA, "generated_at": now, "session": None, "plan": None,
                 "failures": [], "events": []}

    snap = statusline.load_snapshot()
    windows = statusline.current_windows(snap, now)
    if windows:
        out["plan"] = {
            "as_of": snap["captured_at"], "as_of_provenance": "clk",
            "windows": [{"name": n, "used_percentage": w["used_percentage"], "resets_at": w.get("resets_at"),
                         "provenance": "obs"} for n, w in windows.items()],
        }

    s = _session(conn, session_id)
    if s is None:
        out["reason"] = ("AgentLens has no record of this session yet (recording starts with the next hook event)"
                         if session_id else "no sessions recorded yet")
        return out
    provider, sid = s["provider"], s["session_id"]
    usage = store.session_usage(conn, provider, sid)
    out["session"] = {
        "id": sid, "model": s["model"], "project": s["project"],
        "state": render.session_state(s), "last_event": s["last_seen"],
        "prompts": s["prompts"], "tool_calls": s["tool_calls"], "tool_failures": s["tool_failures"],
        "tokens": {
            "requests": usage["requests"], "subagent_requests": usage["sub_requests"],
            "input": usage["input"], "cache_read": usage["cache_read"], "cache_write": usage["cache_write"],
            "uncached": usage["uncached"], "output": usage["output"], "hit_ratio": usage["hit_ratio"],
            "provenance": "obs", "hit_ratio_provenance": "calc",
        } if usage["requests"] else None,
        "tokens_reason": None if usage["requests"] else cache.usage_reason(conn, provider, sid),
    }
    events = store.session_events(conn, provider, sid)
    out["failures"] = [
        {"ts": e["ts"], "source": e["tool_name"] or "turn", "message": render.clip(render.error_message(e), 160)}
        for e in render.error_events(events)[-FAILURES:]
    ]
    out["events"] = [
        {"ts": e["ts"], "type": e["type"], "detail": render.clip(str(render.event_detail(e)), 100)}
        for e in events[-EVENTS:]
    ]
    return out

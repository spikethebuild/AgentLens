"""Plain-text rendering for the v0.1 CLI.

Every metric is tagged with where it came from:
  [obs]  reported by the provider (Claude Code)
  [calc] derived by AgentLens from collected events
  [est]  estimated by AgentLens (none in v0.1)
  [clk]  a timestamp from the collector clock: when AgentLens received the hook
Missing values are printed as "unavailable" with a reason, never guessed.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Iterable, Sequence

OBS, CALC, EST, CLK = "[obs]", "[calc]", "[est]", "[clk]"
UNAVAILABLE = "unavailable"

LEGEND = (
    "[obs] reported by Claude Code · [calc] derived by AgentLens · [est] estimated · "
    "[clk] collector clock (time AgentLens received the hook)"
)


# ------------------------------------------------------------------ formats


def fmt_ts(ts: float | None, short: bool = False) -> str:
    if ts is None:
        return UNAVAILABLE
    return time.strftime("%m-%d %H:%M:%S" if short else "%Y-%m-%d %H:%M:%S", time.localtime(ts))


def fmt_duration_ms(ms: float | None) -> str:
    if ms is None:
        return UNAVAILABLE
    if ms < 1000:
        return f"{ms:.0f} ms"
    s = ms / 1000
    if s < 60:
        return f"{s:.2f} s"
    m, s = divmod(int(round(s)), 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def short_path(p: str | None, width: int = 40) -> str:
    if not p:
        return "-"
    home = os.path.expanduser("~")
    if p == home or p.startswith(home + os.sep):
        p = "~" + p[len(home):]
    return p if len(p) <= width else "…" + p[-(width - 1):]


def clip(s: str, width: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= width else s[: width - 1] + "…"


def table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    rows = [[str(c) for c in r] for r in rows]
    widths = [len(h) for h in headers]
    for r in rows:
        widths = [max(w, len(c)) for w, c in zip(widths, r)]
    line = lambda cells: "  ".join(c.ljust(w) for c, w in zip(cells, widths)).rstrip()  # noqa: E731
    out = [line(headers), line(["─" * w for w in widths])]
    out += [line(r) for r in rows]
    return "\n".join(out)


def kv(rows: Sequence[tuple[str, str, str]]) -> str:
    """label / value / provenance-tag rows, aligned."""
    lw = max(len(r[0]) for r in rows)
    vw = max(len(r[1]) for r in rows)
    return "\n".join(f"  {k.ljust(lw)}  {v.ljust(vw)}  {tag}".rstrip() for k, v, tag in rows)


# ------------------------------------------------------------- session logic


def session_state(s: sqlite3.Row) -> str:
    if s["ended_at"] is not None and s["ended_at"] >= s["last_seen"]:
        return f"ended ({s['end_reason'] or 'reason unavailable'})"
    return "open"


def session_duration(s: sqlite3.Row) -> tuple[str, str]:
    """(text, tag). Never derived from the first *seen* event: that would be a guess."""
    if s["started_at"] is None:
        return f"{UNAVAILABLE} (no SessionStart)", ""
    if s["ended_at"] is not None and s["ended_at"] >= s["last_seen"]:
        return fmt_duration_ms((s["ended_at"] - s["started_at"]) * 1000), CALC
    return "≥ " + fmt_duration_ms((s["last_seen"] - s["started_at"]) * 1000) + " (open)", CALC


def tool_summary(tool_name: str | None, data_json: str | None) -> str:
    try:
        data = json.loads(data_json) if data_json else {}
    except ValueError:
        data = {}
    inp = data.get("input") or {}
    if not isinstance(inp, dict) or not inp:
        return ""
    if "command" in inp:
        return str(inp["command"])
    for key in ("file_path", "notebook_path"):
        if key in inp:
            extra = ""
            size = inp.get("content_size")
            if isinstance(size, dict):
                extra = f" ({size.get('bytes')} B written)"
            return short_path(str(inp[key]), 60) + extra
    if "pattern" in inp:
        return f"{inp['pattern']}" + (f" in {short_path(str(inp['path']), 30)}" if inp.get("path") else "")
    if "url" in inp:
        return str(inp["url"])
    if "query" in inp:
        return str(inp["query"])
    if "subagent_type" in inp:
        return f"{inp['subagent_type']}: {inp.get('description', '')}"
    parts = []
    for k, v in list(inp.items())[:4]:
        parts.append(f"{k}={v if not isinstance(v, dict) else '<' + ','.join(f'{a}:{b}' for a, b in v.items()) + '>'}")
    return " ".join(parts)


def _status_detail(call: sqlite3.Row) -> str:
    status = call["status"]
    try:
        res = json.loads(call["result_data"]) if call["result_data"] else {}
    except ValueError:
        res = {}
    if status == "failed" and "exit_code" in res:
        return f"failed (exit {res['exit_code']})"
    return "no result" if status == "no_result" else status


# ------------------------------------------------------------------ commands


def render_sessions(rows: Sequence[sqlite3.Row]) -> str:
    if not rows:
        return "No sessions recorded yet. Run `agentlens install`, then start Claude Code."
    headers = [
        "SESSION", "PROJECT", "STARTED [clk]", "DURATION [calc]", "STATE",
        "PROMPTS [calc]", "TOOLS [calc]", "FAILED [calc]",
    ]
    body = []
    for s in rows:
        dur, _ = session_duration(s)
        body.append([
            s["session_id"][:8],
            short_path(s["project"], 28),
            fmt_ts(s["started_at"], short=True) if s["started_at"] else "unavailable",
            dur,
            session_state(s),
            s["prompts"],
            s["tool_calls"],
            s["tool_failures"],
        ])
    return table(headers, body) + "\n\n" + LEGEND


def render_inspect(
    s: sqlite3.Row,
    calls: Sequence[sqlite3.Row],
    events: Sequence[sqlite3.Row],
    timeline_limit: int | None = 60,
) -> str:
    dur, dur_tag = session_duration(s)
    by_status: dict[str, int] = {}
    for c in calls:
        by_status[c["status"]] = by_status.get(c["status"], 0) + 1
    timed = [c for c in calls if c["duration_ms"] is not None]
    n_obs = sum(1 for c in timed if c["duration_provenance"] == "observed")
    n_calc = len(timed) - n_obs
    types = [e["type"] for e in events]
    approvals_req = types.count("approval.request")
    approvals_den = types.count("approval.denied")

    status_bits = ", ".join(
        f"{by_status.get(k, 0)} {label}"
        for k, label in (("ok", "ok"), ("failed", "failed"), ("interrupted", "interrupted"),
                         ("denied", "denied"), ("no_result", "no result"))
        if by_status.get(k)
    )
    tool_time = (
        f"{fmt_duration_ms(sum(c['duration_ms'] for c in timed))} "
        f"(sum over {len(timed)} of {len(calls)} calls: {n_obs} obs, {n_calc} calc)"
        if timed else f"{UNAVAILABLE} (no timed tool calls)"
    )
    started = (
        f"{fmt_ts(s['started_at'])} ({s['start_source'] or 'source unavailable'})"
        if s["started_at"] else f"{UNAVAILABLE} (no SessionStart; AgentLens may have been installed mid-session)"
    )
    ended = (
        f"{fmt_ts(s['ended_at'])} ({s['end_reason'] or 'reason unavailable'})"
        if s["ended_at"] else "no SessionEnd received (open, or ended without the hook running)"
    )
    rows = [
        ("provider", s["provider"], ""),
        ("project", s["project"] or UNAVAILABLE, OBS if s["project"] else ""),
        ("model", s["model"] or f"{UNAVAILABLE} (not reported by Claude Code)", OBS if s["model"] else ""),
        ("permission mode", s["permission_mode"] or UNAVAILABLE, OBS if s["permission_mode"] else ""),
        ("started", started, CLK if s["started_at"] else ""),
        ("ended", ended, CLK if s["ended_at"] else ""),
        ("first / last event", f"{fmt_ts(s['first_seen'])} / {fmt_ts(s['last_seen'])}", CLK),
        ("session duration", dur, dur_tag),
        ("events", str(len(events)), CALC),
        ("prompts", str(s["prompts"]), CALC),
        ("tool calls", f"{len(calls)}" + (f" ({status_bits})" if status_bits else ""), CALC),
        ("tool time", tool_time, CALC if timed else ""),
        ("subagents", str(types.count("subagent.start")), CALC),
        ("approvals", f"{approvals_req} requested, {approvals_den} denied", CALC),
        ("turn failures", str(types.count("turn.failure")), CALC),
        ("tokens", f"{UNAVAILABLE} (not in hook payloads; planned for v0.3)", ""),
        ("cost", f"{UNAVAILABLE} (planned for v0.3)", ""),
    ]
    out = [f"Session {s['session_id']}", kv(rows)]

    out.append("\nTool calls")
    if calls:
        trows = []
        for c in calls:
            prov = {"observed": OBS, "calculated": CALC}.get(c["duration_provenance"] or "", "")
            trows.append([
                fmt_ts(c["started_at"] or c["ended_at"], short=True),
                c["tool_name"] or "?",
                _status_detail(c),
                f"{fmt_duration_ms(c['duration_ms'])} {prov}".strip(),
                clip(tool_summary(c["tool_name"], c["pre_data"]), 70),
            ])
        out.append(table(["TIME [clk]", "TOOL", "STATUS", "DURATION", "INPUT (sanitized)"], trows))
    else:
        out.append("  none recorded")

    failures = [e for e in events if e["type"] in ("tool.failure", "turn.failure")]
    if failures:
        out.append("\nErrors")
        for e in failures:
            data = json.loads(e["data"])
            msg = data.get("error") or ""
            if data.get("error_details"):
                msg += f": {data['error_details']}"
            label = e["tool_name"] or "turn"
            out.append(f"  {fmt_ts(e['ts'], short=True)}  {label:<8} {clip(msg, 90)}")

    shown = events if timeline_limit is None else events[-timeline_limit:]
    out.append(
        f"\nTimeline ({len(shown)} of {len(events)} events)" if len(shown) < len(events) else "\nTimeline"
    )
    erows = []
    for e in shown:
        data = json.loads(e["data"])
        detail = ""
        if e["tool_name"]:
            detail = e["tool_name"] + (f" {e['status']}" if e["status"] else "")
        elif e["type"] == "prompt.submit":
            detail = f"{data.get('prompt_chars', '?')} chars (text not stored)" if "prompt_text" not in data else clip(data["prompt_text"], 60)
        elif e["type"] == "session.start":
            detail = data.get("start_source", "")
        elif e["type"] == "session.end":
            detail = data.get("end_reason", "")
        elif e["type"] == "notification":
            detail = data.get("notification_type") or data.get("message", "")
        elif e["type"].startswith("subagent."):
            detail = data.get("agent_type", "")
        elif e["type"] == "turn.failure":
            detail = data.get("error", "")
        elif e["type"] == "session.compact":
            detail = data.get("trigger", "")
        erows.append([fmt_ts(e["ts"], short=True), e["type"], clip(str(detail), 70)])
    out.append(table(["TIME [clk]", "EVENT", "DETAIL"], erows))
    out.append("\n" + LEGEND)
    return "\n".join(out)

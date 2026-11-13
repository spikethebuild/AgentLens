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

from agentlens import errors

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


def fmt_tokens(n: int | None) -> str:
    if n is None:
        return UNAVAILABLE
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 10_000:
        return f"{n / 1000:.0f}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


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


# A session with no SessionEnd is "active" while events keep arriving and "idle"
# once it has been quiet this long. Closing the terminal or a crash sends no
# SessionEnd, so without this such sessions would look open forever. Neither label
# claims the session ended: AgentLens cannot know that.
IDLE_AFTER_S = 60 * 60


def is_ended(s: sqlite3.Row) -> bool:
    return s["ended_at"] is not None and s["ended_at"] >= s["last_seen"]


def session_state(s: sqlite3.Row, now: float | None = None) -> str:
    """"ended (<reason>)", "active" or "idle" (no SessionEnd, quiet > IDLE_AFTER_S)."""
    if is_ended(s):
        return f"ended ({s['end_reason'] or 'reason unavailable'})"
    now = time.time() if now is None else now
    return "active" if now - last_activity(s) <= IDLE_AFTER_S else "idle"


def last_activity(s: sqlite3.Row) -> float:
    """Time of the session's last hook event.

    Status-line refreshes also record ``plan.usage`` events, and Claude Code runs
    the status line for idle sessions too, so they do not count as activity.
    """
    try:
        ts = s["last_activity"]
    except (IndexError, KeyError):
        ts = None
    return ts if ts is not None else s["last_seen"]


def is_active(s: sqlite3.Row, now: float | None = None) -> bool:
    return session_state(s, now) == "active"


def session_duration(s: sqlite3.Row) -> tuple[str, str]:
    """(text, tag). Never derived from the first *seen* event: that would be a guess."""
    if s["started_at"] is None:
        return f"{UNAVAILABLE} (no SessionStart)", ""
    if is_ended(s):
        return fmt_duration_ms((s["ended_at"] - s["started_at"]) * 1000), CALC
    return "≥ " + fmt_duration_ms((s["last_seen"] - s["started_at"]) * 1000) + " (no SessionEnd)", CALC


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


def parse_data(data_json: str | None) -> dict:
    try:
        data = json.loads(data_json) if data_json else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def status_detail(call: sqlite3.Row) -> str:
    status = call["status"]
    res = parse_data(call["result_data"])
    if status == "failed" and "exit_code" in res:
        return f"failed (exit {res['exit_code']})"
    return "no result" if status == "no_result" else status


def duration_tag(call: sqlite3.Row) -> str:
    return {"observed": OBS, "calculated": CALC}.get(call["duration_provenance"] or "", "")


def duration_parts(call: sqlite3.Row) -> tuple[str, str]:
    """(duration, provenance tag), or why the duration is unavailable."""
    if call["duration_ms"] is None:
        if call["status"] == "no_result":
            return f"{UNAVAILABLE} (no result recorded)", ""
        if call["started_at"] is None:
            return f"{UNAVAILABLE} (no start event)", ""
        return UNAVAILABLE, ""
    return fmt_duration_ms(call["duration_ms"]), duration_tag(call)


def duration_text(call: sqlite3.Row) -> str:
    return " ".join(x for x in duration_parts(call) if x)


def tool_call_fields(call: sqlite3.Row) -> list[tuple[str, str, str]]:
    """Every stored fact about one tool call, untruncated, as kv() rows."""
    pre = parse_data(call["pre_data"])
    res = parse_data(call["result_data"])
    rows: list[tuple[str, str, str]] = [
        ("tool", call["tool_name"] or "?", OBS if call["tool_name"] else ""),
        ("tool use id", call["tool_use_id"] or UNAVAILABLE, OBS if call["tool_use_id"] else ""),
        ("status", status_detail(call), OBS if call["status"] not in ("no_result", "denied") else CALC),
        ("started", fmt_ts(call["started_at"]) if call["started_at"] else f"{UNAVAILABLE} (no PreToolUse)",
         CLK if call["started_at"] else ""),
        ("finished", fmt_ts(call["ended_at"]) if call["ended_at"] else f"{UNAVAILABLE} (no result)",
         CLK if call["ended_at"] else ""),
        ("duration", *duration_parts(call)),
    ]
    inp = pre.get("input") if isinstance(pre.get("input"), dict) else None
    if inp is None:
        inp = res.get("input") if isinstance(res.get("input"), dict) else None
    if inp:
        for k, v in inp.items():
            if isinstance(v, dict):
                v = ", ".join(f"{a}={b}" for a, b in v.items())
            rows.append((f"input.{k}", str(v), OBS))
    else:
        rows.append(("input", f"{UNAVAILABLE} (not recorded)", ""))
    if "exit_code" in res:
        rows.append(("exit code", str(res["exit_code"]), OBS))
    if res.get("is_interrupt"):
        rows.append(("interrupted", "yes (user interrupt)", OBS))
    if res.get("error"):
        rows.append(("error", errors.headline(str(res["error"])) or str(res["error"]), OBS))
    if res.get("error_chars"):
        rows.append(("error size", f"{res['error_chars']} chars, {res.get('error_lines', '?')} lines (rest not stored)", OBS))
    return rows


def error_events(events: Sequence[sqlite3.Row]) -> list[sqlite3.Row]:
    return [e for e in events if e["type"] in ("tool.failure", "turn.failure")]


def error_message(e: sqlite3.Row) -> str:
    """One line: ``exit N · <headline>`` for tool failures, ``<error>: <details>`` for turns.

    Events recorded before 0.1.1 hold up to 1000 chars of raw output; the headline
    is re-derived here so they read the same as new ones.
    """
    data = parse_data(e["data"])
    msg = errors.headline(str(data.get("error") or ""))
    if data.get("exit_code") is not None:
        msg = f"exit {data['exit_code']}" + (f" · {msg}" if msg else "")
    if data.get("error_details"):
        msg += f": {data['error_details']}"
    return msg or UNAVAILABLE


def event_detail(e: sqlite3.Row) -> str:
    """One-line description of a timeline event."""
    data = parse_data(e["data"])
    t = e["type"]
    if e["tool_name"]:
        return e["tool_name"] + (f" {e['status']}" if e["status"] else "")
    if t == "prompt.submit":
        if "prompt_text" in data:
            return str(data["prompt_text"])
        return f"{data.get('prompt_chars', '?')} chars (text not stored)"
    if t == "session.start":
        return data.get("start_source", "")
    if t == "session.end":
        return data.get("end_reason", "")
    if t == "notification":
        return data.get("notification_type") or data.get("message", "")
    if t.startswith("subagent."):
        return data.get("agent_type", "")
    if t == "turn.failure":
        return data.get("error", "")
    if t == "session.compact":
        return data.get("trigger", "")
    return ""


# ------------------------------------------------------------------ commands


def render_sessions(rows: Sequence[sqlite3.Row]) -> str:
    if not rows:
        return "No sessions recorded yet. Run `agentlens install`, then start Claude Code."
    headers = [
        "SESSION", "PROJECT", "STARTED [clk]", "DURATION [calc]", "STATE",
        "PROMPTS [calc]", "TOOLS [calc]", "FAILED [calc]", "TOKENS IN/OUT [obs]",
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
            f"{fmt_tokens(s['tokens_in'])} / {fmt_tokens(s['tokens_out'])}" if s["api_requests"] else UNAVAILABLE,
        ])
    return table(headers, body) + "\n\n" + LEGEND


def token_rows(usage: dict | None, reason: str | None = None) -> list[tuple[str, str, str]]:
    """Per-session token totals (main conversation + subagents) with provenance."""
    if not usage or not usage.get("requests"):
        why = reason or "no per-request usage recorded for this session"
        return [("tokens", f"{UNAVAILABLE} ({why})", "")]
    reqs = f"{usage['main_requests']} main conversation, {usage['sub_requests']} subagent"
    if usage["incomplete"]:
        reqs += f" ({usage['incomplete']} missing usage fields)"
    ratio = usage["hit_ratio"]
    return [
        ("API requests", reqs, CALC),
        ("input tokens",
         f"{fmt_tokens(usage['input'])} (cache read {fmt_tokens(usage['cache_read'])}, "
         f"cache written {fmt_tokens(usage['cache_write'])}, uncached {fmt_tokens(usage['uncached'])})", OBS),
        ("output tokens", fmt_tokens(usage["output"]), OBS),
        ("cache hit ratio",
         f"{ratio:.1%} (main conversation; details: agentlens cache)" if ratio is not None
         else f"{UNAVAILABLE} (no complete main-conversation requests)", CALC if ratio is not None else ""),
    ]


def session_overview_rows(
    s: sqlite3.Row, calls: Sequence[sqlite3.Row], events: Sequence[sqlite3.Row],
    usage: dict | None = None, usage_reason: str | None = None,
) -> list[tuple[str, str, str]]:
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
        if s["ended_at"] else "no SessionEnd received (still running, or closed without the hook firing)"
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
        *token_rows(usage, usage_reason),
        ("cost", f"{UNAVAILABLE} (pricing planned for v0.3)", ""),
    ]
    return rows


def render_inspect(
    s: sqlite3.Row,
    calls: Sequence[sqlite3.Row],
    events: Sequence[sqlite3.Row],
    timeline_limit: int | None = 60,
    usage: dict | None = None,
    usage_reason: str | None = None,
) -> str:
    rows = session_overview_rows(s, calls, events, usage, usage_reason)
    out = [f"Session {s['session_id']}", kv(rows)]

    out.append("\nTool calls")
    if calls:
        trows = []
        for c in calls:
            trows.append([
                fmt_ts(c["started_at"] or c["ended_at"], short=True),
                c["tool_name"] or "?",
                status_detail(c),
                duration_text(c),
                clip(tool_summary(c["tool_name"], c["pre_data"]), 70),
            ])
        out.append(table(["TIME [clk]", "TOOL", "STATUS", "DURATION", "INPUT (sanitized)"], trows))
    else:
        out.append("  none recorded")

    failures = error_events(events)
    if failures:
        out.append("\nErrors")
        for e in failures:
            label = e["tool_name"] or "turn"
            out.append(f"  {fmt_ts(e['ts'], short=True)}  {label:<8} {clip(error_message(e), 90)}")

    shown = events if timeline_limit is None else events[-timeline_limit:]
    out.append(
        f"\nTimeline ({len(shown)} of {len(events)} events)" if len(shown) < len(events) else "\nTimeline"
    )
    erows = []
    for e in shown:
        erows.append([fmt_ts(e["ts"], short=True), e["type"], clip(str(event_detail(e)), 70)])
    out.append(table(["TIME [clk]", "EVENT", "DETAIL"], erows))
    out.append("\n" + LEGEND)
    return "\n".join(out)


def plan_usage_rows(snap: dict | None, installed: bool, now: float,
                    window_tokens: dict | None = None) -> list[tuple[str, str, str]]:
    """Claude plan-usage windows from the status line snapshot. Never a plan name or token limit."""
    from agentlens import statusline

    windows = statusline.current_windows(snap, now)
    if not windows:
        if not installed and not snap:
            why = "enable with `agentlens statusline install`"
        elif not snap:
            why = "not reported yet: Claude Code sends it to Pro/Max/Ultra plans after the first response"
        else:
            why = "the last reported windows have reset; waiting for Claude Code to report again"
        return [("plan usage", f"{UNAVAILABLE} ({why})", "")]
    rows = []
    labels = {"five_hour": "current session (5h)", "seven_day": "current week"}
    for name in statusline.WINDOWS:
        w = windows.get(name)
        if w is None:
            continue
        resets = w.get("resets_at")
        when = fmt_ts(resets, short=True) if resets else "reset time not reported"
        rows.append((labels[name], f"{w['used_percentage']:.0f}% used · resets {when}", OBS))
    age = max(0, now - snap["captured_at"])
    rows.append(("as of", f"{fmt_ts(snap['captured_at'], short=True)} ({_ago(age)})", CLK))
    if window_tokens is not None and "five_hour" in windows:
        if window_tokens["requests"]:
            rows.append(("tokens this session",
                         f"{fmt_tokens(window_tokens['input'])} in / {fmt_tokens(window_tokens['output'])} out "
                         f"(this machine)", CALC))
        else:
            rows.append(("tokens this session", f"{UNAVAILABLE} (no requests recorded in this window)", ""))
    return rows


def _ago(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min ago"
    return f"{seconds / 3600:.1f} h ago"

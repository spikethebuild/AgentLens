"""``agentlens statusline``: Claude Code status line command (opt-in).

Claude Code pipes a JSON object to the status line command after each assistant
message (debounced). For Pro/Max/Ultra subscribers it includes ``rate_limits``
with the 5-hour and 7-day windows' ``used_percentage`` and ``resets_at``. That
is the only documented source of plan-usage data; Claude Code exposes no plan
name and Anthropic publishes no token limit, so AgentLens shows neither.

Rules (same spirit as the hook fast path):
* Standard library only; never touches SQLite.
* Stores only ``session_id``, ``cwd`` and ``rate_limits``. It also *displays*
  the model name, ``context_window.used_percentage`` and ``prompt_cache``
  (hit ratio, misses, warm/ttl) but never writes them anywhere.
* Always prints a line and exits 0: a non-zero exit or empty output blanks the
  user's status bar.
* The latest snapshot goes to ``$AGENTLENS_HOME/plan_usage.json`` (atomic, 0600).
  A spool event is written only when a window's rounded percentage or reset
  time changes, so history stays small.
* If the user had a status line before ``agentlens statusline install``, it is
  saved and run with the same input; its output is printed below ours.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any

from agentlens import paths

MAX_PAYLOAD_BYTES = 4 * 1024 * 1024
WRAPPED_TIMEOUT_S = 3.0
WINDOWS = ("five_hour", "seven_day")
WINDOW_LABEL = {"five_hour": "session", "seven_day": "week"}
WINDOW_SECONDS = {"five_hour": 5 * 3600, "seven_day": 7 * 86400}
FILE_MODE = 0o600


def snapshot_path():
    return paths.home() / "plan_usage.json"


def previous_path():
    """The user's status line from before ``agentlens statusline install``."""
    return paths.home() / "statusline-previous.json"


# ------------------------------------------------------------------ parsing


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def parse(payload: Any, received_at: float) -> dict | None:
    """Snapshot of the documented rate-limit windows, or None if there are none."""
    if not isinstance(payload, dict):
        return None
    limits = payload.get("rate_limits")
    if not isinstance(limits, dict):
        return None
    windows = {}
    for name in WINDOWS:
        w = limits.get(name)
        if not isinstance(w, dict):
            continue
        pct, resets = _num(w.get("used_percentage")), _num(w.get("resets_at"))
        if pct is None or not 0 <= pct <= 100:
            continue
        windows[name] = {"used_percentage": pct, "resets_at": resets}
    if not windows:
        return None
    sid = payload.get("session_id")
    cwd = payload.get("cwd")
    return {
        "captured_at": received_at,
        "session_id": sid if isinstance(sid, str) and sid else None,
        "cwd": cwd if isinstance(cwd, str) else None,
        "windows": windows,
    }


def load_snapshot() -> dict | None:
    try:
        data = json.loads(snapshot_path().read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("windows"), dict):
        return None
    return data


def current_windows(snap: dict | None, now: float) -> dict:
    """Windows whose reset time has not passed (Claude Code drops a window at resets_at too)."""
    if not snap:
        return {}
    out = {}
    for name in WINDOWS:
        w = snap["windows"].get(name)
        if isinstance(w, dict) and (w.get("resets_at") is None or w["resets_at"] > now):
            out[name] = w
    return out


# ------------------------------------------------------------------ recording


def _key(snap: dict | None) -> dict:
    if not snap:
        return {}
    return {n: (round(w["used_percentage"]), w.get("resets_at")) for n, w in snap["windows"].items()}


def _write_snapshot(snap: dict) -> None:
    home = paths.ensure_dir(paths.home())
    target = snapshot_path()
    tmp = home / f".plan_usage.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    try:
        os.write(fd, json.dumps(snap, separators=(",", ":")).encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, target)


def _spool_event(snap: dict) -> None:
    from agentlens import spool
    from agentlens.events import Category, Event, Provenance, TsSource, _sha256

    sid = snap["session_id"] or "unknown"
    windows = snap["windows"]
    event_id = _sha256("claude-code", "plan.usage", sid,
                       json.dumps(_key(snap), sort_keys=True), str(int(snap["captured_at"])))
    spool.write_events([Event(
        event_id=event_id, provider="claude-code", session_id=sid, project=snap["cwd"],
        category=Category.USAGE, type="plan.usage", ts=snap["captured_at"],
        ts_source=TsSource.COLLECTOR, source="statusline", provenance=Provenance.OBSERVED,
        data={"windows": windows, "collector": "statusline"},
    )])


def record(snap: dict) -> bool:
    """Save the latest snapshot; spool an event if it changed. Returns True if it changed."""
    changed = _key(load_snapshot()) != _key(snap)
    _write_snapshot(snap)
    if changed:
        _spool_event(snap)
    return changed


# ------------------------------------------------------------------ output


def _clock(ts: float | None, now: float) -> str:
    if ts is None:
        return "?"
    fmt = "%H:%M" if ts - now < 86400 else "%a %H:%M"
    return time.strftime(fmt, time.localtime(ts))


def render_line(snap: dict | None, now: float) -> str:
    windows = current_windows(snap, now)
    if not windows:
        return "AgentLens · plan usage not reported"
    bits = []
    for name in WINDOWS:
        w = windows.get(name)
        if w is None:
            continue
        bit = f"{WINDOW_LABEL[name]} {w['used_percentage']:.0f}%"
        if name == "five_hour":
            bit += f" · resets {_clock(w.get('resets_at'), now)}"
        bits.append(bit)
    return " · ".join(bits)


def _clean(text: str, limit: int = 24) -> str:
    """Printable text only: the status bar must not carry escape sequences from input."""
    return "".join(c for c in text if c.isprintable())[:limit]


def session_line(payload: Any) -> str | None:
    """Model, context use and prompt-cache health from Claude Code's own status line input.

    Display only: none of these values is stored. Each part is left out when Claude
    Code has not reported it yet (``null`` early in a session, absent before the
    first response).
    """
    if not isinstance(payload, dict):
        return None
    bits = []
    model = payload.get("model")
    if isinstance(model, dict):
        name = model.get("display_name") or model.get("id")
        if isinstance(name, str) and name.strip():
            bits.append(_clean(name.strip()))
    cw = payload.get("context_window")
    if isinstance(cw, dict):
        used = _num(cw.get("used_percentage"))
        if used is not None and 0 <= used <= 100:
            bits.append(f"ctx {used:.0f}%")
    pc = payload.get("prompt_cache")
    if isinstance(pc, dict):
        ratio = _num(pc.get("hit_ratio"))
        if ratio is not None and 0 <= ratio <= 1:
            bit = f"cache {ratio:.0%} hit"
            misses = _num(pc.get("misses"))
            if misses is not None and misses >= 0:
                bit += f", {misses:.0f} miss" + ("" if misses == 1 else "es")
            warm, ttl = pc.get("warm"), pc.get("ttl")
            state = [x for x in ("warm" if warm is True else "cold" if warm is False else None,
                                 _clean(ttl, 4) if isinstance(ttl, str) and ttl else None) if x]
            if state:
                bit += f" ({', '.join(state)})"
            bits.append(bit)
    return " · ".join(bits) or None


def render(payload: Any, snap: dict | None, now: float) -> str:
    """The AgentLens rows: plan usage, then the session line. Never empty."""
    rows = []
    if current_windows(snap, now):
        rows.append(render_line(snap, now))
    extra = session_line(payload)
    if extra:
        rows.append(extra)
    return "\n".join(rows) or render_line(snap, now)


def _run_previous(raw: bytes) -> str:
    try:
        prev = json.loads(previous_path().read_text())
        command = prev["statusLine"]["command"]
    except (OSError, ValueError, KeyError, TypeError):
        return ""
    if not isinstance(command, str) or not command.strip():
        return ""
    try:
        r = subprocess.run(["sh", "-c", command], input=raw, capture_output=True, timeout=WRAPPED_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.decode("utf-8", "replace").rstrip("\n") if r.returncode == 0 else ""


def run() -> int:
    received_at = time.time()
    line = "AgentLens"
    raw = b""
    try:
        raw = sys.stdin.buffer.read(MAX_PAYLOAD_BYTES + 1)
        if len(raw) > MAX_PAYLOAD_BYTES:
            raise ValueError(f"status line payload too large ({len(raw)} bytes)")
        payload = json.loads(raw)
        snap = parse(payload, received_at)
        if snap is not None:
            record(snap)
        line = render(payload, snap, received_at)
    except BaseException as exc:  # noqa: BLE001 - must never blank the status bar
        try:
            from agentlens.errlog import log_error

            log_error(f"statusline failed: {type(exc).__name__}", exc)
        except BaseException:  # noqa: BLE001
            pass
    out = line
    try:
        wrapped = _run_previous(raw) if raw else ""
        if wrapped:
            out += "\n" + wrapped
    except BaseException:  # noqa: BLE001
        pass
    try:
        sys.stdout.write(out + "\n")
        sys.stdout.flush()
    except BaseException:  # noqa: BLE001
        pass
    return 0

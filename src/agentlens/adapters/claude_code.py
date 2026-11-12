"""Claude Code hook payloads -> normalized events.

Field names follow https://code.claude.com/docs/en/hooks (checked Nov 2026).
Every optional field is read defensively; a missing field is left out of
``data`` rather than filled with a guess.

Privacy: file contents, tool output, assistant messages and (by default)
prompt text are reduced to sizes/hashes here, before anything reaches disk.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from agentlens import config as config_mod
from agentlens.events import (
    Category,
    Event,
    Provenance,
    TsSource,
    payload_event_id,
    tool_event_id,
)
from agentlens.redact import clean, fingerprint, redact_obj

PROVIDER = "claude-code"
SOURCE = "hook"

# hook_event_name -> (category, normalized type, status)
EVENT_MAP: dict[str, tuple[Category, str, str | None]] = {
    "SessionStart": (Category.SESSION, "session.start", None),
    "SessionEnd": (Category.SESSION, "session.end", None),
    "UserPromptSubmit": (Category.PROMPT, "prompt.submit", None),
    "PreToolUse": (Category.TOOL, "tool.pre", "started"),
    "PostToolUse": (Category.TOOL, "tool.post", "ok"),
    "PostToolUseFailure": (Category.TOOL, "tool.failure", "failed"),
    "PermissionRequest": (Category.APPROVAL, "approval.request", "requested"),
    "PermissionDenied": (Category.APPROVAL, "approval.denied", "denied"),
    "Notification": (Category.SESSION, "notification", None),
    "SubagentStart": (Category.TASK, "subagent.start", None),
    "SubagentStop": (Category.TASK, "subagent.stop", None),
    "Stop": (Category.SESSION, "turn.stop", None),
    "StopFailure": (Category.ERROR, "turn.failure", "failed"),
    "PreCompact": (Category.SESSION, "session.compact", None),
}

# The hook events `agentlens install` registers.
HOOK_EVENTS: tuple[str, ...] = tuple(EVENT_MAP)

_EXIT_CODE = re.compile(r"^Exit code (\d+)")


# ------------------------------------------------------------ small helpers


def _str(v: Any) -> str | None:
    return v if isinstance(v, str) and v else None


def _int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return None


def _set(d: dict, key: str, value: Any) -> None:
    if value is not None:
        d[key] = value


def _text_size(v: Any) -> dict[str, int] | None:
    if not isinstance(v, str):
        return None
    return {"bytes": len(v.encode("utf-8", "surrogatepass")), "lines": v.count("\n") + (1 if v and not v.endswith("\n") else 0)}


# ------------------------------------------------------ tool input sanitizing


def _bash(inp: dict) -> dict:
    out: dict[str, Any] = {}
    _set(out, "command", clean(inp.get("command"), 500) if isinstance(inp.get("command"), str) else None)
    _set(out, "description", clean(inp.get("description"), 120) if isinstance(inp.get("description"), str) else None)
    if isinstance(inp.get("run_in_background"), bool):
        out["run_in_background"] = inp["run_in_background"]
    _set(out, "timeout", _int(inp.get("timeout")))
    return out


def _path(inp: dict, key: str = "file_path") -> dict:
    out: dict[str, Any] = {}
    _set(out, key, clean(inp.get(key), 500) if isinstance(inp.get(key), str) else None)
    return out


def _write(inp: dict) -> dict:
    out = _path(inp)
    _set(out, "content_size", _text_size(inp.get("content")))
    return out


def _edit(inp: dict) -> dict:
    out = _path(inp)
    for key in ("old_string", "new_string"):
        if isinstance(inp.get(key), str):
            out[f"{key}_chars"] = len(inp[key])
    if isinstance(inp.get("replace_all"), bool):
        out["replace_all"] = inp["replace_all"]
    if isinstance(inp.get("edits"), list):  # MultiEdit
        out["edits"] = len(inp["edits"])
    return out


def _read(inp: dict) -> dict:
    out = _path(inp)
    _set(out, "offset", _int(inp.get("offset")))
    _set(out, "limit", _int(inp.get("limit")))
    return out


def _notebook(inp: dict) -> dict:
    out = _path(inp, "notebook_path")
    _set(out, "edit_mode", _str(inp.get("edit_mode")))
    if isinstance(inp.get("new_source"), str):
        out["new_source_chars"] = len(inp["new_source"])
    return out


def _search(inp: dict) -> dict:
    out: dict[str, Any] = {}
    for key, limit in (("pattern", 200), ("path", 500), ("glob", 200), ("type", 50), ("output_mode", 50)):
        if isinstance(inp.get(key), str):
            out[key] = clean(inp[key], limit)
    return out


def _webfetch(inp: dict) -> dict:
    out: dict[str, Any] = {}
    _set(out, "url", clean(inp.get("url"), 300) if isinstance(inp.get("url"), str) else None)
    if isinstance(inp.get("prompt"), str):
        out["prompt_chars"] = len(inp["prompt"])
    return out


def _websearch(inp: dict) -> dict:
    out: dict[str, Any] = {}
    _set(out, "query", clean(inp.get("query"), 200) if isinstance(inp.get("query"), str) else None)
    return out


def _agent(inp: dict) -> dict:
    out: dict[str, Any] = {}
    _set(out, "subagent_type", clean(inp.get("subagent_type"), 80) if isinstance(inp.get("subagent_type"), str) else None)
    _set(out, "description", clean(inp.get("description"), 120) if isinstance(inp.get("description"), str) else None)
    if isinstance(inp.get("prompt"), str):
        out["prompt_chars"] = len(inp["prompt"])
    return out


def _generic(inp: dict) -> dict:
    """Unknown/MCP tools: short scalars only, redacted. Long text and nested data -> sizes."""
    out: dict[str, Any] = {}
    for key in list(inp)[:20]:
        if not isinstance(key, str):
            continue
        v = inp[key]
        if isinstance(v, bool) or (isinstance(v, (int, float)) and not isinstance(v, bool)):
            out[key] = v
        elif isinstance(v, str):
            out[key] = clean(v, 200) if len(v) <= 200 else {"chars": len(v)}
        elif isinstance(v, list):
            out[key] = {"items": len(v)}
        elif isinstance(v, dict):
            out[key] = {"keys": len(v)}
    return redact_obj(out)


_SANITIZERS: dict[str, Callable[[dict], dict]] = {
    "Bash": _bash,
    "Write": _write,
    "Edit": _edit,
    "MultiEdit": _edit,
    "Read": _read,
    "NotebookEdit": _notebook,
    "Glob": _search,
    "Grep": _search,
    "WebFetch": _webfetch,
    "WebSearch": _websearch,
    "Agent": _agent,
    "Task": _agent,
}


def sanitize_tool_input(tool_name: str | None, tool_input: Any) -> dict:
    if not isinstance(tool_input, dict):
        return {}
    fn = _SANITIZERS.get(tool_name or "", _generic)
    return fn(tool_input)


def summarize_tool_response(resp: Any) -> dict | None:
    """Top-level shape only: booleans and numbers kept, text reduced to its length."""
    if resp is None:
        return None
    if isinstance(resp, str):
        return {"chars": len(resp)}
    if isinstance(resp, list):
        return {"items": len(resp)}
    if not isinstance(resp, dict):
        return None
    out: dict[str, Any] = {}
    for key in list(resp)[:30]:
        if not isinstance(key, str):
            continue
        v = resp[key]
        if isinstance(v, bool) or isinstance(v, (int, float)):
            out[key] = v
        elif isinstance(v, str):
            out[f"{key}_chars"] = len(v)
        elif isinstance(v, list):
            out[f"{key}_items"] = len(v)
        elif isinstance(v, dict):
            out[f"{key}_keys"] = len(v)
    return redact_obj(out)


# ------------------------------------------------------------------ adapter


def normalize(payload: Any, received_at: float, cfg: dict | None = None) -> list[Event]:
    """Convert one hook payload into normalized events. Raises ValueError if unusable."""
    if not isinstance(payload, dict):
        raise ValueError("hook payload must be a JSON object")
    session_id = _str(payload.get("session_id"))
    hook = _str(payload.get("hook_event_name"))
    if not session_id or not hook:
        raise ValueError("hook payload missing session_id or hook_event_name")
    cfg = cfg if cfg is not None else config_mod.load()

    category, etype, status = EVENT_MAP.get(hook, (Category.SESSION, f"claude_code.{hook}", None))
    tool_name = _str(payload.get("tool_name"))
    tool_use_id = _str(payload.get("tool_use_id"))
    duration_ms = None
    data: dict[str, Any] = {"hook_event_name": hook}

    # common, optional fields
    _set(data, "permission_mode", _str(payload.get("permission_mode")))
    _set(data, "prompt_id", _str(payload.get("prompt_id")))
    _set(data, "agent_id", _str(payload.get("agent_id")))
    _set(data, "agent_type", _str(payload.get("agent_type")))
    _set(data, "transcript_path", _str(payload.get("transcript_path")))
    effort = payload.get("effort")
    if isinstance(effort, dict):
        _set(data, "effort", _str(effort.get("level")))

    if hook == "SessionStart":
        _set(data, "start_source", _str(payload.get("source")))
        _set(data, "model", _str(payload.get("model")))
        if isinstance(payload.get("session_title"), str):
            data["session_title_chars"] = len(payload["session_title"])

    elif hook == "SessionEnd":
        _set(data, "end_reason", _str(payload.get("reason")))

    elif hook == "UserPromptSubmit":
        prompt = payload.get("prompt")
        if isinstance(prompt, str):
            data["prompt_chars"] = len(prompt)
            data["prompt_hmac"] = fingerprint(prompt)
            if cfg.get("capture_prompts"):
                data["prompt_text"] = clean(prompt, 2000)

    elif hook in ("PreToolUse", "PermissionRequest"):
        data["input"] = sanitize_tool_input(tool_name, payload.get("tool_input"))

    elif hook == "PostToolUse":
        duration_ms = _int(payload.get("duration_ms"))
        _set(data, "response", summarize_tool_response(payload.get("tool_response")))

    elif hook == "PostToolUseFailure":
        duration_ms = _int(payload.get("duration_ms"))
        if payload.get("is_interrupt") is True:
            status = "interrupted"
            data["is_interrupt"] = True
        error = payload.get("error")
        if isinstance(error, str):
            m = _EXIT_CODE.match(error)
            if m:
                data["exit_code"] = int(m.group(1))
            data["error"] = clean(error, 1000)

    elif hook == "PermissionDenied":
        _set(data, "reason", clean(payload.get("reason"), 300) if isinstance(payload.get("reason"), str) else None)
        data["input"] = sanitize_tool_input(tool_name, payload.get("tool_input"))

    elif hook == "Notification":
        _set(data, "notification_type", _str(payload.get("notification_type")))
        for key, limit in (("message", 300), ("title", 120)):
            if isinstance(payload.get(key), str):
                data[key] = clean(payload[key], limit)

    elif hook in ("Stop", "SubagentStop"):
        if isinstance(payload.get("stop_hook_active"), bool):
            data["stop_hook_active"] = payload["stop_hook_active"]
        if isinstance(payload.get("last_assistant_message"), str):
            data["last_assistant_message_chars"] = len(payload["last_assistant_message"])
        _set(data, "agent_transcript_path", _str(payload.get("agent_transcript_path")))

    elif hook == "StopFailure":
        _set(data, "error", clean(payload.get("error"), 100) if isinstance(payload.get("error"), str) else None)
        _set(data, "error_details", clean(payload.get("error_details"), 500) if isinstance(payload.get("error_details"), str) else None)

    elif hook == "PreCompact":
        _set(data, "trigger", _str(payload.get("trigger")))
        if isinstance(payload.get("custom_instructions"), str):
            data["custom_instructions_chars"] = len(payload["custom_instructions"])

    mcp = payload.get("mcp_server")
    if isinstance(mcp, dict):
        _set(data, "mcp_server", _str(mcp.get("name")))

    if tool_use_id:
        event_id = tool_event_id(PROVIDER, session_id, hook, tool_use_id)
    else:
        event_id = payload_event_id(PROVIDER, payload, received_at)

    # final safety net: nothing unredacted leaves the adapter
    data = redact_obj(data)
    return [
        Event(
            event_id=event_id,
            provider=PROVIDER,
            session_id=session_id,
            project=_str(payload.get("cwd")),
            category=category,
            type=etype,
            ts=received_at,
            ts_source=TsSource.COLLECTOR,
            source=SOURCE,
            provenance=Provenance.OBSERVED,
            tool_name=tool_name,
            tool_use_id=tool_use_id,
            status=status,
            duration_ms=duration_ms,
            data=data,
        )
    ]

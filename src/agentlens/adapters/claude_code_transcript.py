"""Per-request token usage from Claude Code session transcripts (JSONL).

The transcript format is **not documented as stable**: the docs call it internal
and say it changes between versions. So this reader is defensive:

* Only assistant entries with a ``message.usage`` object are read. Every other
  line (user prompts, tool results, attachments, request blobs) is skipped
  by a substring check before it is even parsed, and nothing from it is kept.
* One API request is written as several entries that repeat the same usage and
  ``requestId``. Records are keyed by ``requestId`` (falling back to
  ``message.id``) so a request is counted once.
* A missing usage field stays ``None`` ("unavailable"), never 0.
* Reading is incremental: callers pass the byte offset saved last time and get
  back the new one. Only complete lines are consumed; a half-written last line
  is left for the next read. If the file shrank or was replaced (different
  inode), reading restarts at 0, which is safe because requests are deduplicated.

What is kept per request: request id, timestamp, model, sidechain flag, the five
token counts and the Claude Code version string. Never message content.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

PROVIDER = "claude-code"
SOURCE = "transcript"
CHUNK = 1 << 20
MAX_LINE = 64 << 20  # a single line larger than this is skipped, not buffered forever


@dataclass
class ReadResult:
    records: list[dict] = field(default_factory=list)
    offset: int = 0
    inode: int | None = None
    size: int = 0
    skipped: int = 0  # usage-looking lines that could not be used
    reset: bool = False
    error: str | None = None


def _int(v: Any) -> int | None:
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    return v if v >= 0 else None


def _ts(v: Any) -> float | None:
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_line(line: bytes, session_id: str, is_sidechain: bool) -> dict | None:
    """One transcript line -> a usage record, or None if it isn't one."""
    if b'"assistant"' not in line or b'"usage"' not in line:
        return None
    try:
        entry = json.loads(line)
    except ValueError:
        raise ValueError("unparseable usage line") from None
    if not isinstance(entry, dict) or entry.get("type") != "assistant":
        return None
    message = entry.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
        return None
    usage = message["usage"]
    request_id = entry.get("requestId") or message.get("id")
    ts = _ts(entry.get("timestamp"))
    if not isinstance(request_id, str) or not request_id or ts is None:
        raise ValueError("usage entry without request id or timestamp")
    model = message.get("model") if isinstance(message.get("model"), str) else None
    if model == "<synthetic>":  # locally generated message, not an API request
        return None
    split = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    return {
        "provider": PROVIDER,
        "session_id": session_id,
        "request_id": request_id,
        "ts": ts,
        "model": model,
        "is_sidechain": int(bool(entry.get("isSidechain")) or is_sidechain),
        "input_tokens": _int(usage.get("input_tokens")),
        "cache_read_tokens": _int(usage.get("cache_read_input_tokens")),
        "cache_write_tokens": _int(usage.get("cache_creation_input_tokens")),
        "cache_write_5m_tokens": _int(split.get("ephemeral_5m_input_tokens")),
        "cache_write_1h_tokens": _int(split.get("ephemeral_1h_input_tokens")),
        "output_tokens": _int(usage.get("output_tokens")),
        "client_version": entry.get("version") if isinstance(entry.get("version"), str) else None,
        "source": SOURCE,
    }


def read_new(path: str, session_id: str, offset: int = 0, inode: int | None = None,
             is_sidechain: bool = False) -> ReadResult:
    """Read usage records appended to ``path`` since ``offset``."""
    out = ReadResult(offset=offset, inode=inode)
    if not path.endswith(".jsonl"):
        out.error = "not a .jsonl transcript"
        return out
    try:
        st = os.stat(path)
    except OSError as exc:
        out.error = f"transcript unreadable: {exc.strerror or exc}"
        return out
    out.inode, out.size = st.st_ino, st.st_size
    if (inode is not None and st.st_ino != inode) or st.st_size < offset:
        out.offset, out.reset = 0, True
    if st.st_size == out.offset:
        return out
    seen: dict[str, dict] = {}
    try:
        with open(path, "rb") as fh:
            fh.seek(out.offset)
            pos = out.offset
            buf = b""
            while True:
                chunk = fh.read(CHUNK)
                if not chunk:
                    break
                buf += chunk
                start = 0
                while True:
                    nl = buf.find(b"\n", start)
                    if nl < 0:
                        break
                    line = buf[start:nl]
                    pos += nl - start + 1
                    start = nl + 1
                    try:
                        rec = parse_line(line, session_id, is_sidechain)
                    except ValueError:
                        out.skipped += 1
                        continue
                    if rec is not None:
                        prev = seen.get(rec["request_id"])
                        if prev is None or (rec["output_tokens"] or 0) > (prev["output_tokens"] or 0):
                            if prev is not None:
                                rec["ts"] = min(rec["ts"], prev["ts"])
                            seen[rec["request_id"]] = rec
                buf = buf[start:]
                if len(buf) > MAX_LINE:  # pathological line: drop it and resync at the next newline
                    pos += len(buf)
                    buf = b""
                    out.skipped += 1
            out.offset = pos  # the unterminated tail (if any) is re-read next time
    except OSError as exc:
        out.error = f"transcript read failed: {exc.strerror or exc}"
        return out
    out.records = list(seen.values())
    return out

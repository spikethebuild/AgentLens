"""Build Claude Code-shaped transcript JSONL for tests.

Mirrors the shape observed in real transcripts (2026-11-12): assistant entries
carry requestId, timestamp, version, isSidechain and message{id, model, usage};
one request is split over several entries with identical usage. Every
non-usage line carries a content sentinel that must never reach the database.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

SENTINEL = "TRANSCRIPT-CONTENT-SENTINEL"


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def request_lines(req_id: str, ts: float, *, read: int | None, write: int | None, inp: int | None = 3,
                  out: int = 200, model: str = "claude-sonnet-5", w5m: int | None = None,
                  w1h: int | None = None, sidechain: bool = False, split: int = 2,
                  session_id: str = "s") -> list[str]:
    usage: dict = {"output_tokens": out}
    if inp is not None:
        usage["input_tokens"] = inp
    if read is not None:
        usage["cache_read_input_tokens"] = read
    if write is not None:
        usage["cache_creation_input_tokens"] = write
        if w5m is None and w1h is None:
            w5m = write
        usage["cache_creation"] = {"ephemeral_5m_input_tokens": w5m or 0, "ephemeral_1h_input_tokens": w1h or 0}
    usage["service_tier"] = "standard"
    lines = [json.dumps({"type": "user", "timestamp": iso(ts - 1), "sessionId": session_id,
                         "message": {"role": "user", "content": f"{SENTINEL} prompt"}}, separators=(",", ":"))]
    for part in range(split):
        lines.append(json.dumps({
            "type": "assistant", "requestId": req_id, "timestamp": iso(ts + part * 0.01),
            "sessionId": session_id, "isSidechain": sidechain, "version": "2.1.322",
            "message": {"id": f"msg_{req_id}", "model": model, "role": "assistant",
                        "content": [{"type": "text", "text": f"{SENTINEL} answer part {part}"}],
                        "usage": usage},
        }, separators=(",", ":")))
    return lines


def write(path: Path, lines: list[str], mode: str = "w") -> Path:
    with open(path, mode) as fh:
        for line in lines:
            fh.write(line + "\n")
    return path

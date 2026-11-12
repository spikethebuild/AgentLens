"""On-disk spool between the hook fast path and ingest.

Each hook invocation writes one small JSON file atomically (tmp + rename) into
a 0700 directory. Ingest drains files in name order. Names start with a
nanosecond timestamp, so name order is arrival order.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Iterable

from agentlens import paths
from agentlens.events import Event

SPOOL_VERSION = 1
FILE_MODE = 0o600
TMP_PREFIX = ".tmp-"


def write_events(events: Iterable[Event]) -> Path:
    directory = paths.ensure_dir(paths.spool_dir())
    body = json.dumps(
        {"spool_version": SPOOL_VERSION, "events": [e.to_dict() for e in events]},
        separators=(",", ":"),
    ).encode("utf-8")
    name = f"{time.time_ns()}-{os.getpid()}-{secrets.token_hex(4)}.json"
    tmp = directory / f"{TMP_PREFIX}{name}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    try:
        os.write(fd, body)
    except BaseException:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    os.close(fd)
    final = directory / name
    os.replace(tmp, final)
    return final


def pending() -> list[Path]:
    directory = paths.spool_dir()
    if not directory.is_dir():
        return []
    return sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.suffix == ".json" and not p.name.startswith(".")
    )


def read_events(path: Path) -> list[Event]:
    """Parse and validate a spool file. Raises ValueError on any problem."""
    try:
        doc = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"unreadable spool file: {exc}") from exc
    if not isinstance(doc, dict) or doc.get("spool_version") != SPOOL_VERSION:
        raise ValueError("unsupported spool file format")
    items = doc.get("events")
    if not isinstance(items, list):
        raise ValueError("spool file has no events list")
    return [Event.from_dict(item) for item in items]


def move_to_failed(path: Path, reason: str) -> Path:
    failed = paths.ensure_dir(paths.failed_dir())
    target = failed / path.name
    os.replace(path, target)
    try:
        reason_fd = os.open(failed / f"{path.name}.reason", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
        try:
            os.write(reason_fd, reason[:2000].encode("utf-8", "replace"))
        finally:
            os.close(reason_fd)
    except OSError:
        pass
    return target


def failed_count() -> int:
    d = paths.failed_dir()
    if not d.is_dir():
        return 0
    return sum(1 for p in d.iterdir() if p.suffix == ".json")


def remove_stale_tmp(max_age_s: float = 3600) -> int:
    """Delete temp files left by hooks that were killed mid-write."""
    directory = paths.spool_dir()
    if not directory.is_dir():
        return 0
    removed = 0
    cutoff = time.time() - max_age_s
    for p in directory.iterdir():
        if p.name.startswith(TMP_PREFIX):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except OSError:
                pass
    return removed

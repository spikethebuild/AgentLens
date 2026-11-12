"""Drain the spool into SQLite.

Runs on every CLI read and (detached) after SessionEnd. A file lock keeps two
ingests from overlapping. Spool files are deleted only after the transaction
that holds their events commits; if ingest dies in between, the files are
re-ingested next time and deduplicated by event_id.
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from agentlens import paths, spool, store
from agentlens.errlog import log_error

try:  # POSIX only; on other platforms ingest runs unlocked (dedup keeps it correct)
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

BATCH_FILES = 200


@dataclass
class IngestResult:
    files: int = 0
    inserted: int = 0
    duplicates: int = 0
    failed: int = 0
    locked: bool = False


@contextmanager
def _lock(wait_s: float) -> Iterator[bool]:
    if fcntl is None:
        yield True
        return
    paths.ensure_dir(paths.home())
    fd = os.open(paths.lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    try:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        yield acquired
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _insert_files(conn: sqlite3.Connection, loaded: list[tuple[Path, list]], result: IngestResult) -> None:
    events = [e for _, evs in loaded for e in evs]
    try:
        inserted = store.insert_events(conn, events)
    except sqlite3.IntegrityError:
        if len(loaded) == 1:
            path, _ = loaded[0]
            spool.move_to_failed(path, "database rejected events (integrity error)")
            result.failed += 1
            return
        for item in loaded:  # isolate the poison file
            _insert_files(conn, [item], result)
        return
    result.inserted += inserted
    result.duplicates += len(events) - inserted
    for path, _ in loaded:
        path.unlink(missing_ok=True)
        result.files += 1


def ingest(wait_s: float = 0.0, batch_files: int = BATCH_FILES) -> IngestResult:
    result = IngestResult()
    with _lock(wait_s) as acquired:
        if not acquired:
            result.locked = True
            return result
        spool.remove_stale_tmp()
        files = spool.pending()
        if not files:
            return result
        conn = store.connect()
        try:
            for start in range(0, len(files), batch_files):
                loaded = []
                for path in files[start : start + batch_files]:
                    try:
                        loaded.append((path, spool.read_events(path)))
                    except FileNotFoundError:
                        continue
                    except ValueError as exc:
                        spool.move_to_failed(path, str(exc))
                        log_error(f"ingest: moved {path.name} to failed: {exc}")
                        result.failed += 1
                if loaded:
                    _insert_files(conn, loaded, result)
        finally:
            conn.close()
    return result

"""SQLite event store.

* WAL journal so CLI reads never block ingest.
* Events are inserted with ON CONFLICT DO NOTHING in one transaction per batch, so
  re-ingesting the same spool file is harmless.
* The ``sessions`` table is an order-independent merge (MIN/MAX/latest-wins),
  so events can arrive in any order.
* Tool calls are paired at query time by the ``tool_calls`` view; there is no
  derived state that can drift out of sync with the events.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from agentlens import paths
from agentlens.events import SCHEMA_VERSION, Event

DB_SCHEMA_VERSION = 1

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id       TEXT PRIMARY KEY,
    provider       TEXT NOT NULL,
    session_id     TEXT NOT NULL,
    project        TEXT,
    category       TEXT NOT NULL,
    type           TEXT NOT NULL,
    ts             REAL NOT NULL,
    ts_source      TEXT NOT NULL,
    source         TEXT NOT NULL,
    provenance     TEXT NOT NULL CHECK (provenance IN ('observed','calculated','estimated')),
    tool_name      TEXT,
    tool_use_id    TEXT,
    status         TEXT,
    duration_ms    INTEGER,
    data           TEXT NOT NULL DEFAULT '{}',
    schema_version INTEGER NOT NULL,
    ingested_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS events_session_ts ON events(provider, session_id, ts);
CREATE INDEX IF NOT EXISTS events_tool ON events(provider, session_id, tool_use_id)
    WHERE tool_use_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS sessions (
    provider        TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    project         TEXT,
    started_at      REAL,
    start_source    TEXT,
    model           TEXT,
    permission_mode TEXT,
    first_seen      REAL NOT NULL,
    last_seen       REAL NOT NULL,
    ended_at        REAL,
    end_reason      TEXT,
    PRIMARY KEY (provider, session_id)
);

CREATE VIEW IF NOT EXISTS tool_calls AS
WITH ids AS (
    SELECT DISTINCT provider, session_id, tool_use_id
    FROM events WHERE category = 'tool' AND tool_use_id IS NOT NULL
),
pre AS (
    SELECT provider, session_id, tool_use_id, tool_name, ts, data
    FROM events WHERE type = 'tool.pre'
),
fin AS (
    SELECT provider, session_id, tool_use_id, tool_name, ts, status, duration_ms, data
    FROM events WHERE type IN ('tool.post', 'tool.failure')
),
den AS (
    SELECT provider, session_id, tool_use_id, MAX(ts) AS ts
    FROM events WHERE type = 'approval.denied' AND tool_use_id IS NOT NULL
    GROUP BY provider, session_id, tool_use_id
)
SELECT
    ids.provider,
    ids.session_id,
    ids.tool_use_id,
    COALESCE(pre.tool_name, fin.tool_name)               AS tool_name,
    pre.ts                                               AS started_at,
    fin.ts                                               AS ended_at,
    CASE
        WHEN fin.status IS NOT NULL THEN fin.status
        WHEN den.ts IS NOT NULL THEN 'denied'
        ELSE 'no_result'
    END                                                  AS status,
    CASE
        WHEN fin.duration_ms IS NOT NULL THEN fin.duration_ms
        WHEN pre.ts IS NOT NULL AND fin.ts IS NOT NULL AND fin.ts >= pre.ts
            THEN CAST(ROUND((fin.ts - pre.ts) * 1000) AS INTEGER)
    END                                                  AS duration_ms,
    CASE
        WHEN fin.duration_ms IS NOT NULL THEN 'observed'
        WHEN pre.ts IS NOT NULL AND fin.ts IS NOT NULL AND fin.ts >= pre.ts THEN 'calculated'
    END                                                  AS duration_provenance,
    pre.data                                             AS pre_data,
    fin.data                                             AS result_data
FROM ids
LEFT JOIN pre USING (provider, session_id, tool_use_id)
LEFT JOIN fin USING (provider, session_id, tool_use_id)
LEFT JOIN den USING (provider, session_id, tool_use_id);
"""

# ON CONFLICT(event_id) DO NOTHING rather than INSERT OR IGNORE: OR IGNORE would
# also silently drop rows that violate CHECK / NOT NULL constraints.
_INSERT_EVENT = """
INSERT INTO events (
    event_id, provider, session_id, project, category, type, ts, ts_source, source,
    provenance, tool_name, tool_use_id, status, duration_ms, data, schema_version, ingested_at
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT (event_id) DO NOTHING
"""

# Every SET expression reads the *old* row (``sessions.*``), so the merge is
# order-independent: earliest start wins, latest end wins, latest model wins.
_UPSERT_SESSION = """
INSERT INTO sessions (
    provider, session_id, project, started_at, start_source, model, permission_mode,
    first_seen, last_seen, ended_at, end_reason
) VALUES (:provider, :session_id, :project, :started_at, :start_source, :model,
          :permission_mode, :first_seen, :last_seen, :ended_at, :end_reason)
ON CONFLICT (provider, session_id) DO UPDATE SET
    project = COALESCE(sessions.project, excluded.project),
    started_at = CASE
        WHEN excluded.started_at IS NOT NULL
             AND (sessions.started_at IS NULL OR excluded.started_at < sessions.started_at)
        THEN excluded.started_at ELSE sessions.started_at END,
    start_source = CASE
        WHEN excluded.started_at IS NOT NULL
             AND (sessions.started_at IS NULL OR excluded.started_at < sessions.started_at)
        THEN excluded.start_source ELSE sessions.start_source END,
    model = CASE
        WHEN excluded.model IS NOT NULL AND excluded.last_seen >= sessions.last_seen
        THEN excluded.model ELSE COALESCE(sessions.model, excluded.model) END,
    permission_mode = CASE
        WHEN excluded.permission_mode IS NOT NULL AND excluded.last_seen >= sessions.last_seen
        THEN excluded.permission_mode
        ELSE COALESCE(sessions.permission_mode, excluded.permission_mode) END,
    first_seen = MIN(sessions.first_seen, excluded.first_seen),
    last_seen = MAX(sessions.last_seen, excluded.last_seen),
    ended_at = CASE
        WHEN excluded.ended_at IS NOT NULL
             AND (sessions.ended_at IS NULL OR excluded.ended_at >= sessions.ended_at)
        THEN excluded.ended_at ELSE sessions.ended_at END,
    end_reason = CASE
        WHEN excluded.ended_at IS NOT NULL
             AND (sessions.ended_at IS NULL OR excluded.ended_at >= sessions.ended_at)
        THEN excluded.end_reason ELSE sessions.end_reason END
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or paths.db_path()
    paths.ensure_dir(path.parent)
    if not path.exists():
        # create owner-only before SQLite touches it
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    migrate(conn)
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0]) if row else 0


def migrate(conn: sqlite3.Connection) -> None:
    current = schema_version(conn)
    if current > DB_SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema v{current} is newer than this AgentLens (v{DB_SCHEMA_VERSION})"
        )
    if current < 1:
        conn.executescript(_SCHEMA_V1)
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(DB_SCHEMA_VERSION),),
            )


# ------------------------------------------------------------------- writes


def _session_rows(events: Iterable[Event]) -> list[dict]:
    """Pre-merge a batch per session so each session is upserted once."""
    merged: dict[tuple[str, str], dict] = {}
    for ev in events:
        key = (ev.provider, ev.session_id)
        row = merged.get(key)
        if row is None:
            row = merged[key] = {
                "provider": ev.provider,
                "session_id": ev.session_id,
                "project": ev.project,
                "started_at": None,
                "start_source": None,
                "model": None,
                "permission_mode": None,
                "first_seen": ev.ts,
                "last_seen": ev.ts,
                "ended_at": None,
                "end_reason": None,
                "_attr_ts": float("-inf"),
            }
        row["first_seen"] = min(row["first_seen"], ev.ts)
        row["last_seen"] = max(row["last_seen"], ev.ts)
        row["project"] = row["project"] or ev.project
        if ev.type == "session.start" and (row["started_at"] is None or ev.ts < row["started_at"]):
            row["started_at"] = ev.ts
            row["start_source"] = ev.data.get("start_source")
        if ev.type == "session.end" and (row["ended_at"] is None or ev.ts >= row["ended_at"]):
            row["ended_at"] = ev.ts
            row["end_reason"] = ev.data.get("end_reason")
        if ev.ts >= row["_attr_ts"]:
            if ev.data.get("model"):
                row["model"] = ev.data["model"]
            if ev.data.get("permission_mode"):
                row["permission_mode"] = ev.data["permission_mode"]
            row["_attr_ts"] = ev.ts
    for row in merged.values():
        row.pop("_attr_ts")
    return list(merged.values())


def insert_events(conn: sqlite3.Connection, events: list[Event]) -> int:
    """Insert a batch atomically. Returns the number of *new* events."""
    if not events:
        return 0
    now = time.time()
    rows = [
        (
            ev.event_id, ev.provider, ev.session_id, ev.project, ev.category.value, ev.type,
            ev.ts, ev.ts_source.value, ev.source, ev.provenance.value, ev.tool_name,
            ev.tool_use_id, ev.status, ev.duration_ms,
            json.dumps(ev.data, sort_keys=True, separators=(",", ":")),
            ev.schema_version or SCHEMA_VERSION, now,
        )
        for ev in events
    ]
    with conn:
        before = conn.total_changes
        conn.executemany(_INSERT_EVENT, rows)
        inserted = conn.total_changes - before
        conn.executemany(_UPSERT_SESSION, _session_rows(events))
    return inserted


# -------------------------------------------------------------------- reads


class AmbiguousSession(LookupError):
    pass


@dataclass
class Totals:
    sessions: int
    events: int
    tool_calls: int
    last_event_ts: float | None


def totals(conn: sqlite3.Connection) -> Totals:
    row = conn.execute(
        """SELECT (SELECT COUNT(*) FROM sessions),
                  (SELECT COUNT(*) FROM events),
                  (SELECT COUNT(*) FROM tool_calls),
                  (SELECT MAX(ts) FROM events)"""
    ).fetchone()
    return Totals(row[0], row[1], row[2], row[3])


_SESSION_SUMMARY = """
SELECT s.*,
    (SELECT COUNT(*) FROM events e
        WHERE e.provider = s.provider AND e.session_id = s.session_id)        AS event_count,
    (SELECT COUNT(*) FROM tool_calls t
        WHERE t.provider = s.provider AND t.session_id = s.session_id)        AS tool_calls,
    (SELECT COUNT(*) FROM tool_calls t
        WHERE t.provider = s.provider AND t.session_id = s.session_id
          AND t.status IN ('failed', 'interrupted'))                          AS tool_failures,
    (SELECT COUNT(*) FROM events e
        WHERE e.provider = s.provider AND e.session_id = s.session_id
          AND e.type = 'prompt.submit')                                       AS prompts
FROM sessions s
"""


def list_sessions(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        _SESSION_SUMMARY + " ORDER BY s.last_seen DESC LIMIT ?", (limit,)
    ).fetchall()


def find_session(conn: sqlite3.Connection, prefix: str) -> sqlite3.Row | None:
    """Resolve a full or unique-prefix session ID. Raises AmbiguousSession."""
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = conn.execute(
        _SESSION_SUMMARY + " WHERE s.session_id = ? OR s.session_id LIKE ? ESCAPE '\\'"
        " ORDER BY s.last_seen DESC LIMIT 10",
        (prefix, escaped + "%"),
    ).fetchall()
    exact = [r for r in rows if r["session_id"] == prefix]
    if exact:
        return exact[0]
    if len(rows) > 1:
        raise AmbiguousSession([r["session_id"] for r in rows])
    return rows[0] if rows else None


def session_events(conn: sqlite3.Connection, provider: str, session_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM events WHERE provider = ? AND session_id = ? ORDER BY ts, rowid",
        (provider, session_id),
    ).fetchall()


def session_tool_calls(conn: sqlite3.Connection, provider: str, session_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM tool_calls WHERE provider = ? AND session_id = ?"
        " ORDER BY COALESCE(started_at, ended_at)",
        (provider, session_id),
    ).fetchall()


def max_event_rowid(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM events").fetchone()[0]


def session_events_after(
    conn: sqlite3.Connection, provider: str, session_id: str, after_rowid: int
) -> list[sqlite3.Row]:
    """Events of one session inserted after ``after_rowid`` (for live tailing).

    rowid follows insertion order, so this returns only what a later ingest added.
    Rows carry ``_rowid`` so the caller can advance its cursor.
    """
    return conn.execute(
        "SELECT rowid AS _rowid, * FROM events WHERE provider = ? AND session_id = ? AND rowid > ?"
        " ORDER BY rowid",
        (provider, session_id, after_rowid),
    ).fetchall()


def events_after(conn: sqlite3.Connection, after_rowid: int, limit: int = 200) -> list[sqlite3.Row]:
    """Events from all sessions inserted after ``after_rowid``, oldest first (live feed)."""
    return conn.execute(
        "SELECT rowid AS _rowid, * FROM events WHERE rowid > ? ORDER BY rowid LIMIT ?",
        (after_rowid, limit),
    ).fetchall()


def recent_events(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    """The latest ``limit`` events from all sessions, oldest first."""
    rows = conn.execute(
        "SELECT rowid AS _rowid, * FROM events ORDER BY rowid DESC LIMIT ?", (limit,)
    ).fetchall()
    return rows[::-1]


@dataclass
class ToolTimeStats:
    timed: int          # calls with a duration
    avg_ms: float | None
    observed: int       # of those, durations reported by the provider


def tool_time_stats(conn: sqlite3.Connection, project: str | None = None) -> ToolTimeStats:
    """Average tool duration over calls that have one; ``project=""`` means no project."""
    row = conn.execute(
        """SELECT COUNT(t.duration_ms), AVG(t.duration_ms),
                  COALESCE(SUM(t.duration_provenance = 'observed'), 0)
           FROM tool_calls t JOIN sessions s USING (provider, session_id)
           WHERE ? IS NULL OR COALESCE(s.project, '') = ?""",
        (project, project),
    ).fetchone()
    return ToolTimeStats(row[0], row[1], row[2])

from __future__ import annotations

import os
import sqlite3
import stat
import threading

import pytest

from agentlens import paths, store
from agentlens.events import Category, Event, Provenance, TsSource, tool_event_id

_n = 0


def ev(type_, ts, session="s1", category=None, tool_use_id=None, **kw):
    global _n
    _n += 1
    prefix = type_.split(".")[0]
    cat = category or (Category(prefix) if prefix in {c.value for c in Category} else Category.SESSION)
    kw.setdefault("provenance", Provenance.OBSERVED)
    eid = (
        tool_event_id("claude-code", session, type_, tool_use_id)
        if tool_use_id
        else f"{_n:064x}"
    )
    return Event(
        event_id=eid,
        provider="claude-code",
        session_id=session,
        project="/proj",
        category=cat,
        type=type_,
        ts=ts,
        ts_source=TsSource.COLLECTOR,
        source="hook",
        tool_use_id=tool_use_id,
        **kw,
    )


@pytest.fixture
def conn():
    c = store.connect()
    yield c
    c.close()


def test_wal_and_schema(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store.schema_version(conn) == store.DB_SCHEMA_VERSION
    mode = stat.S_IMODE(os.stat(paths.db_path()).st_mode)
    assert mode == 0o600


def test_migrate_is_idempotent(conn):
    store.migrate(conn)
    store.connect().close()
    assert store.schema_version(conn) == store.DB_SCHEMA_VERSION == 2


def test_v1_database_migrates_to_v2(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(str(path))
    old.executescript(store._SCHEMA_V1)
    old.execute("INSERT INTO meta(key, value) VALUES ('schema_version', '1')")
    old.commit()
    old.close()
    conn = store.connect(path)
    assert store.schema_version(conn) == 2
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"events", "sessions", "api_requests", "transcript_cursors"} <= tables
    conn.close()


def test_refuses_newer_schema(conn):
    with conn:
        conn.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    with pytest.raises(RuntimeError):
        store.connect()


def test_insert_or_ignore_dedupes(conn):
    batch = [ev("session.start", 1.0, data={"start_source": "startup"}), ev("turn.stop", 2.0)]
    assert store.insert_events(conn, batch) == 2
    assert store.insert_events(conn, batch) == 0
    assert store.totals(conn).events == 2


def test_batch_is_atomic(conn):
    good = ev("turn.stop", 1.0)
    bad = ev("turn.stop", 2.0)
    # Force a constraint failure inside the batch by smuggling an invalid provenance.
    object.__setattr__(bad, "provenance", type("P", (), {"value": "guessed"})())
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_events(conn, [good, bad])
    assert store.totals(conn).events == 0
    assert store.totals(conn).sessions == 0


def test_session_upsert_merges_out_of_order(conn):
    end = ev("session.end", 50.0, data={"end_reason": "prompt_input_exit"})
    mid = ev("turn.stop", 20.0, data={"permission_mode": "default"})
    start = ev("session.start", 10.0, data={"start_source": "startup", "model": "claude-x"})
    store.insert_events(conn, [end])
    store.insert_events(conn, [mid])
    store.insert_events(conn, [start])
    s = store.find_session(conn, "s1")
    assert s["started_at"] == 10.0 and s["start_source"] == "startup"
    assert s["ended_at"] == 50.0 and s["end_reason"] == "prompt_input_exit"
    assert s["first_seen"] == 10.0 and s["last_seen"] == 50.0
    assert s["model"] == "claude-x"
    assert s["permission_mode"] == "default"


def test_earliest_start_and_latest_end_win(conn):
    store.insert_events(conn, [ev("session.start", 30.0, data={"start_source": "resume"})])
    store.insert_events(conn, [ev("session.start", 10.0, data={"start_source": "startup"})])
    store.insert_events(conn, [ev("session.end", 60.0, data={"end_reason": "other"})])
    store.insert_events(conn, [ev("session.end", 40.0, data={"end_reason": "clear"})])
    s = store.find_session(conn, "s1")
    assert (s["started_at"], s["start_source"]) == (10.0, "startup")
    assert (s["ended_at"], s["end_reason"]) == (60.0, "other")


def test_session_without_start_has_no_started_at(conn):
    store.insert_events(conn, [ev("turn.stop", 5.0)])
    s = store.find_session(conn, "s1")
    assert s["started_at"] is None and s["first_seen"] == 5.0


def _tool(type_, ts, tid, status=None, duration_ms=None, session="s1"):
    return ev(type_, ts, session=session, category=Category.TOOL, tool_use_id=tid,
              tool_name="Bash", status=status, duration_ms=duration_ms)


def test_tool_calls_view_pairs_and_labels_provenance(conn):
    store.insert_events(conn, [
        _tool("tool.post", 12.0, "t1", status="ok", duration_ms=1500),  # Post before Pre
        _tool("tool.pre", 10.0, "t1", status="started"),
        _tool("tool.pre", 20.0, "t2", status="started"),
        _tool("tool.failure", 20.25, "t2", status="failed"),
        _tool("tool.pre", 30.0, "t3", status="started"),
        _tool("tool.pre", 40.0, "t4", status="started"),
        _tool("tool.post", 39.9, "t4", status="ok"),  # async skew: Post stamped earlier
    ])
    calls = {r["tool_use_id"]: r for r in store.session_tool_calls(conn, "claude-code", "s1")}
    assert calls["t1"]["duration_ms"] == 1500 and calls["t1"]["duration_provenance"] == "observed"
    assert calls["t2"]["duration_ms"] == 250 and calls["t2"]["duration_provenance"] == "calculated"
    assert calls["t2"]["status"] == "failed"
    assert calls["t3"]["status"] == "no_result" and calls["t3"]["duration_ms"] is None
    assert calls["t3"]["duration_provenance"] is None
    # negative calculated duration is reported unavailable, never as a wrong number
    assert calls["t4"]["duration_ms"] is None and calls["t4"]["duration_provenance"] is None


def test_denied_tool_status(conn):
    store.insert_events(conn, [
        _tool("tool.pre", 1.0, "t9", status="started"),
        ev("approval.denied", 1.5, category=Category.APPROVAL, tool_use_id="t9", status="denied"),
    ])
    (call,) = store.session_tool_calls(conn, "claude-code", "s1")
    assert call["status"] == "denied"


def test_list_sessions_counts(conn):
    store.insert_events(conn, [
        ev("session.start", 1.0, session="aaa111"),
        ev("prompt.submit", 2.0, session="aaa111", category=Category.PROMPT),
        _tool("tool.pre", 3.0, "x1", session="aaa111"),
        _tool("tool.failure", 4.0, "x1", status="failed", session="aaa111"),
        ev("session.start", 9.0, session="bbb222"),
    ])
    rows = store.list_sessions(conn)
    assert [r["session_id"] for r in rows] == ["bbb222", "aaa111"]
    a = rows[1]
    assert (a["event_count"], a["tool_calls"], a["tool_failures"], a["prompts"]) == (4, 1, 1, 1)


def test_find_session_prefix(conn):
    store.insert_events(conn, [ev("turn.stop", 1.0, session="abc-1"), ev("turn.stop", 2.0, session="abd-2")])
    assert store.find_session(conn, "abc")["session_id"] == "abc-1"
    assert store.find_session(conn, "zzz") is None
    with pytest.raises(store.AmbiguousSession):
        store.find_session(conn, "ab")
    # LIKE wildcards in user input are literal
    assert store.find_session(conn, "a_c") is None
    assert store.find_session(conn, "%") is None


def test_concurrent_reader_during_write(conn):
    store.insert_events(conn, [ev("turn.stop", 1.0)])
    errors = []

    def reader():
        try:
            c = store.connect()
            for _ in range(50):
                store.totals(c)
            c.close()
        except Exception as e:  # pragma: no cover
            errors.append(e)

    t = threading.Thread(target=reader)
    t.start()
    for i in range(50):
        store.insert_events(conn, [ev("turn.stop", 10.0 + i)])
    t.join()
    assert not errors

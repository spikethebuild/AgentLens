from __future__ import annotations

import os
import shutil
import sqlite3
import time

import pytest

from agentlens import ingest, paths, spool, store
from agentlens.adapters import claude_code

TS = 1_763_000_000.0


def spool_fixture(fixture, name, ts=TS):
    return spool.write_events(claude_code.normalize(fixture(name), ts, {"capture_prompts": False}))


def db_counts():
    conn = store.connect()
    try:
        t = store.totals(conn)
        return t.events, t.sessions
    finally:
        conn.close()


def test_drains_spool_into_db(fixture):
    for i, name in enumerate(["session_start", "pre_tool_use_bash", "post_tool_use_bash", "session_end"]):
        spool_fixture(fixture, name, TS + i)
    r = ingest.ingest()
    assert (r.files, r.inserted, r.duplicates, r.failed, r.locked) == (4, 4, 0, 0, False)
    assert spool.pending() == []
    assert db_counts() == (4, 1)


def test_empty_spool_is_noop():
    r = ingest.ingest()
    assert r.files == 0 and not paths.db_path().exists()


def test_bad_files_go_to_failed_and_good_ones_still_ingest(fixture):
    spool_fixture(fixture, "stop")
    d = paths.spool_dir()
    (d / "0000000000000000001-1-aaaa.json").write_text("{not json")
    (d / "0000000000000000002-1-bbbb.json").write_text('{"spool_version": 99, "events": []}')
    (d / "0000000000000000003-1-cccc.json").write_text('{"spool_version": 1, "events": [{"bogus": true}]}')
    r = ingest.ingest()
    assert (r.files, r.failed) == (1, 3)
    failed = sorted(p.name for p in paths.failed_dir().iterdir())
    assert "0000000000000000001-1-aaaa.json" in failed
    assert "0000000000000000001-1-aaaa.json.reason" in failed
    assert spool.failed_count() == 3
    assert spool.pending() == []
    assert db_counts() == (1, 1)


def test_crash_before_unlink_reingests_safely(fixture, tmp_path):
    f = spool_fixture(fixture, "pre_tool_use_bash")
    backup = tmp_path / "copy.json"
    shutil.copy(f, backup)
    ingest.ingest()
    shutil.copy(backup, f)  # as if ingest died after commit but before unlink
    r = ingest.ingest()
    assert (r.files, r.inserted, r.duplicates) == (1, 0, 1)
    assert db_counts() == (1, 1)


def test_duplicate_hook_firings_dedupe(fixture):
    # same hook installed at user and project level fires twice
    spool_fixture(fixture, "stop", TS)
    spool_fixture(fixture, "stop", TS + 0.4)
    spool_fixture(fixture, "post_tool_use_bash", TS)
    spool_fixture(fixture, "post_tool_use_bash", TS + 3)
    r = ingest.ingest()
    assert (r.inserted, r.duplicates) == (2, 2)


@pytest.mark.skipif(ingest.fcntl is None, reason="needs fcntl")
def test_held_lock_skips(fixture):
    spool_fixture(fixture, "stop")
    paths.ensure_dir(paths.home())
    fd = os.open(paths.lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    ingest.fcntl.flock(fd, ingest.fcntl.LOCK_EX)
    try:
        t = time.monotonic()
        r = ingest.ingest(wait_s=0.2)
        assert r.locked and r.files == 0
        assert time.monotonic() - t >= 0.2
        assert len(spool.pending()) == 1
    finally:
        ingest.fcntl.flock(fd, ingest.fcntl.LOCK_UN)
        os.close(fd)
    assert ingest.ingest().files == 1


def test_batches(fixture):
    for i in range(7):
        payload = fixture("stop")
        payload["session_id"] = f"s{i}"
        spool.write_events(claude_code.normalize(payload, TS + i, {}))
    r = ingest.ingest(batch_files=3)
    assert (r.files, r.inserted) == (7, 7)
    assert db_counts() == (7, 7)


def test_poison_file_is_isolated(fixture, monkeypatch):
    good = spool_fixture(fixture, "stop")
    poison = spool_fixture(fixture, "session_end", TS + 1)
    real = store.insert_events

    def picky(conn, events):
        if any(e.type == "session.end" for e in events):
            raise sqlite3.IntegrityError("simulated")
        return real(conn, events)

    monkeypatch.setattr(store, "insert_events", picky)
    r = ingest.ingest()
    assert (r.files, r.inserted, r.failed) == (1, 1, 1)
    assert not good.exists() and not poison.exists()
    assert (paths.failed_dir() / poison.name).exists()


def test_operational_error_keeps_files(fixture, monkeypatch):
    f = spool_fixture(fixture, "stop")

    def locked(conn, events):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "insert_events", locked)
    with pytest.raises(sqlite3.OperationalError):
        ingest.ingest()
    assert f.exists() and spool.failed_count() == 0


def test_stale_tmp_cleaned(fixture):
    spool_fixture(fixture, "stop")
    stale = paths.spool_dir() / ".tmp-123-1-abcd.json"
    fresh = paths.spool_dir() / ".tmp-456-1-efgh.json"
    stale.write_text("{")
    fresh.write_text("{")
    old = time.time() - 7200
    os.utime(stale, (old, old))
    ingest.ingest()
    assert not stale.exists() and fresh.exists()

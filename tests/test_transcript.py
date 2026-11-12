"""Incremental transcript usage reading (Claude Code JSONL)."""

from __future__ import annotations

import os

from agentlens import store
from agentlens.adapters import claude_code_transcript as cct
from agentlens.ingest import ingest, sync_transcripts
from transcripts import SENTINEL, request_lines, write

T0 = 1_790_000_000.0


def test_split_entries_count_once_and_keep_no_content(tmp_path):
    path = write(tmp_path / "t.jsonl", request_lines("req_a", T0, read=1000, write=500, split=3))
    r = cct.read_new(str(path), "s")
    (rec,) = r.records
    assert (rec["cache_read_tokens"], rec["cache_write_tokens"], rec["input_tokens"]) == (1000, 500, 3)
    assert rec["cache_write_5m_tokens"] == 500 and rec["cache_write_1h_tokens"] == 0
    assert rec["ts"] == T0 and rec["model"] == "claude-sonnet-5" and rec["client_version"] == "2.1.322"
    assert SENTINEL not in repr(rec)
    assert r.offset == path.stat().st_size


def test_incremental_offsets_never_double_count(tmp_path):
    path = write(tmp_path / "t.jsonl", request_lines("req_a", T0, read=0, write=5000))
    first = cct.read_new(str(path), "s")
    write(path, request_lines("req_b", T0 + 10, read=5000, write=100), mode="a")
    second = cct.read_new(str(path), "s", first.offset, first.inode)
    assert [r["request_id"] for r in second.records] == ["req_b"]
    third = cct.read_new(str(path), "s", second.offset, second.inode)
    assert third.records == [] and third.offset == second.offset


def test_partial_last_line_is_left_for_next_read(tmp_path):
    lines = request_lines("req_a", T0, read=0, write=5000, split=1)
    path = tmp_path / "t.jsonl"
    full = "\n".join(lines) + "\n"
    path.write_text(full[:-20])  # last line cut mid-way, no newline yet
    r1 = cct.read_new(str(path), "s")
    assert r1.records == [] and r1.skipped == 0
    path.write_text(full)
    r2 = cct.read_new(str(path), "s", r1.offset, r1.inode)
    assert [r["request_id"] for r in r2.records] == ["req_a"]


def test_truncated_or_replaced_file_restarts_safely(tmp_path):
    path = write(tmp_path / "t.jsonl", request_lines("req_a", T0, read=0, write=5000)
                 + request_lines("req_b", T0 + 5, read=5000, write=10))
    r1 = cct.read_new(str(path), "s")
    write(path, request_lines("req_c", T0 + 9, read=0, write=7000))  # rewritten, smaller
    r2 = cct.read_new(str(path), "s", r1.offset, r1.inode)
    assert r2.reset and [r["request_id"] for r in r2.records] == ["req_c"]


def test_missing_usage_fields_stay_unavailable(tmp_path):
    path = write(tmp_path / "t.jsonl", request_lines("req_a", T0, read=None, write=None, inp=None))
    (rec,) = cct.read_new(str(path), "s").records
    assert rec["cache_read_tokens"] is None and rec["cache_write_tokens"] is None and rec["input_tokens"] is None


def test_garbage_synthetic_and_non_jsonl_paths(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text('{"type":"assistant","usage" broken json\n'
                    '{"type":"assistant","requestId":"x","timestamp":"2026-11-12T10:00:00Z",'
                    '"message":{"model":"<synthetic>","usage":{"input_tokens":0}}}\n')
    r = cct.read_new(str(path), "s")
    assert r.records == [] and r.skipped == 1
    assert cct.read_new(str(tmp_path / "x.txt"), "s").error == "not a .jsonl transcript"
    assert "unreadable" in cct.read_new(str(tmp_path / "missing.jsonl"), "s").error


def test_sidechain_flag(tmp_path):
    path = write(tmp_path / "t.jsonl", request_lines("req_a", T0, read=0, write=5000, sidechain=True))
    (rec,) = cct.read_new(str(path), "s").records
    assert rec["is_sidechain"] == 1


# ----------------------------------------------------- ingest integration


def test_ingest_registers_transcripts_and_reads_usage(tmp_path, fixture):
    from agentlens import spool
    from agentlens.adapters.claude_code import normalize

    path = write(tmp_path / "sess.jsonl", request_lines("req_a", T0, read=0, write=5000))
    payload = fixture("session_start")
    payload["transcript_path"] = str(path)
    spool.write_events(normalize(payload, T0))
    r = ingest()
    assert r.requests == 1
    conn = store.connect()
    (req,) = store.session_requests(conn, "claude-code", payload["session_id"])
    assert req["cache_write_tokens"] == 5000

    # appended later: a plain CLI read picks it up without any new hook event
    write(path, request_lines("req_b", T0 + 30, read=5000, write=50), mode="a")
    assert ingest().requests == 1
    assert len(store.session_requests(conn, "claude-code", payload["session_id"])) == 2
    # unchanged file is not even opened again
    assert sync_transcripts(conn) == 0
    conn.close()
    db_bytes = b"".join(p.read_bytes() for p in store.paths.db_path().parent.glob("agentlens.db*"))
    assert SENTINEL.encode() not in db_bytes


def test_unreadable_transcript_records_reason(tmp_path, fixture):
    from agentlens import spool
    from agentlens.adapters.claude_code import normalize

    payload = fixture("session_start")
    payload["transcript_path"] = str(tmp_path / "gone.jsonl")
    spool.write_events(normalize(payload, T0))
    ingest()
    conn = store.connect()
    (cur,) = store.session_transcripts(conn, "claude-code", payload["session_id"])
    assert "unreadable" in cur["error"]
    conn.close()
    assert not os.path.exists(tmp_path / "gone.jsonl")

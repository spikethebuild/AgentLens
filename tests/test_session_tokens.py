"""Per-session token totals in `sessions`, `inspect` and the dashboard data."""

from __future__ import annotations

import copy

from agentlens import cli, render, spool, store
from agentlens.adapters.claude_code import normalize
from agentlens.ingest import ingest
from conftest import load_fixture
from transcripts import SENTINEL, request_lines, write

SID = "3f2a9c1e-5b7d-4e8a-9c0f-1a2b3c4d5e6f"
T0 = 1_790_000_000.0


def record(tmp_path, lines: list[str] | None):
    path = tmp_path / "sess.jsonl"
    if lines is not None:
        write(path, lines)
    payload = copy.deepcopy(load_fixture("session_start"))
    payload.update(session_id=SID, transcript_path=str(path))
    spool.write_events(normalize(payload, T0 - 5, {"capture_prompts": False}))
    ingest()


def test_session_usage_totals(tmp_path):
    record(tmp_path,
           request_lines("r1", T0, read=0, write=50_000, inp=10, out=300)
           + request_lines("r2", T0 + 10, read=50_000, write=1_000, inp=5, out=200)
           + request_lines("r3", T0 + 20, read=2_000, write=500, inp=1, out=50, sidechain=True))
    conn = store.connect()
    u = store.session_usage(conn, "claude-code", SID)
    assert (u["main_requests"], u["sub_requests"], u["requests"]) == (2, 1, 3)
    assert u["cache_read"] == 52_000 and u["cache_write"] == 51_500 and u["uncached"] == 16
    assert u["input"] == 52_000 + 51_500 + 16 and u["output"] == 550
    assert u["hit_ratio"] == 50_000 / (50_000 + 51_000 + 15)  # main conversation only
    row = store.list_sessions(conn)[0]
    assert (row["api_requests"], row["tokens_in"], row["tokens_out"]) == (3, u["input"], 550)
    conn.close()


def test_inspect_and_sessions_show_tokens_with_provenance(tmp_path, capsys):
    record(tmp_path, request_lines("r1", T0, read=0, write=50_000, out=300)
           + request_lines("r2", T0 + 10, read=50_000, write=1_000, out=200))
    assert cli.main(["inspect", SID[:8]]) == 0
    out = capsys.readouterr().out
    assert "API requests        2 main conversation, 0 subagent" in out
    assert "input tokens" in out and "cache read 50k" in out and "[obs]" in out
    assert "output tokens       500" in out
    assert "cache hit ratio" in out and "[calc]" in out
    assert SENTINEL not in out
    assert cli.main(["sessions"]) == 0
    out = capsys.readouterr().out
    assert "TOKENS IN/OUT [obs]" in out and "101k / 500" in out


def test_tokens_unavailable_with_reason(tmp_path, capsys):
    record(tmp_path, None)  # transcript path recorded but the file never existed
    assert cli.main(["inspect", SID[:8]]) == 0
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if ln.strip().startswith("tokens"))
    assert "unavailable (" in line
    assert cli.main(["sessions"]) == 0
    assert "unavailable" in capsys.readouterr().out.splitlines()[2]


def test_token_rows_never_invent_numbers():
    assert render.token_rows(None) == [("tokens", "unavailable (no per-request usage recorded for this session)", "")]
    rows = dict((k, (v, t)) for k, v, t in render.token_rows({
        "requests": 1, "main_requests": 1, "sub_requests": 0, "incomplete": 1, "input": 0,
        "cache_read": 0, "cache_write": 0, "uncached": 0, "output": 7, "hit_ratio": None}))
    assert "1 missing usage fields" in rows["API requests"][0]
    assert rows["cache hit ratio"] == ("unavailable (no complete main-conversation requests)", "")

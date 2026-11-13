"""Token use per prompt (turns.py)."""

from __future__ import annotations

import copy

from conftest import load_fixture

from agentlens import config, paths, spool, store, turns
from agentlens.adapters.claude_code import normalize
from agentlens.ingest import ingest
from transcripts import request_lines, write

SID = "bbbbcccc-0000-4000-8000-000000000002"
T0 = 1_790_000_000.0


def hook(name, ts, **overrides):
    p = copy.deepcopy(load_fixture(name))
    p["session_id"] = SID
    p.update(overrides)
    return normalize(p, ts)


def seed(tmp_path, capture=False):
    if capture:
        paths.ensure_dir(paths.home())
        paths.config_path().write_text('{"capture_prompts": true}')
    lines = (request_lines("r0", T0 + 2, read=0, write=20_000, session_id=SID)          # before any prompt
             + request_lines("r1", T0 + 11, read=20_000, write=1_000, session_id=SID)
             + request_lines("r2", T0 + 15, read=21_000, write=60_000, session_id=SID)   # prompt 1 is big
             + request_lines("s1", T0 + 16, read=0, write=5_000, sidechain=True, session_id=SID)
             + request_lines("r3", T0 + 31, read=81_000, write=500, session_id=SID))
    path = write(tmp_path / "t.jsonl", lines)
    spool.write_events(
        hook("session_start", T0, transcript_path=str(path))
        + hook("user_prompt_submit", T0 + 10, prompt="refactor the whole parser sk-ant-api03-SECRETSECRETSECRET")
        + hook("pre_tool_use_bash", T0 + 12) + hook("post_tool_use_failure_bash", T0 + 13)
        + hook("user_prompt_submit", T0 + 30, prompt="thanks")
    )
    ingest()
    conn = store.connect()
    try:
        return turns.session_turns(conn, "claude-code", SID)
    finally:
        conn.close()


def test_requests_are_attributed_to_the_prompt_before_them(tmp_path):
    ts = seed(tmp_path)
    assert [t.index for t in ts] == [0, 1, 2]
    before, big, small = ts
    assert [r["request_id"] for r in before.requests] == ["r0"]
    assert [r["request_id"] for r in big.requests] == ["r1", "r2"]
    assert [r["request_id"] for r in big.sub_requests] == ["s1"]
    assert big.tokens_in == (20_003 + 1_000) + (21_000 + 60_003) + 5_003
    assert big.peak_context == 81_003 and big.cache_write == 66_000
    assert big.tools == {"Bash": 1} and big.failures == 1
    assert small.tokens_in == 81_503 and small.end is None


def test_prompt_text_only_with_capture(tmp_path):
    big = seed(tmp_path)[1]
    assert big.prompt_text is None
    assert big.prompt_label() == f"{len('refactor the whole parser sk-ant-api03-SECRETSECRETSECRET')} chars (text not stored)"


def test_captured_prompt_text_is_redacted(tmp_path):
    assert config.load() is not None
    big = seed(tmp_path, capture=True)[1]
    assert big.prompt_text and "refactor the whole parser" in big.prompt_text
    assert "SECRETSECRET" not in big.prompt_text


def test_session_without_prompts_or_requests_has_no_turns(tmp_path):
    spool.write_events(hook("session_start", T0))
    ingest()
    conn = store.connect()
    try:
        assert turns.session_turns(conn, "claude-code", SID) == []
    finally:
        conn.close()

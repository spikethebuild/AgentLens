"""Cache coach rules: one scenario per rule, plus the no-data paths."""

from __future__ import annotations

import copy

from agentlens import cache, cli, spool, store
from agentlens.adapters.claude_code import normalize
from conftest import load_fixture
from transcripts import request_lines, write

SID = "3f2a9c1e-5b7d-4e8a-9c0f-1a2b3c4d5e6f"
T0 = 1_790_000_000.0


def hook(name: str, ts: float, **overrides) -> list:
    payload = copy.deepcopy(load_fixture(name))
    payload["session_id"] = SID
    payload.update(overrides)
    return normalize(payload, ts, {"capture_prompts": False})


def run(tmp_path, events: list, requests: list[str] | None, with_transcript: bool = True):
    path = tmp_path / "sess.jsonl"
    if requests is not None:
        write(path, requests)
    start = hook("session_start", T0 - 5, **({"transcript_path": str(path)} if with_transcript else {"transcript_path": None}))
    spool.write_events(start + [e for evs in events for e in evs])
    conn = store.connect()
    from agentlens.ingest import ingest

    ingest()
    rep = cache.analyze(conn, store.find_session(conn, SID))
    conn.close()
    return rep


def base_requests(model="claude-sonnet-5"):
    return (request_lines("req_1", T0, read=0, write=50_000, model=model)
            + request_lines("req_2", T0 + 10, read=50_000, write=1_000, model=model))


def rules(rep):
    return [f.rule for f in rep.findings]


# ----------------------------------------------------------------- rules


def test_model_switch_from_hook_with_claude_code_estimate(tmp_path):
    reqs = base_requests() + request_lines("req_3", T0 + 30, read=0, write=52_000, model="claude-opus-5")
    rep = run(tmp_path, [hook("post_model_switch", T0 + 20)], reqs)
    (m,) = rep.misses
    assert m.cause == "model_switch" and m.shortfall == 51_000
    (f,) = rep.findings
    assert f.rule == "model_switch" and f.severity == "warn" and f.impact_tokens == 51_000
    assert "re-processed 51k tokens [calc]" in f.impact
    assert "Claude Code estimate $1.14 [obs]" in f.impact
    assert any("PostModelSwitch" in e for e in f.evidence) and any("req_2" in e for e in f.evidence)
    assert "Pick the model" in f.fix


def test_model_change_seen_only_in_transcript(tmp_path):
    reqs = base_requests() + request_lines("req_3", T0 + 30, read=0, write=52_000, model="claude-opus-5")
    rep = run(tmp_path, [], reqs)
    (f,) = rep.findings
    assert f.rule == "model_switch" and "seen in transcript" in f.title


def test_compaction_is_info_and_attributed(tmp_path):
    reqs = base_requests() + request_lines("req_3", T0 + 30, read=2_000, write=8_000)
    rep = run(tmp_path, [hook("pre_compact", T0 + 15), hook("post_compact", T0 + 20)], reqs)
    (m,) = rep.misses
    assert m.cause == "compaction"
    (f,) = rep.findings
    assert f.rule == "compaction" and f.severity == "info"
    assert "/clear" in f.fix


def test_repeated_compactions_warn(tmp_path):
    events = [hook("post_compact", T0 + 100 * i, compact_summary=f"s{i}") for i in range(1, 4)]
    rep = run(tmp_path, events, base_requests())
    assert any(f.rule == "compaction" and f.severity == "warn" and "3 compactions" in f.title for f in rep.findings)


def test_idle_beyond_5m_ttl(tmp_path):
    reqs = base_requests() + request_lines("req_3", T0 + 10 + 400, read=0, write=51_500)
    rep = run(tmp_path, [], reqs)
    (f,) = rep.findings
    assert f.rule == "idle" and "> 5m cache TTL (5m cache writes) [calc]" in f.title


def test_same_gap_with_1h_ttl_is_not_idle(tmp_path):
    reqs = (request_lines("req_1", T0, read=0, write=50_000, w1h=50_000, w5m=0)
            + request_lines("req_2", T0 + 400, read=0, write=50_500, w1h=50_500, w5m=0))
    rep = run(tmp_path, [], reqs)
    (m,) = rep.misses
    assert m.cause == "unexplained" and m.ttl_s == 3600


def test_resume_after_cache_expired_uses_claude_code_fields(tmp_path):
    rep = run(tmp_path, [hook("session_start_resume", T0 + 50)], base_requests())
    (f,) = [f for f in rep.findings if f.title.startswith("Resumed")]
    assert f.rule == "idle" and "re-sends 182k tokens [obs]" in f.impact and "$1.14" in f.impact


def test_effort_change_on_affected_model(tmp_path):
    reqs = base_requests() + request_lines("req_3", T0 + 30, read=0, write=51_500)
    events = [hook("user_prompt_submit", T0 + 2, effort={"level": "low"}),
              hook("user_prompt_submit", T0 + 20, effort={"level": "high"}, prompt="second")]
    rep = run(tmp_path, events, reqs)
    (f,) = rep.findings
    assert f.rule == "effort_change"


def test_effort_change_on_exempt_model_is_not_blamed(tmp_path):
    model = "claude-opus-5-5"
    reqs = base_requests(model) + request_lines("req_3", T0 + 30, read=0, write=51_500, model=model)
    events = [hook("user_prompt_submit", T0 + 2, effort={"level": "low"}),
              hook("user_prompt_submit", T0 + 20, effort={"level": "high"}, prompt="second")]
    rep = run(tmp_path, events, reqs)
    (f,) = rep.findings
    assert f.rule == "unexplained"


def test_unexplained_rebuild_cites_config_change_and_never_claims_tools(tmp_path):
    reqs = base_requests() + request_lines("req_3", T0 + 30, read=0, write=51_500)
    rep = run(tmp_path, [hook("config_change", T0 + 20)], reqs)
    (f,) = rep.findings
    assert f.rule == "unexplained"
    assert any("ConfigChange project_settings" in e for e in f.evidence)
    assert "cause unknown" in f.impact


def test_small_shortfall_is_not_a_miss(tmp_path):
    reqs = base_requests() + request_lines("req_3", T0 + 30, read=49_500, write=600)
    rep = run(tmp_path, [], reqs)
    assert rep.misses == [] and rep.findings == []


def test_low_hit_ratio_attributes_causes(tmp_path):
    reqs = []
    for i in range(10):
        reqs += request_lines(f"req_{i:02d}", T0 + 10 * i, read=1_000, write=5_000)
    rep = run(tmp_path, [], reqs)
    low = [f for f in rep.findings if f.rule == "low_hit"]
    assert len(low) == 1 and "unexplained" in low[0].impact
    assert rep.hit_ratio < 0.7


def test_large_instruction_file(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text("\n".join(f"- rule {i}: do the thing carefully" for i in range(250)) + "\n")
    rep = run(tmp_path, [hook("instructions_loaded", T0 - 4, file_path=str(md))], base_requests())
    (f,) = [f for f in rep.findings if f.rule == "instructions"]
    assert "(250 lines)" in f.title and "[est]" in f.impact and "written to cache 1× this session [calc]" in f.impact


def test_small_instruction_file_is_fine(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text("# short\n")
    rep = run(tmp_path, [hook("instructions_loaded", T0 - 4, file_path=str(md))], base_requests())
    assert "instructions" not in rules(rep)


# ---------------------------------------------------------- no-data paths


def test_no_transcript_path_still_reports_hook_findings(tmp_path):
    rep = run(tmp_path, [hook("post_model_switch", T0 + 20, transcript_path=None)], None, with_transcript=False)
    assert rep.usage_status == "no transcript path recorded for this session"
    (f,) = rep.findings
    assert f.rule == "model_switch" and "unavailable (no usage data)" in f.impact and "$1.14" in f.impact
    text = cache.render_report(rep)
    assert "unavailable (no transcript path recorded for this session)" in text
    assert "unavailable" in cache.render_brief(rep)


def test_missing_transcript_file(tmp_path):
    rep = run(tmp_path, [], None)  # path registered, file never written
    assert "unreadable" in rep.usage_status and rep.findings == []


def test_transcript_without_usage_entries(tmp_path):
    path = tmp_path / "sess.jsonl"
    path.write_text('{"type":"user","message":{"content":"hi"}}\n')
    rep = run(tmp_path, [], None)
    assert "no usage entries" in rep.usage_status


# ------------------------------------------------------------- outputs


def test_brief_is_at_most_five_short_lines(tmp_path):
    reqs = (base_requests()
            + request_lines("req_3", T0 + 30, read=0, write=52_000, model="claude-opus-5")
            + request_lines("req_4", T0 + 500, read=0, write=53_000, model="claude-opus-5")
            + request_lines("req_5", T0 + 520, read=0, write=54_000, model="claude-opus-5"))
    rep = run(tmp_path, [hook("post_model_switch", T0 + 20), hook("config_change", T0 + 510)], reqs)
    brief = cache.render_brief(rep)
    lines = brief.splitlines()
    assert 2 <= len(lines) <= 5 and all(len(line) <= 140 for line in lines)
    assert lines[0].startswith("AgentLens cache · 3f2a9c1e") and lines[-1].startswith("fix:")


def test_report_has_provenance_and_evidence(tmp_path):
    reqs = base_requests() + request_lines("req_3", T0 + 30, read=0, write=52_000, model="claude-opus-5")
    rep = run(tmp_path, [hook("post_model_switch", T0 + 20)], reqs)
    text = cache.render_report(rep, show_requests=True)
    for needle in ("cache hit ratio", "[calc]", "[obs]", "evidence", "fix", "undocumented format", "MISS"):
        assert needle in text


def test_cli_cache_defaults_to_latest_session_in_cwd(tmp_path, capsys, monkeypatch):
    run(tmp_path, [], base_requests())
    monkeypatch.chdir(tmp_path)  # not the session's project: falls back to the latest overall
    assert cli.main(["cache", "--brief"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("AgentLens cache · 3f2a9c1e") and len(out) <= 5


def test_cli_cache_with_no_sessions(capsys):
    assert cli.main(["cache", "--brief"]) == 0
    assert "no sessions recorded yet" in capsys.readouterr().out
    assert cli.main(["cache", "nope"]) == 1

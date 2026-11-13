"""Cost coach rules and the plugin inventory (all against temp dirs)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from agentlens import cli, coach, inventory, paths, spool, store
from agentlens.adapters.claude_code import normalize
from conftest import load_fixture
from transcripts import request_lines, write

SID = "c0ac4e11-0000-4000-8000-000000000001"
T0 = 1_790_000_000.0
PROJECT = "/work/app"


# ------------------------------------------------------------- fixtures


def make_plugin(root: Path, name: str, skills: int, desc_len: int = 400, hidden: int = 0,
                hooks: int = 0) -> Path:
    for i in range(skills):
        d = root / "skills" / f"s{i}"
        d.mkdir(parents=True)
        extra = "disable-model-invocation: true\n" if i < hidden else ""
        d.joinpath("SKILL.md").write_text(f"---\nname: s{i}\ndescription: {'x' * desc_len}\n{extra}---\nbody\n")
    (root / "agents").mkdir(parents=True, exist_ok=True)
    (root / "agents" / "a0.md").write_text("---\nname: a0\ndescription: an agent\n---\n")
    if hooks:
        (root / "hooks").mkdir(exist_ok=True)
        (root / "hooks" / "hooks.json").write_text(json.dumps(
            {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x"}] * hooks}]}}))
    return root


def install(tmp_path: Path, plugins: dict[str, Path], enabled: dict[str, bool]) -> None:
    settings = paths.claude_settings_path()
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"enabledPlugins": enabled}))
    reg = settings.parent / "plugins" / "installed_plugins.json"
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(json.dumps({"version": 2, "plugins": {
        k: [{"scope": "user", "installPath": str(p)}] for k, p in plugins.items()}}))


def hook(name: str, ts: float, sid: str = SID, **overrides) -> list:
    payload = copy.deepcopy(load_fixture(name))
    payload.update(session_id=sid, cwd=PROJECT, **overrides)
    return normalize(payload, ts, {"capture_prompts": False})


def seed(tmp_path: Path, contexts: list[int], sid: str = SID, extra_events: list | None = None,
         start: float = T0) -> None:
    """One session whose main requests have the given total context sizes."""
    lines = []
    for i, ctx in enumerate(contexts):
        lines += request_lines(f"req_{sid[:4]}_{i}", start + i * 10, read=ctx - 1000 if i else 0,
                               write=1000 if i else ctx - 3, session_id=sid)
    path = write(tmp_path / f"{sid}.jsonl", lines)
    spool.write_events(hook("session_start", start - 5, sid, transcript_path=str(path))
                       + [e for evs in (extra_events or []) for e in evs])
    from agentlens.ingest import ingest

    ingest()


def analyze(now: float = T0 + 3600, **kw) -> coach.CoachReport:
    conn = store.connect()
    try:
        return coach.analyze(conn, PROJECT, now=now, **kw)
    finally:
        conn.close()


def rules(rep) -> list[str]:
    return [f.rule for f in rep.findings]


# ------------------------------------------------------------- inventory


def test_inventory_estimates_listing_and_respects_precedence(tmp_path):
    big = make_plugin(tmp_path / "big", "big", skills=20, hidden=5, hooks=3)
    off = make_plugin(tmp_path / "off", "off", skills=2)
    install(tmp_path, {"big@m": big, "off@m": off}, {"big@m": True, "off@m": True})
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "settings.local.json").write_text(json.dumps({"enabledPlugins": {"off@m": False}}))
    inv = inventory.inventory(proj)
    assert [p.key for p in inv] == ["big@m"]
    p = inv[0]
    assert len(p.skills) == 15 and p.agents == {"a0"} and p.hook_handlers == 3
    assert 15 * 400 // 4 <= p.listing_tokens_est < 20 * 420 // 4
    assert inventory.find_skill(inv, "s7") == "/big:s7"
    assert inventory.find_skill(inv, "s0") is None  # hidden from the model


def test_inventory_survives_missing_or_broken_registry(tmp_path):
    settings = paths.claude_settings_path()
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"enabledPlugins": {"ghost@m": True}}))
    (settings.parent / "plugins").mkdir(exist_ok=True)
    (settings.parent / "plugins" / "installed_plugins.json").write_text("{not json")
    [p] = inventory.inventory()
    assert p.install_path is None and p.listing_tokens_est is None


# ------------------------------------------------------------- rules


def test_long_context_rule_cites_requests(tmp_path):
    seed(tmp_path, [80_000] + [150_000] * 5 + [250_000] * 12)
    rep = analyze()
    [f] = [f for f in rep.findings if f.rule == "long_context"]
    assert "12 of 18 requests above 200k" in f.title
    assert f.impact_tokens == 12 * 249_000
    assert any("first above: request req_c0ac_6" in e for e in f.evidence)
    assert "/compact" in f.fix and "Installed plugin skill" not in f.fix


def test_long_context_names_an_installed_compaction_skill(tmp_path):
    ecc = make_plugin(tmp_path / "ecc", "ecc", skills=0)
    d = ecc / "skills" / "strategic-compact"
    d.mkdir(parents=True)
    d.joinpath("SKILL.md").write_text("---\nname: strategic-compact\ndescription: compact at good points\n---\n")
    install(tmp_path, {"ecc@ecc": ecc}, {"ecc@ecc": True})
    seed(tmp_path, [250_000] * 12)
    [f] = [f for f in analyze().findings if f.rule == "long_context"]
    assert f.fix.endswith("Installed plugin skill: /ecc:strategic-compact.")


def test_short_sessions_get_no_long_context_finding(tmp_path):
    seed(tmp_path, [30_000] * 20)
    assert "long_context" not in rules(analyze())


def test_start_context_rule(tmp_path):
    for i, sid in enumerate(["a" * 8 + SID[8:], "b" * 8 + SID[8:], "c" * 8 + SID[8:]]):
        seed(tmp_path, [70_000 + i * 1000, 71_000], sid=sid, start=T0 + i * 100)
    [f] = [f for f in analyze().findings if f.rule == "start_context"]
    assert "71k tokens" in f.title and "3 sessions" in f.title
    assert len(f.evidence) == 3 and "/context" in f.fix


def test_unused_heavy_plugin_flagged_used_one_not(tmp_path):
    heavy = make_plugin(tmp_path / "heavy", "heavy", skills=30, hooks=4)
    used = make_plugin(tmp_path / "used", "used", skills=30)
    install(tmp_path, {"heavy@m": heavy, "used@m": used}, {"heavy@m": True, "used@m": True})
    skill_call = hook("pre_tool_use_bash", T0 + 15, tool_name="Skill", tool_use_id="toolu_skill",
                      tool_input={"skill": "used:s3"})
    for i, sid in enumerate(["a" * 8 + SID[8:], "b" * 8 + SID[8:], "c" * 8 + SID[8:]]):
        seed(tmp_path, [20_000, 26_000], sid=sid, start=T0 + i * 100,
             extra_events=[skill_call] if i == 0 else None)
    rep = analyze()
    titles = [f.title for f in rep.findings if f.rule == "plugin_overhead"]
    assert len(titles) == 1 and titles[0].startswith("Plugin heavy:")
    f = next(f for f in rep.findings if f.rule == "plugin_overhead")
    assert "4 hook handlers still run" in f.impact and "[est]" in f.impact
    assert rep.skill_uses["used:s3"].count == 1


def test_mcp_tool_calls_count_as_plugin_use(tmp_path):
    mem = make_plugin(tmp_path / "mem", "mem", skills=30)
    install(tmp_path, {"mem@m": mem}, {"mem@m": True})
    call = hook("pre_tool_use_bash", T0 + 15, tool_name="mcp__plugin_mem_search__find",
                tool_use_id="toolu_mcp", tool_input={"q": "x"})
    for i, sid in enumerate(["a" * 8 + SID[8:], "b" * 8 + SID[8:], "c" * 8 + SID[8:]]):
        seed(tmp_path, [20_000, 21_000], sid=sid, start=T0 + i * 100, extra_events=[call] if i == 2 else None)
    assert "plugin_overhead" not in rules(analyze())


def test_too_few_sessions_never_call_a_plugin_unused(tmp_path):
    heavy = make_plugin(tmp_path / "heavy", "heavy", skills=30)
    install(tmp_path, {"heavy@m": heavy}, {"heavy@m": True})
    seed(tmp_path, [20_000, 21_000])
    assert "plugin_overhead" not in rules(analyze())


def test_large_bash_output(tmp_path):
    evs = []
    for i, chars in enumerate([50_000, 30_000, 25_000, 1_000]):
        tid = f"toolu_big{i}"
        evs.append(hook("pre_tool_use_bash", T0 + 20 + i, tool_use_id=tid,
                        tool_input={"command": f"cat file{i}.log"}))
        evs.append(hook("post_tool_use_bash", T0 + 21 + i, tool_use_id=tid,
                        tool_response={"stdout": "y" * chars, "stderr": "", "interrupted": False}))
    seed(tmp_path, [20_000, 21_000], extra_events=evs)
    [f] = [f for f in analyze().findings if f.rule == "large_output"]
    assert f.severity == "warn" and "3 Bash calls" in f.title
    assert f.impact_tokens == 105_000 // 4
    assert f.evidence[0] == "50k chars: cat file0.log"


def test_cache_warnings_are_folded_in(tmp_path):
    # 2 h idle gap with a 5-minute cache: the cache report's idle rule fires
    lines = (request_lines("req_1", T0, read=0, write=60_000, session_id=SID)
             + request_lines("req_2", T0 + 10, read=60_000, write=500, session_id=SID)
             + request_lines("req_3", T0 + 7200, read=0, write=61_000, session_id=SID))
    path = write(tmp_path / "s.jsonl", lines)
    spool.write_events(hook("session_start", T0 - 5, transcript_path=str(path)))
    from agentlens.ingest import ingest

    ingest()
    rep = analyze(now=T0 + 8000)
    f = next(f for f in rep.findings if f.rule == "cache:idle")
    assert f.evidence[0] == f"session {SID[:8]}"


def test_scope_by_project_and_days(tmp_path):
    seed(tmp_path, [250_000] * 12)
    assert analyze(now=T0 + 20 * 86400).sessions == []           # older than 14 days
    assert len(analyze(now=T0 + 20 * 86400, days=30).sessions) == 1
    conn = store.connect()
    try:
        assert coach.analyze(conn, "/elsewhere", now=T0 + 10).sessions == []
        assert len(coach.analyze(conn, None, now=T0 + 10).sessions) == 1
    finally:
        conn.close()


# ------------------------------------------------------------- output


def test_brief_is_at_most_five_lines(tmp_path):
    seed(tmp_path, [250_000] * 12)
    out = coach.render_brief(analyze())
    assert 2 <= len(out.splitlines()) <= 5 and out.startswith("AgentLens coach · 1 sessions")


def test_cli_coach(tmp_path, capsys, monkeypatch):
    seed(tmp_path, [250_000] * 12)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["coach", "--project", PROJECT, "--days", "100000"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Cost coach · last 100000 days") and "above 200k" in out and "[calc]" in out
    assert cli.main(["coach", "--all-projects", "--days", "100000", "--brief"]) == 0
    assert len(capsys.readouterr().out.strip().splitlines()) <= 5


def test_coach_skill_is_user_invoked_and_short():
    root = Path(__file__).resolve().parents[1]
    text = (root / "skills" / "coach" / "SKILL.md").read_text()
    front = text.split("---")[1]
    assert "name: coach" in front and "disable-model-invocation: true" in front
    assert '!`sh "${CLAUDE_PLUGIN_ROOT}/hooks/run.sh" coach --brief`' in text
    assert len(text) < 600


def test_many_long_sessions_are_folded(tmp_path):
    for i in range(6):
        sid = f"{i:08d}" + SID[8:]
        seed(tmp_path, [250_000 + i * 1000] * 12, sid=sid, start=T0 + i * 200)
    longs = [f for f in analyze().findings if f.rule == "long_context"]
    assert len(longs) == coach.LONG_CONTEXT_SHOWN + 1
    assert longs[-1].title.startswith("3 more sessions") or any(f.title.startswith("3 more sessions") for f in longs)
    shown = [f for f in longs if f.title.startswith("Session ")]
    assert [f.evidence[0] for f in shown] == ["session 00000005", "session 00000004", "session 00000003"]


# ------------------------------------------------------------ expensive prompts


def _prompt_session(tmp_path, prompt="fix it", capture=False, contexts=(300_000,) * 25, bash_out=0):
    """One session: a tiny first prompt, then a prompt whose turn runs `contexts` requests."""
    lines = request_lines(f"req_{SID[:4]}_a", T0 + 2, read=0, write=10_000, session_id=SID)
    for i, ctx in enumerate(contexts):
        lines += request_lines(f"req_{SID[:4]}_{i}", T0 + 20 + i, read=ctx - 1000, write=1000, session_id=SID)
    path = write(tmp_path / f"{SID}.jsonl", lines)
    big = copy.deepcopy(load_fixture("user_prompt_submit"))
    big.update(session_id=SID, cwd=PROJECT, prompt=prompt)
    extra = hook("user_prompt_submit", T0 + 1, prompt="hello") + normalize(big, T0 + 10, {"capture_prompts": capture})
    for i in range(9):
        extra += hook("pre_tool_use_read", T0 + 11 + i * 0.1, tool_use_id=f"toolu_r{i}")
    if bash_out:
        post = hook("post_tool_use_bash", T0 + 15)
        post[0].data["response"] = {"stdout_chars": bash_out, "stderr_chars": 0}
        extra += post
    spool.write_events(hook("session_start", T0 - 5, transcript_path=str(path)) + extra)
    from agentlens.ingest import ingest

    ingest()


def test_heavy_prompt_names_the_prompt_and_gives_tips(tmp_path):
    _prompt_session(tmp_path, bash_out=50_000)
    rep = analyze()
    (f,) = [f for f in rep.findings if f.rule == "heavy_prompt"]
    assert "used 7.50M tokens" in f.title and "[calc]" in f.title
    assert f.evidence[0] == "prompt text not stored (6 chars; opt in with capture_prompts)"
    assert "prompt.submit" in f.evidence[1] and f.evidence[2].startswith("largest request req_")
    assert "A 6-character prompt led to 25 requests" in f.fix
    assert "9 file reads/searches" in f.fix
    assert "Tool output was 50k characters" in f.fix
    assert "new session" not in f.fix and "/clear" not in f.fix


def test_heavy_prompt_quotes_captured_text_redacted(tmp_path):
    _prompt_session(tmp_path, prompt="rewrite the parser sk-ant-api03-SECRETSECRETSECRET please", capture=True)
    (f,) = [f for f in analyze().findings if f.rule == "heavy_prompt"]
    assert f.evidence[0].startswith('prompt: "rewrite the parser') and "SECRETSECRET" not in f.evidence[0]


def test_small_prompts_are_not_flagged(tmp_path):
    _prompt_session(tmp_path, contexts=(50_000,) * 5)
    assert "heavy_prompt" not in rules(analyze())

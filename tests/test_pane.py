"""`agentlens pane-data`: the JSON the Claude Code pane draws, and the plugin wiring."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import time
from pathlib import Path

from agentlens import cli, pane, paths, plugin, spool, statusline, store
from agentlens.adapters.claude_code import normalize
from agentlens.ingest import ingest
from conftest import load_fixture
from transcripts import SENTINEL, request_lines, write

ROOT = Path(__file__).resolve().parent.parent
SID = "3f2a9c1e-5b7d-4e8a-9c0f-1a2b3c4d5e6f"
T0 = 1_790_000_000.0


def record(tmp_path, with_usage=True):
    path = tmp_path / "t.jsonl"
    if with_usage:
        write(path, request_lines("r1", T0, read=0, write=40_000, out=900)
              + request_lines("r2", T0 + 10, read=40_000, write=500, out=400))
    evs = []
    for i, name in enumerate(["session_start", "pre_tool_use_bash_fail", "post_tool_use_failure_bash", "stop"]):
        p = copy.deepcopy(load_fixture(name))
        p["session_id"] = SID
        if name == "session_start":
            p["transcript_path"] = str(path)
        evs += normalize(p, T0 - 5 + i)
    spool.write_events(evs)
    ingest()


def data(capsys, *args) -> dict:
    assert cli.main(["pane-data", *args]) == 0
    return json.loads(capsys.readouterr().out)


def test_snapshot_for_a_session(tmp_path, capsys):
    record(tmp_path)
    d = data(capsys, "--session", SID)
    s = d["session"]
    assert d["schema"] == 1 and s["id"] == SID
    assert s["tokens"]["input"] == 80_500 + 6 and s["tokens"]["output"] == 1300
    assert s["tokens"]["provenance"] == "obs" and s["tokens"]["hit_ratio_provenance"] == "calc"
    assert s["tool_failures"] == 1 and d["failures"][0]["source"] == "Bash"
    assert d["events"] and all({"ts", "type", "detail"} <= set(e) for e in d["events"])
    assert d["plan"] is None
    assert SENTINEL not in json.dumps(d)


def test_unknown_session_and_no_usage(tmp_path, capsys):
    d = data(capsys, "--session", "nope")
    assert d["session"] is None and "no record of this session" in d["reason"]
    assert data(capsys)["reason"] == "no sessions recorded yet"
    record(tmp_path, with_usage=False)
    s = data(capsys, "--session", SID)["session"]
    assert s["tokens"] is None and s["tokens_reason"]


def test_plan_snapshot_is_included_until_it_resets(capsys):
    now = time.time()
    paths.ensure_dir(paths.home())
    statusline.snapshot_path().write_text(json.dumps(statusline.parse(
        {"session_id": "x", "rate_limits": {"five_hour": {"used_percentage": 23, "resets_at": now + 60},
                                            "seven_day": {"used_percentage": 62, "resets_at": now - 1}}}, now)))
    plan = data(capsys)["plan"]
    assert [w["name"] for w in plan["windows"]] == ["five_hour"]
    assert plan["windows"][0]["provenance"] == "obs" and plan["as_of_provenance"] == "clk"


def test_errors_become_json(monkeypatch, capsys):
    monkeypatch.setattr(pane, "snapshot", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    d = data(capsys)
    assert d == {"schema": 1, "error": "RuntimeError: boom"}


def test_launcher_runs_pane_data(isolated_env):
    r = subprocess.run(["/bin/sh", str(ROOT / "hooks" / "run.sh"), "pane-data"], capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0 and json.loads(r.stdout)["schema"] == 1


def test_plugin_declares_the_pane_module():
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())
    assert hooks["modules"] == ["./agentlens-pane.tsx"] == [plugin.PANE_MODULE]
    assert (ROOT / "hooks" / "agentlens-pane.tsx").is_file()
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["types"] == "./types/index.d.ts" and (ROOT / "types" / "index.d.ts").is_file()
    module = (ROOT / "hooks" / "agentlens-pane.tsx").read_text()
    # read-only and token-free: no model calls, no tool-call hooks
    for forbidden in ("$.model", "'tool.call'", "$.fs.write", "$.http"):
        assert forbidden not in module

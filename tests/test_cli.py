"""End to end: real hook subprocesses -> spool -> ingest -> CLI output."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from agentlens import cli, installer, paths
from agentlens.render import fmt_duration_ms

PY = sys.executable
SID = "3f2a9c1e-5b7d-4e8a-9c0f-1a2b3c4d5e6f"

SEQUENCE = [
    "session_start", "user_prompt_submit",
    "pre_tool_use_bash", "post_tool_use_bash",
    "pre_tool_use_bash_fail", "post_tool_use_failure_bash",
    "pre_tool_use_write", "post_tool_use_write",
    "pre_tool_use_read", "post_tool_use_read",
    "pre_tool_use_edit",
    "permission_request", "permission_denied",
    "subagent_start", "subagent_stop", "notification",
    "stop_failure", "stop", "session_end",
]


def agentlens(env, *args, stdin=None):
    return subprocess.run(
        [PY, "-m", "agentlens", *args], input=stdin, env=env,
        capture_output=True, text=True, timeout=60,
    )


@pytest.fixture
def recorded(subprocess_env, fixture):
    for name in SEQUENCE:
        r = agentlens(subprocess_env, "hook", "claude-code", stdin=json.dumps(fixture(name)))
        assert r.returncode == 0
    return subprocess_env


def test_sessions_lists_recorded_session(recorded):
    r = agentlens(recorded, "sessions")
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert SID[:8] in out
    assert "ended (prompt_input_exit)" in out
    for tag in ("[clk]", "[calc]"):
        assert tag in out
    assert "[obs] reported by Claude Code" in out  # legend


def test_inspect_shows_provenance_and_unavailable(recorded):
    r = agentlens(recorded, "inspect", SID[:6])
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert f"Session {SID}" in out
    assert "claude-sonnet-5-5" in out and "[obs]" in out
    assert "tokens" in out and "unavailable (not in hook payloads" in out
    assert "cost" in out and "unavailable (planned for v0.3)" in out
    # observed provider duration vs failure without duration
    assert "1.84 s [obs]" in out
    assert "failed (exit 2)" in out
    assert "no result" in out  # Edit has a Pre but no Post
    assert "1 requested, 1 denied" in out
    assert "rate_limit" in out
    assert "API_KEY=[REDACTED:value] pytest" in out


def test_inspect_never_prints_private_content(recorded):
    out = agentlens(recorded, "inspect", SID, "--all").stdout
    for forbidden in ("hunter2-fixture", "fixture-not-a-secret-123", "fixture-file-content-secret",
                      "SECRET_FILE_BODY", "All 3 checkout tests", "3 passed in"):
        assert forbidden not in out
    assert "chars (text not stored)" in out


def test_every_metric_line_has_provenance_or_is_unavailable(recorded):
    out = agentlens(recorded, "inspect", SID).stdout
    overview = out.split("\nTool calls")[0].splitlines()[1:]
    for line in overview:
        label = line.strip().split("  ")[0]
        if label in ("provider",):
            continue
        assert any(t in line for t in ("[obs]", "[calc]", "[est]", "[clk]")) or "unavailable" in line or "no SessionEnd" in line, line


def test_inspect_missing_and_ambiguous(recorded, fixture):
    assert agentlens(recorded, "inspect", "zzzz").returncode == 1
    other = fixture("stop")
    other["session_id"] = SID[:4] + "-other"
    agentlens(recorded, "hook", "claude-code", stdin=json.dumps(other))
    r = agentlens(recorded, "inspect", SID[:4])
    assert r.returncode == 2 and SID in r.stderr


def test_session_without_start_reports_unavailable(subprocess_env, fixture):
    agentlens(subprocess_env, "hook", "claude-code", stdin=json.dumps(fixture("stop")))
    out = agentlens(subprocess_env, "inspect", SID).stdout
    assert "unavailable (no SessionStart" in out
    assert "no SessionEnd received" in out


def test_status_end_to_end(subprocess_env, fixture, isolated_env):
    r = agentlens(subprocess_env, "status")
    assert r.returncode == 0 and "not installed" in r.stdout
    assert agentlens(subprocess_env, "install").returncode == 0
    assert isolated_env["settings"].exists()
    agentlens(subprocess_env, "hook", "claude-code", stdin=json.dumps(fixture("session_start")))
    out = agentlens(subprocess_env, "status").stdout
    assert f"installed for {len(installer.HOOK_EVENTS)}/{len(installer.HOOK_EVENTS)} events" in out
    assert f"{PY}  ok" in out
    assert "0 pending" in out and "sessions" in out and "[calc]" in out


def test_status_warns_on_missing_interpreter_and_disabled_hooks(subprocess_env, isolated_env):
    installer.install(python="/does/not/exist/python")
    settings = isolated_env["settings"]
    doc = json.loads(settings.read_text())
    doc["disableAllHooks"] = True
    settings.write_text(json.dumps(doc))
    out = agentlens(subprocess_env, "status").stdout
    assert "MISSING" in out and "does not exist" in out
    assert "disableAllHooks" in out


def test_install_twice_and_uninstall_via_cli(subprocess_env, isolated_env):
    first = agentlens(subprocess_env, "install")
    second = agentlens(subprocess_env, "install")
    assert "Added AgentLens hooks" in first.stdout
    assert "already installed" in second.stdout
    r = agentlens(subprocess_env, "uninstall")
    assert "Removed AgentLens hooks" in r.stdout
    assert json.loads(isolated_env["settings"].read_text()) == {"hooks": {}}


def test_install_refuses_invalid_settings(subprocess_env, isolated_env):
    s = isolated_env["settings"]
    s.parent.mkdir(parents=True)
    s.write_text("{oops")
    r = agentlens(subprocess_env, "install")
    assert r.returncode == 1 and "not valid JSON" in r.stderr
    assert s.read_text() == "{oops"


def test_ingest_command(recorded):
    r = agentlens(recorded, "ingest")
    assert r.returncode == 0 and "Ingested" in r.stdout


def test_session_end_detached_ingest_eventually_drains(recorded):
    # SessionEnd spawned `agentlens ingest --quiet` in the background
    import time

    deadline = time.time() + 10
    while time.time() < deadline and any(paths.spool_dir().glob("*.json")):
        time.sleep(0.1)
    assert not any(paths.spool_dir().glob("*.json"))
    assert paths.db_path().exists()


def test_console_entry_routes_hook(monkeypatch, fixture, capsys):
    import io

    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(fixture("stop")).encode())))
    assert cli.main(["hook", "claude-code"]) == 0
    assert capsys.readouterr().out == ""


def test_help_and_no_args(capsys):
    assert cli.main([]) == 0
    assert "install" in capsys.readouterr().out


@pytest.mark.parametrize("ms, text", [(None, "unavailable"), (840, "840 ms"), (1840, "1.84 s"),
                                      (749_000, "12m 29s"), (3_720_000, "1h 02m")])
def test_duration_format(ms, text):
    assert fmt_duration_ms(ms) == text

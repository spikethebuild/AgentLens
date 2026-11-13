"""Opt-in dashboard auto-open on SessionStart (macOS, Terminal.app)."""

from __future__ import annotations

import json
import stat
import sys

import pytest

from agentlens import autoopen, cli, hook, paths


@pytest.fixture
def popen(monkeypatch):
    calls = []
    monkeypatch.setattr(autoopen.subprocess, "Popen", lambda argv, **kw: calls.append(argv))
    monkeypatch.setattr(autoopen.sys, "platform", "darwin")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    return calls


def enable():
    autoopen.enable(sys.executable)


def test_off_by_default(popen):
    assert autoopen.maybe_open("startup") == "skip: off"
    assert popen == []


@pytest.mark.parametrize("source", ["resume", "clear", "compact", "fork", None])
def test_only_new_sessions(popen, source):
    enable()
    assert autoopen.maybe_open(source).startswith("skip")
    assert popen == []


def test_opens_terminal_with_script(popen):
    enable()
    assert autoopen.maybe_open("startup", now=1000.0) == "opened"
    argv = popen[0]
    assert argv[:4] == ["open", "-g", "-a", "Terminal"]
    script = autoopen.script_path()
    assert argv[4] == str(script)
    assert stat.S_IMODE(script.stat().st_mode) == 0o700
    body = script.read_text()
    assert "-m agentlens dashboard" in body and str(paths.home()) in body


def test_cooldown_and_running_dashboard(popen):
    enable()
    assert autoopen.maybe_open("startup", now=1000.0) == "opened"
    assert autoopen.maybe_open("startup", now=1005.0) == "skip: just launched"
    autoopen.mark_running()  # this test process stands in for the TUI
    try:
        assert autoopen.maybe_open("startup", now=9999.0) == "skip: already open"
    finally:
        autoopen.clear_running()
    assert not autoopen.pid_path().exists()
    assert len(popen) == 1


def test_stale_pid_is_ignored(popen, monkeypatch):
    enable()
    paths.ensure_dir(autoopen.run_dir())
    autoopen.pid_path().write_text("424242\n")
    monkeypatch.setattr(autoopen, "_alive", lambda pid: False)
    assert autoopen.maybe_open("startup", now=1000.0) == "opened"


def test_non_interactive_and_platform(popen, monkeypatch):
    enable()
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "sdk-cli")
    assert autoopen.maybe_open("startup") == "skip: not an interactive session"
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setattr(autoopen.sys, "platform", "linux")
    assert autoopen.maybe_open("startup") == "skip: macOS only"
    assert popen == []


def test_missing_python(popen):
    autoopen.enable("/nonexistent/python")
    assert autoopen.maybe_open("startup").startswith("skip: dashboard python missing")


def test_config_keeps_other_keys():
    paths.ensure_dir(paths.config_path().parent)
    paths.config_path().write_text(json.dumps({"capture_prompts": True}))
    enable()
    autoopen.disable()
    cfg = json.loads(paths.config_path().read_text())
    assert cfg["capture_prompts"] is True and cfg["open_dashboard"] is False
    assert stat.S_IMODE(paths.config_path().stat().st_mode) == 0o600


def test_hook_session_start_triggers_open(popen, fixture):
    enable()
    payload = fixture("session_start")
    payload["source"] = "startup"
    hook.handle("claude-code", json.dumps(payload).encode(), 1000.0)
    assert len(popen) == 1
    hook.handle("claude-code", json.dumps(fixture("stop")).encode(), 1001.0)
    assert len(popen) == 1


def test_hook_survives_autoopen_crash(monkeypatch, fixture):
    monkeypatch.setattr(autoopen, "maybe_open", lambda *_: 1 / 0)
    payload = fixture("session_start")
    payload["source"] = "startup"
    hook.handle("claude-code", json.dumps(payload).encode(), 1000.0)  # must not raise


def test_cli_toggle(capsys, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    pytest.importorskip("textual")
    assert cli.main(["dashboard", "--auto-open", "on"]) == 0
    assert autoopen.settings() == (True, sys.executable)
    assert cli.main(["dashboard", "--auto-open", "off"]) == 0
    assert autoopen.settings()[0] is False


def test_window_on_request_comes_to_front(popen):
    enable()
    opened, msg = autoopen.open_on_request()
    assert opened and "Opened" in msg
    assert popen[0][:3] == ["open", "-a", "Terminal"]  # no -g: the person asked for it


def test_window_on_request_when_already_open(popen):
    enable()
    autoopen.mark_running()
    try:
        opened, msg = autoopen.open_on_request()
    finally:
        autoopen.clear_running()
    assert not opened and "already open" in msg and popen == []


def test_window_without_a_dashboard_python(popen, monkeypatch):
    monkeypatch.setattr(autoopen, "dashboard_python", lambda: None)
    opened, msg = autoopen.open_on_request()
    assert not opened and "agentlens-local[tui]" in msg and popen == []


def test_window_not_on_linux(popen, monkeypatch):
    monkeypatch.setattr(autoopen.sys, "platform", "linux")
    opened, msg = autoopen.open_on_request()
    assert not opened and "macOS only" in msg and popen == []


def test_auto_open_off_remembers_dashboard_python(capsys):
    pytest.importorskip("textual")
    assert cli.main(["dashboard", "--auto-open", "off"]) == 0
    assert autoopen.settings() == (False, sys.executable)
    assert "/agentlens-dashboard" in capsys.readouterr().out


def test_cli_window(popen, capsys):
    enable()
    assert cli.main(["dashboard", "--window"]) == 0
    assert "Opened" in capsys.readouterr().out and len(popen) == 1

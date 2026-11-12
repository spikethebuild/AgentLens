"""Claude Code plugin packaging: manifest, hooks.json, launcher, skill, dual-install guard."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agentlens import __version__, cli, installer, plugin, spool
from agentlens.adapters.claude_code import HOOK_EVENTS
from conftest import load_fixture

ROOT = Path(__file__).resolve().parent.parent
RUN_SH = ROOT / "hooks" / "run.sh"
SH = shutil.which("sh") or "/bin/sh"


# ------------------------------------------------------------- manifests


def test_hooks_json_is_generated_from_hook_events():
    on_disk = (ROOT / "hooks" / "hooks.json").read_text()
    assert on_disk == plugin.render_hooks_json(), "regenerate hooks/hooks.json from agentlens.plugin"


def test_hooks_json_handlers_are_async_launcher_calls():
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]
    assert set(hooks) == set(HOOK_EVENTS)
    assert "MessageDisplay" not in hooks and "PostModelSwitch" in hooks and "InstructionsLoaded" in hooks
    for event, groups in hooks.items():
        (group,) = groups
        assert "matcher" not in group  # all events
        (handler,) = group["hooks"]
        assert handler["type"] == "command" and handler["async"] is True
        assert handler["command"] == 'sh "${CLAUDE_PLUGIN_ROOT}/hooks/run.sh" hook claude-code'
        assert ("timeout" in handler) == (event != "SessionEnd")


def test_manifest_and_marketplace():
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "agentlens"
    assert manifest["version"] == __version__
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    (entry,) = market["plugins"]
    assert entry == {**entry, "name": "agentlens", "source": "./"}
    assert market["name"] and market["owner"]["name"]


def test_status_skill_is_user_invoked_and_short():
    text = (ROOT / "skills" / "status" / "SKILL.md").read_text()
    front = text.split("---")[1]
    assert "name: status" in front and "disable-model-invocation: true" in front
    assert '!`sh "${CLAUDE_PLUGIN_ROOT}/hooks/run.sh" status --brief`' in text
    assert len(text) < 600  # the skill body itself enters the model's context


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed")
def test_claude_plugin_validate_passes():
    r = subprocess.run(["claude", "plugin", "validate", str(ROOT)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr


# --------------------------------------------------------------- launcher


def launcher_env(isolated_env, **extra):
    env = {
        "HOME": str(isolated_env["home"]),
        "AGENTLENS_HOME": str(isolated_env["agentlens_home"]),
        "AGENTLENS_CLAUDE_SETTINGS": str(isolated_env["settings"]),
        "CLAUDE_PLUGIN_ROOT": str(ROOT),
        "PATH": os.environ["PATH"],
    }
    env.update(extra)
    return env


def run_sh(env, *args, stdin=b""):
    return subprocess.run([SH, str(RUN_SH), *args], input=stdin, env=env, capture_output=True, timeout=60)


def test_launcher_hook_spools_event_marked_as_plugin(isolated_env):
    env = launcher_env(isolated_env, AGENTLENS_PYTHON=sys.executable)
    r = run_sh(env, "hook", "claude-code", stdin=json.dumps(load_fixture("post_model_switch")).encode())
    assert r.returncode == 0 and r.stdout == b""
    (path,) = spool.pending()
    (ev,) = spool.read_events(path)
    assert ev.type == "model.switch" and ev.data["collector"] == "plugin"


def test_launcher_hook_without_python_exits_0_and_logs(isolated_env, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("cat", "mkdir", "date", "uname", "dirname", "printf"):
        src = shutil.which(tool)
        if src:
            (bindir / tool).symlink_to(src)
    env = launcher_env(isolated_env, PATH=str(bindir))
    r = run_sh(env, "hook", "claude-code", stdin=b'{"session_id":"s","hook_event_name":"Stop"}')
    assert r.returncode == 0 and r.stdout == b""
    log = (isolated_env["agentlens_home"] / "logs" / "agentlens.log").read_text()
    assert "no Python 3.10+ found" in log
    assert not spool.pending()


def test_launcher_skips_too_old_python(isolated_env, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    old = bindir / "python3"
    old.write_text("#!/bin/sh\nexit 1\n")  # fails the >= 3.10 check
    old.chmod(0o755)
    for tool in ("cat", "mkdir", "date", "uname", "dirname"):
        if shutil.which(tool):
            (bindir / tool).symlink_to(shutil.which(tool))
    env = launcher_env(isolated_env, PATH=str(bindir))
    r = run_sh(env, "hook", "claude-code", stdin=b"{}")
    assert r.returncode == 0
    assert "no Python 3.10+ found" in (isolated_env["agentlens_home"] / "logs" / "agentlens.log").read_text()


def test_launcher_hook_exits_0_even_if_python_crashes(isolated_env, tmp_path):
    crash = tmp_path / "crashpy"
    crash.write_text(f"#!/bin/sh\ncase \"$*\" in *version_info*) exec {sys.executable} \"$@\";; esac\nexit 3\n")
    crash.chmod(0o755)
    env = launcher_env(isolated_env, AGENTLENS_PYTHON=str(crash))
    assert run_sh(env, "hook", "claude-code", stdin=b"{}").returncode == 0


def test_launcher_runs_cli_status_brief(isolated_env):
    env = launcher_env(isolated_env, AGENTLENS_PYTHON=sys.executable)
    r = run_sh(env, "status", "--brief")
    assert r.returncode == 0, r.stderr
    lines = r.stdout.decode().strip().splitlines()
    assert 1 <= len(lines) <= 4
    assert lines[0].startswith(f"AgentLens {__version__}")


# ------------------------------------------------------- dual-install guard


def write_settings(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def test_plugin_enabled_precedence(tmp_path):
    user = tmp_path / "user.json"
    project = tmp_path / "proj"
    assert not plugin.plugin_enabled(user, project)
    write_settings(user, {"enabledPlugins": {"agentlens@agentlens": True}})
    assert plugin.plugin_enabled(user, project)
    write_settings(project / ".claude" / "settings.local.json", {"enabledPlugins": {"agentlens@agentlens": False}})
    assert not plugin.plugin_enabled(user, project)
    write_settings(user, {"enabledPlugins": {"other@x": True}})
    (project / ".claude" / "settings.local.json").unlink()
    assert not plugin.plugin_enabled(user, project)


def test_install_refuses_when_plugin_enabled(isolated_env, capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    settings = isolated_env["settings"]
    write_settings(settings, {"enabledPlugins": {"agentlens@agentlens": True}})
    assert cli.main(["install"]) == 1
    assert "fire twice" in capsys.readouterr().err
    assert installer.status(settings).events == []
    assert cli.main(["install", "--force"]) == 0
    assert installer.status(settings).events


def test_status_warns_about_double_install(isolated_env, capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    settings = isolated_env["settings"]
    assert cli.main(["install"]) == 0
    data = json.loads(settings.read_text())
    data["enabledPlugins"] = {"agentlens@agentlens": True}
    settings.write_text(json.dumps(data))
    capsys.readouterr()
    assert cli.main(["status", "--brief"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert "recording via plugin" in out[0]
    assert any("installed twice" in line for line in out)
    assert len(out) <= 4


def test_status_brief_when_nothing_installed(capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["status", "--brief"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert "NOT recording" in out[0] and len(out) <= 4


def test_cache_skill_is_user_invoked_and_short():
    text = (ROOT / "skills" / "cache" / "SKILL.md").read_text()
    front = text.split("---")[1]
    assert "name: cache" in front and "disable-model-invocation: true" in front
    assert '!`sh "${CLAUDE_PLUGIN_ROOT}/hooks/run.sh" cache --brief`' in text
    assert len(text) < 600

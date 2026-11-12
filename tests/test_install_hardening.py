"""Stage 2: scopes, CLAUDE_CONFIG_DIR, --dry-run, --repair, status across scopes, doctor."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentlens import cli, doctor, installer, paths
from agentlens.adapters.claude_code import HOOK_EVENTS

PY = sys.executable


@pytest.fixture
def project(tmp_path, monkeypatch):
    d = tmp_path / "proj"
    d.mkdir()
    monkeypatch.chdir(d)
    return d


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def our_handlers(doc: dict, event: str) -> list[dict]:
    return [h for g in doc.get("hooks", {}).get(event, []) for h in g.get("hooks", [])
            if "agentlens hook" in h.get("command", "")]


def backups(path: Path) -> list[Path]:
    return [p for p in path.parent.iterdir() if ".agentlens-bak-" in p.name]


# ------------------------------------------------------------------ paths


def test_claude_config_dir_moves_user_settings(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENTLENS_CLAUDE_SETTINGS")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert paths.claude_settings_path() == tmp_path / "cfg" / "settings.json"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert paths.claude_settings_path() == Path.home() / ".claude" / "settings.json"


def test_agentlens_override_wins_over_claude_config_dir(monkeypatch, tmp_path, isolated_env):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert paths.claude_settings_path() == isolated_env["settings"]


def test_scope_paths(project, isolated_env):
    assert paths.scope_settings_path("user") == isolated_env["settings"]
    assert paths.scope_settings_path("project", project) == project / ".claude" / "settings.json"
    assert paths.scope_settings_path("local", project) == project / ".claude" / "settings.local.json"
    with pytest.raises(ValueError):
        paths.scope_settings_path("managed", project)


# ------------------------------------------------------------------ scopes


@pytest.mark.parametrize("scope, name", [("project", "settings.json"), ("local", "settings.local.json")])
def test_install_and_uninstall_per_scope(project, isolated_env, capsys, scope, name):
    assert cli.main(["install", "--scope", scope, "--python", PY]) == 0
    target = project / ".claude" / name
    assert set(load(target)["hooks"]) == set(HOOK_EVENTS)
    assert not isolated_env["settings"].exists()  # user settings untouched
    out = capsys.readouterr().out
    if scope == "project":
        assert "usually committed" in out
    assert cli.main(["uninstall", "--scope", scope]) == 0
    assert not any(our_handlers(load(target), e) for e in HOOK_EVENTS)


def test_scope_and_settings_are_exclusive(project, capsys):
    with pytest.raises(SystemExit):
        cli.main(["install", "--scope", "local", "--settings", "x.json"])


def test_local_scope_warns_when_not_gitignored(project, capsys):
    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        pytest.skip("git not available")
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    cli.main(["install", "--scope", "local", "--python", PY])
    assert "not ignored by git" in capsys.readouterr().out
    (project / ".gitignore").write_text(".claude/settings.local.json\n")
    cli.main(["uninstall", "--scope", "local"])
    cli.main(["install", "--scope", "local", "--python", PY])
    assert "not ignored by git" not in capsys.readouterr().out


def test_status_lists_project_and_local_hooks(project, capsys):
    installer.install(paths.scope_settings_path("local", project), python=PY)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert f"local hooks     installed for {len(HOOK_EVENTS)}/{len(HOOK_EVENTS)} events" in out
    assert "not installed" in out  # the user scope line is unchanged


# ----------------------------------------------------------------- dry run


def test_dry_run_writes_nothing_and_shows_diff(settings_with_user_hooks, capsys):
    path = settings_with_user_hooks
    before = path.read_bytes()
    assert cli.main(["install", "--dry-run", "--python", PY]) == 0
    out = capsys.readouterr().out
    assert "Dry run" in out and "Nothing was written" in out
    assert "+++ " in out and '+            "command": ' in out
    assert "Would add AgentLens hooks" in out
    assert path.read_bytes() == before and backups(path) == []


def test_dry_run_on_missing_file_creates_nothing(isolated_env, capsys):
    assert cli.main(["install", "--dry-run", "--python", PY]) == 0
    assert not isolated_env["settings"].exists()
    assert "/dev/null" in capsys.readouterr().out


def test_uninstall_dry_run(settings_with_user_hooks, capsys):
    installer.install(python=PY)
    path = settings_with_user_hooks
    before = path.read_bytes()
    n = len(backups(path))
    assert cli.main(["uninstall", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Would remove AgentLens hooks" in out and "-            \"command\"" in out
    assert path.read_bytes() == before and len(backups(path)) == n


@pytest.fixture
def settings_with_user_hooks(isolated_env):
    path = isolated_env["settings"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]}}))
    return path


# ------------------------------------------------------------------ repair


def test_repair_normalizes_only_our_entries(settings_with_user_hooks):
    path = settings_with_user_hooks
    installer.install(python=PY)
    doc = load(path)
    cmd = installer.hook_command(PY)
    # duplicate in Stop, edited fields in PreToolUse, ours sharing a user group in Notification,
    # a retired event, and a user handler that must survive everything.
    doc["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": cmd, "async": True, "timeout": 5}]})
    doc["hooks"]["PreToolUse"][0]["hooks"][0]["async"] = False
    doc["hooks"]["Notification"] = [{"hooks": [{"type": "command", "command": "notify-send hi"},
                                               {"type": "command", "command": cmd, "async": True, "timeout": 5}]}]
    doc["hooks"]["MessageDisplay"] = [{"hooks": [{"type": "command", "command": cmd, "async": True}]}]
    path.write_text(json.dumps(doc))

    plain = installer.install(python=PY)  # conservative: none of this is touched
    assert not plain.changed

    change = installer.install(python=PY, repair=True)
    assert set(change.repaired) == {"Stop", "PreToolUse", "Notification"}
    assert change.removed == ["MessageDisplay"]
    doc = load(path)
    for event in HOOK_EVENTS:
        assert our_handlers(doc, event) == [installer._handler(event, PY)], event
    assert "MessageDisplay" not in doc["hooks"]
    assert {"type": "command", "command": "say done"} in [h for g in doc["hooks"]["Stop"] for h in g["hooks"]]
    assert {"type": "command", "command": "notify-send hi"} in [
        h for g in doc["hooks"]["Notification"] for h in g["hooks"]]
    assert not installer.install(python=PY, repair=True).changed  # idempotent


def test_repair_dry_run_reports_without_writing(settings_with_user_hooks, capsys):
    installer.install(python=PY)
    path = settings_with_user_hooks
    doc = load(path)
    doc["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": installer.hook_command(PY)}]})
    path.write_text(json.dumps(doc))
    before = path.read_bytes()
    assert cli.main(["install", "--repair", "--dry-run", "--python", PY]) == 0
    assert "Would repair AgentLens entries for 1 events: Stop" in capsys.readouterr().out
    assert path.read_bytes() == before


def test_repair_fixes_stale_interpreter(isolated_env):
    installer.install(python="/gone/python")
    change = installer.install(python=PY, repair=True)
    assert set(change.repaired) == set(HOOK_EVENTS)
    assert all(our_handlers(load(isolated_env["settings"]), e)[0]["command"] == installer.hook_command(PY)
               for e in HOOK_EVENTS)


# ------------------------------------------------------------------ doctor


def by_name(checks):
    out = {}
    for c in checks:
        out.setdefault(c.name, []).append(c)
    return out


def test_doctor_fails_when_nothing_records(project, capsys):
    assert cli.main(["doctor", "--no-hook-test"]) == 1
    out = capsys.readouterr().out
    assert "FAIL  recording" in out and "fix:" in out and "Doctor changes nothing" in out


def test_doctor_healthy_install_with_hook_test(project, isolated_env, monkeypatch):
    # the hook subprocess must import this checkout's agentlens
    src = str(Path(__file__).resolve().parent.parent / "src")
    monkeypatch.setenv("PYTHONPATH", src + os.pathsep + os.environ.get("PYTHONPATH", ""))
    installer.install(python=PY)
    checks = doctor.run(project)
    names = by_name(checks)
    assert names["recording"][0].level == doctor.OK
    assert names["user hooks"][0].level == doctor.OK
    assert names["hook test"][0].level == doctor.OK, names["hook test"][0].detail
    assert not any(c.level == doctor.FAIL for c in checks), doctor.render(checks)
    assert not paths.spool_dir().exists() or not list(paths.spool_dir().glob("*.json"))  # real spool untouched


def test_doctor_reports_missing_interpreter(project):
    installer.install(python="/does/not/exist/python")
    hook = by_name(doctor.run(project))["hook test"][0]
    assert hook.level == doctor.FAIL and "does not exist" in hook.detail


def test_doctor_warns_on_missing_events_and_duplicates(project, isolated_env):
    installer.install(python=PY)
    path = isolated_env["settings"]
    doc = load(path)
    del doc["hooks"]["ConfigChange"]
    path.write_text(json.dumps(doc))
    hooks = by_name(doctor.run(project, smoke=False))["user hooks"][0]
    assert hooks.level == doctor.WARN and "ConfigChange" in hooks.detail and hooks.fix == "agentlens install"

    installer.install(python=PY)
    doc = load(path)
    doc["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": installer.hook_command(PY)}]})
    path.write_text(json.dumps(doc))
    hooks = by_name(doctor.run(project, smoke=False))["user hooks"][0]
    assert hooks.level == doctor.WARN and "--repair" in hooks.fix


def test_doctor_plugin_plus_settings_and_split_commands(project, isolated_env):
    installer.install(python=PY)
    installer.install(paths.scope_settings_path("local", project), python="/other/python")
    doc = load(isolated_env["settings"])
    doc["enabledPlugins"] = {"agentlens@agentlens": True}
    isolated_env["settings"].write_text(json.dumps(doc))
    names = by_name(doctor.run(project, smoke=False))
    rec = names["recording"][0]
    assert rec.level == doctor.WARN and "fires twice" in rec.detail
    assert "agentlens uninstall --scope local" in rec.fix
    assert names["hook commands"][0].level == doctor.WARN


def test_doctor_disable_all_hooks_follows_precedence(project, isolated_env):
    installer.install(python=PY)
    doc = load(isolated_env["settings"])
    doc["disableAllHooks"] = True
    isolated_env["settings"].write_text(json.dumps(doc))
    assert by_name(doctor.run(project, smoke=False))["hooks enabled"][0].level == doctor.FAIL
    local = paths.scope_settings_path("local", project)
    local.parent.mkdir(parents=True)
    local.write_text(json.dumps({"disableAllHooks": False}))
    assert "hooks enabled" not in by_name(doctor.run(project, smoke=False))


def test_doctor_invalid_settings_and_data_problems(project, isolated_env):
    proj = paths.scope_settings_path("project", project)
    proj.parent.mkdir(parents=True)
    proj.write_text("{not json")
    paths.ensure_dir(paths.failed_dir())
    (paths.failed_dir() / "x.json").write_text("{}")
    stale = paths.spool_dir() / "old.json"
    stale.write_text("{}")
    old = time.time() - 2 * 3600
    os.utime(stale, (old, old))
    os.chmod(paths.home(), 0o755)
    paths.ensure_dir(paths.logs_dir())
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    (paths.logs_dir() / "agentlens.log").write_text(f"{stamp} pid=1 ingest failed\nTraceback...\n")
    names = by_name(doctor.run(project, smoke=False))
    assert names["project settings"][0].level == doctor.FAIL
    spool_details = " ".join(c.detail for c in names["spool"])
    assert "could not be ingested" in spool_details and "longer than an hour" in spool_details
    assert names["error log"][0].level == doctor.WARN and "ingest failed" in names["error log"][0].detail
    # ensure_dir above re-tightened the home dir; loosen it again for the mode check
    os.chmod(paths.home(), 0o755)
    assert by_name(doctor.run(project, smoke=False))["data dir"][0].level == doctor.WARN


def test_doctor_database_check(project):
    from agentlens import store

    store.connect().close()
    db = by_name(doctor.run(project, smoke=False))["database"][0]
    assert db.level == doctor.OK and "integrity ok" in db.detail

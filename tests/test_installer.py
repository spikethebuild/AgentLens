from __future__ import annotations

import json
import os
import stat

import pytest

from agentlens import installer, paths
from agentlens.adapters.claude_code import HOOK_EVENTS

PY = "/opt/py env/bin/python3"  # path with a space on purpose

USER_SETTINGS = {
    "model": "opus",
    "permissions": {"allow": ["Bash(git status)"]},
    "hooks": {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "~/bin/guard.sh", "timeout": 30}]}
        ],
        "Stop": [{"hooks": [{"type": "command", "command": "say done"}]}],
        "SessionEnd": [
            {"hooks": [
                {"type": "command", "command": "~/bin/cleanup.sh"},
            ]}
        ],
    },
}


@pytest.fixture
def settings(isolated_env):
    return isolated_env["settings"]


def write(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2))


def load(path):
    return json.loads(path.read_text())


def ours(doc):
    return [
        (event, h)
        for event, groups in doc.get("hooks", {}).items()
        for g in groups
        for h in g["hooks"]
        if "agentlens hook" in h.get("command", "")
    ]


def backups(path):
    return sorted(p for p in path.parent.iterdir() if ".agentlens-bak-" in p.name)


def test_settings_path_is_sandboxed(settings, isolated_env):
    assert paths.claude_settings_path() == settings
    assert str(settings).startswith(str(isolated_env["home"]))


def test_fresh_install_creates_file(settings):
    change = installer.install(python=PY)
    assert change.added == list(HOOK_EVENTS) and change.backup is None
    doc = load(settings)
    found = dict(ours(doc))
    assert set(found) == set(HOOK_EVENTS)
    for event, h in found.items():
        assert h["type"] == "command" and h["async"] is True
        assert h["command"] == '"/opt/py env/bin/python3" -m agentlens hook claude-code'
        if event == "SessionEnd":
            assert "timeout" not in h
        else:
            assert h["timeout"] == 5
    assert all("matcher" not in g for groups in doc["hooks"].values() for g in groups)


def test_install_preserves_everything_else(settings):
    write(settings, USER_SETTINGS)
    installer.install(python=PY)
    doc = load(settings)
    assert doc["model"] == "opus" and doc["permissions"] == USER_SETTINGS["permissions"]
    assert doc["hooks"]["PreToolUse"][0] == USER_SETTINGS["hooks"]["PreToolUse"][0]
    assert doc["hooks"]["Stop"][0] == USER_SETTINGS["hooks"]["Stop"][0]
    assert doc["hooks"]["SessionEnd"][0] == USER_SETTINGS["hooks"]["SessionEnd"][0]


def test_install_is_idempotent(settings):
    write(settings, USER_SETTINGS)
    installer.install(python=PY)
    before = settings.read_bytes()
    n_backups = len(backups(settings))
    change = installer.install(python=PY)
    assert not change.changed and change.unchanged == list(HOOK_EVENTS)
    assert settings.read_bytes() == before
    assert len(backups(settings)) == n_backups
    assert len(ours(load(settings))) == len(HOOK_EVENTS)


def test_backup_holds_original(settings):
    write(settings, USER_SETTINGS)
    original = settings.read_bytes()
    change = installer.install(python=PY)
    assert change.backup is not None and change.backup.read_bytes() == original


def test_reinstall_updates_moved_interpreter(settings):
    installer.install(python=PY)
    change = installer.install(python="/new/python")
    assert change.updated == list(HOOK_EVENTS) and not change.added
    cmds = {h["command"] for _, h in ours(load(settings))}
    assert cmds == {'"/new/python" -m agentlens hook claude-code'}


@pytest.mark.parametrize(
    "content",
    ["{ invalid json", '["a list"]', '{"hooks": []}', '{"hooks": {"Stop": {}}}', "\xff\xfe"],
)
def test_invalid_settings_are_never_touched(settings, content):
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_bytes(content.encode("latin-1"))
    before = settings.read_bytes()
    with pytest.raises(installer.SettingsError):
        installer.install(python=PY)
    with pytest.raises(installer.SettingsError):
        installer.uninstall()
    assert settings.read_bytes() == before
    assert backups(settings) == []


def test_uninstall_removes_only_ours(settings):
    write(settings, USER_SETTINGS)
    installer.install(python=PY)
    change = installer.uninstall()
    assert sorted(change.removed) == sorted(HOOK_EVENTS)
    assert load(settings) == USER_SETTINGS


def test_uninstall_keeps_foreign_handler_sharing_a_group(settings):
    doc = {"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "say done"},
        {"type": "command", "command": '"/x/python" -m agentlens hook claude-code', "async": True},
    ]}]}}
    write(settings, doc)
    installer.uninstall()
    assert load(settings) == {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]}}


def test_uninstall_is_idempotent_and_handles_missing_file(settings):
    assert not installer.uninstall().changed
    assert not settings.exists()
    installer.install(python=PY)
    installer.uninstall()
    before = settings.read_bytes()
    assert not installer.uninstall().changed
    assert settings.read_bytes() == before


def test_file_mode_preserved(settings):
    write(settings, USER_SETTINGS)
    os.chmod(settings, 0o600)
    installer.install(python=PY)
    assert stat.S_IMODE(os.stat(settings).st_mode) == 0o600


def test_symlinked_settings_written_through(settings, tmp_path):
    real = tmp_path / "dotfiles" / "claude-settings.json"
    write(real, USER_SETTINGS)
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.symlink_to(real)
    installer.install(python=PY)
    assert settings.is_symlink()
    assert len(ours(load(real))) == len(HOOK_EVENTS)


def test_concurrent_edit_aborts(settings, monkeypatch):
    write(settings, USER_SETTINGS)
    real_backup = installer._backup

    def backup_then_someone_writes(target, original):
        b = real_backup(target, original)
        target.write_text('{"edited": "by claude"}')
        return b

    monkeypatch.setattr(installer, "_backup", backup_then_someone_writes)
    with pytest.raises(installer.SettingsError):
        installer.install(python=PY)
    assert load(settings) == {"edited": "by claude"}
    assert not any(p.name.startswith(".settings.json.agentlens-tmp") for p in settings.parent.iterdir())


def test_status(settings):
    st = installer.status()
    assert not st.exists and st.missing == list(HOOK_EVENTS)
    write(settings, dict(USER_SETTINGS, disableAllHooks=True))
    installer.install(python=PY)
    st = installer.status()
    assert st.valid and st.missing == [] and st.disable_all_hooks
    assert st.commands == {installer.hook_command(PY)}


def test_command_escaping():
    assert installer.hook_command('/a"b$c/python') == '"/a\\"b\\$c/python" -m agentlens hook claude-code'

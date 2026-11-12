"""Install / uninstall AgentLens hooks in Claude Code's settings.json.

Rules:
* Invalid JSON or an unexpected shape: stop, change nothing.
* Back up the original before the first change; write atomically.
* Idempotent: an existing AgentLens entry is never duplicated.
* Uninstall removes only handlers whose command contains ``agentlens hook``.
* A symlinked settings.json (dotfile repos) is written through, not replaced.
* ``dry_run`` computes the same change and a unified diff, and writes nothing.
* ``repair`` normalizes AgentLens's own handlers (one canonical handler per
  registered event, none on other events); other handlers are never touched.
"""

from __future__ import annotations

import difflib
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentlens import paths
from agentlens.adapters.claude_code import HOOK_EVENTS

MARKER = "agentlens hook"
HOOK_TIMEOUT_S = 5
# SessionEnd gets no timeout: Claude Code raises the shared SessionEnd budget to
# the highest per-hook timeout, which would slow exit for the user's other hooks.
NO_TIMEOUT_EVENTS = {"SessionEnd"}


class SettingsError(RuntimeError):
    pass


@dataclass
class Change:
    settings_path: Path
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    repaired: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    backup: Path | None = None
    dry_run: bool = False
    diff: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.removed or self.repaired)


def hook_command(python: str | None = None) -> str:
    python = python or sys.executable
    escaped = "".join("\\" + c if c in '"\\$`' else c for c in python)
    return f'"{escaped}" -m agentlens hook claude-code'


def _handler(event: str, python: str | None) -> dict[str, Any]:
    h: dict[str, Any] = {"type": "command", "command": hook_command(python), "async": True}
    if event not in NO_TIMEOUT_EVENTS:
        h["timeout"] = HOOK_TIMEOUT_S
    return h


def _is_ours(handler: Any) -> bool:
    return isinstance(handler, dict) and MARKER in str(handler.get("command", ""))


# ------------------------------------------------------------------- file IO


def _target(path: Path) -> Path:
    return Path(os.path.realpath(path)) if path.is_symlink() else path


def _load(path: Path) -> tuple[dict, bytes | None]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}, None
    if not raw.strip():
        return {}, raw
    try:
        doc = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SettingsError(f"{path} is not valid JSON ({exc}); not modifying it") from exc
    if not isinstance(doc, dict):
        raise SettingsError(f"{path} does not contain a JSON object; not modifying it")
    return doc, raw


def _hooks_section(doc: dict, path: Path) -> dict:
    hooks = doc.get("hooks")
    if hooks is None:
        return {}
    if not isinstance(hooks, dict):
        raise SettingsError(f'"hooks" in {path} is not an object; not modifying it')
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            raise SettingsError(f'"hooks.{event}" in {path} is not a list; not modifying it')
    return hooks


def _backup(target: Path, original: bytes) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = target.with_name(f"{target.name}.agentlens-bak-{stamp}")
    n = 1
    while backup.exists():
        backup = target.with_name(f"{target.name}.agentlens-bak-{stamp}-{n}")
        n += 1
    shutil.copy2(target, backup)
    if backup.read_bytes() != original:
        raise SettingsError(f"backup verification failed for {backup}")
    return backup


def _atomic_write(target: Path, doc: dict, original: bytes | None) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = 0o644
    if original is not None:
        mode = os.stat(target).st_mode & 0o7777
    body = _render(doc).encode("utf-8")
    tmp = target.with_name(f".{target.name}.agentlens-tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, body)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.chmod(tmp, mode)
        # Refuse to clobber a concurrent edit (e.g. Claude Code saving a setting).
        current = target.read_bytes() if target.exists() else None
        if current != original:
            raise SettingsError(f"{target} changed while AgentLens was editing it; try again")
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()


def _render(doc: dict) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def _diff(path: Path, original: bytes | None, doc: dict) -> str:
    before = original.decode("utf-8", "replace") if original else ""
    lines = difflib.unified_diff(
        before.splitlines(keepends=True), _render(doc).splitlines(keepends=True),
        fromfile=f"{path} (current)" if original is not None else "/dev/null",
        tofile=f"{path} (after)",
    )
    return "".join(lines)


def _commit(path: Path, target: Path, doc: dict, original: bytes | None, change: Change,
            dry_run: bool = False) -> Change:
    if not change.changed:
        return change
    if dry_run:
        change.dry_run = True
        change.diff = _diff(path, original, doc)
        return change
    if original is not None:
        change.backup = _backup(target, original)
    _atomic_write(target, doc, original)
    return change


# ----------------------------------------------------------------- operations


def _ours_in(groups: list) -> list[dict]:
    return [
        h for g in groups if isinstance(g, dict)
        for h in (g.get("hooks") if isinstance(g.get("hooks"), list) else [])
        if _is_ours(h)
    ]


def _strip_ours(groups: list) -> list:
    """``groups`` without AgentLens handlers; groups emptied by that are dropped."""
    out = []
    for group in groups:
        inner = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(inner, list):
            out.append(group)
            continue
        kept = [h for h in inner if not _is_ours(h)]
        if len(kept) != len(inner):
            if not kept:
                continue
            group = dict(group, hooks=kept)
        out.append(group)
    return out


def _canonical(groups: list, event: str, python: str | None) -> bool:
    """Exactly one AgentLens handler, in its own all-matching group, in canonical form."""
    mine = [g for g in groups if isinstance(g, dict) and any(_is_ours(h) for h in _ours_in([g]))]
    if len(mine) != 1:
        return False
    g = mine[0]
    return (
        set(g) <= {"hooks", "matcher"} and g.get("matcher") in (None, "", "*")
        and g.get("hooks") == [_handler(event, python)]
    )


def install(settings_path: Path | None = None, python: str | None = None, *,
            repair: bool = False, dry_run: bool = False) -> Change:
    path = settings_path or paths.claude_settings_path()
    target = _target(path)
    doc, original = _load(target)
    hooks = _hooks_section(doc, path)
    change = Change(settings_path=path)
    wanted_cmd = hook_command(python)

    for event in HOOK_EVENTS:
        groups = hooks.setdefault(event, [])
        ours = _ours_in(groups)
        if not ours:
            groups.append({"hooks": [_handler(event, python)]})
            change.added.append(event)
        elif repair and not _canonical(groups, event, python):
            hooks[event] = _strip_ours(groups) + [{"hooks": [_handler(event, python)]}]
            change.repaired.append(event)
        elif any(h.get("command") != wanted_cmd for h in ours):
            for h in ours:
                h["command"] = wanted_cmd  # e.g. the venv moved; keep the user's other fields
            change.updated.append(event)
        else:
            change.unchanged.append(event)

    if repair:  # AgentLens handlers on events it no longer registers (e.g. MessageDisplay)
        for event in [e for e in hooks if e not in HOOK_EVENTS]:
            if _ours_in(hooks[event]):
                kept = _strip_ours(hooks[event])
                if kept:
                    hooks[event] = kept
                else:
                    del hooks[event]
                change.removed.append(event)

    if change.changed:
        doc["hooks"] = hooks
    return _commit(path, target, doc, original, change, dry_run)


def uninstall(settings_path: Path | None = None, *, dry_run: bool = False) -> Change:
    path = settings_path or paths.claude_settings_path()
    target = _target(path)
    doc, original = _load(target)
    change = Change(settings_path=path)
    if original is None:
        return change
    hooks = _hooks_section(doc, path)

    for event in list(hooks):
        if not _ours_in(hooks[event]):
            continue
        change.removed.append(event)
        kept = _strip_ours(hooks[event])
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    return _commit(path, target, doc, original, change, dry_run)


@dataclass
class InstallStatus:
    settings_path: Path
    exists: bool
    valid: bool
    error: str | None
    events: list[str]
    missing: list[str]
    commands: set[str]
    disable_all_hooks: bool


def status(settings_path: Path | None = None) -> InstallStatus:
    path = settings_path or paths.claude_settings_path()
    try:
        doc, original = _load(_target(path))
        hooks = _hooks_section(doc, path)
    except SettingsError as exc:
        return InstallStatus(path, True, False, str(exc), [], list(HOOK_EVENTS), set(), False)
    events, commands = [], set()
    for event, groups in hooks.items():
        for g in groups:
            inner = g.get("hooks") if isinstance(g, dict) else None
            for h in inner if isinstance(inner, list) else []:
                if _is_ours(h):
                    events.append(event)
                    commands.add(str(h.get("command")))
    return InstallStatus(
        settings_path=path,
        exists=original is not None,
        valid=True,
        error=None,
        events=sorted(set(events)),
        missing=[e for e in HOOK_EVENTS if e not in events],
        commands=commands,
        disable_all_hooks=bool(doc.get("disableAllHooks")),
    )

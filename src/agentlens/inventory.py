"""Inventory of enabled Claude Code plugins and what they add to every session.

Which plugins are enabled comes from ``enabledPlugins`` in the user, project and
local settings files (documented). Where each one is installed comes from
``<claude dir>/plugins/installed_plugins.json``, which is **not documented**: it
is parsed defensively and a plugin whose files can't be found is reported with
its listing size unavailable.

The listing estimate counts the name and description of every skill, agent and
command the model can see (``disable-model-invocation: true`` ones excluded),
divided by 4 bytes per token. Claude Code shortens long listings, so this is an
upper bound and always labelled ``[est]``. Nothing here reads file contents
beyond those frontmatter lines, and nothing is stored.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from agentlens import paths

BYTES_PER_TOKEN_EST = 4
_FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---", re.S)


@dataclass
class PluginInfo:
    key: str                       # "<name>@<marketplace>"
    name: str
    install_path: Path | None
    skills: set[str] = field(default_factory=set)    # model-visible skill names
    agents: set[str] = field(default_factory=set)
    commands: set[str] = field(default_factory=set)
    listing_bytes: int | None = None                 # None: files not found
    hook_handlers: int | None = None

    @property
    def listing_tokens_est(self) -> int | None:
        return None if self.listing_bytes is None else self.listing_bytes // BYTES_PER_TOKEN_EST


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _frontmatter(path: Path) -> dict[str, str] | None:
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            head = f.read(8192)
    except OSError:
        return None
    m = _FRONTMATTER.match(head)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line[:1].isspace():
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip().strip("\"'")
    return out


def enabled_plugins(project_dir: Path | None = None) -> list[str]:
    """``name@marketplace`` keys enabled after precedence (local > project > user)."""
    files = [paths.claude_settings_path()]
    if project_dir is not None:
        files += [project_dir / ".claude" / "settings.json", project_dir / ".claude" / "settings.local.json"]
    state: dict[str, bool] = {}
    for f in files:
        plugins = _read_json(f).get("enabledPlugins")
        if isinstance(plugins, dict):
            for k, v in plugins.items():
                if isinstance(k, str) and isinstance(v, bool):
                    state[k] = v
    return sorted(k for k, v in state.items() if v)


def _install_paths() -> dict[str, Path]:
    registry = _read_json(paths.claude_settings_path().parent / "plugins" / "installed_plugins.json")
    plugins = registry.get("plugins")
    out: dict[str, Path] = {}
    if not isinstance(plugins, dict):
        return out
    for key, entries in plugins.items():
        if isinstance(entries, list):
            for entry in entries:
                p = entry.get("installPath") if isinstance(entry, dict) else None
                if isinstance(p, str) and Path(p).is_dir():
                    out[key] = Path(p)
                    break
    return out


def _scan(info: PluginInfo) -> None:
    root = info.install_path
    if root is None:
        return
    total = 0
    for kind, pattern in (("skills", "skills/*/SKILL.md"), ("agents", "agents/*.md"), ("commands", "commands/*.md")):
        for f in sorted(root.glob(pattern)):
            fm = _frontmatter(f)
            if fm is None or fm.get("disable-model-invocation") == "true":
                continue
            name = fm.get("name") or (f.parent.name if kind == "skills" else f.stem)
            getattr(info, kind).add(name)
            total += len(name) + len(fm.get("description", "")) + len(info.name) + 4
    info.listing_bytes = total
    hooks = _read_json(root / "hooks" / "hooks.json").get("hooks")
    if isinstance(hooks, dict):
        info.hook_handlers = sum(
            len(g.get("hooks", [])) for groups in hooks.values() if isinstance(groups, list)
            for g in groups if isinstance(g, dict) and isinstance(g.get("hooks"), list)
        )
    else:
        info.hook_handlers = 0


def inventory(project_dir: Path | None = None) -> list[PluginInfo]:
    installed = _install_paths()
    out = []
    for key in enabled_plugins(project_dir):
        info = PluginInfo(key=key, name=key.split("@", 1)[0], install_path=installed.get(key))
        _scan(info)
        out.append(info)
    return out


def find_skill(plugins: list[PluginInfo], skill: str) -> str | None:
    """``/<plugin>:<skill>`` if an enabled plugin ships that skill, else None."""
    for p in plugins:
        if skill in p.skills:
            return f"/{p.name}:{skill}"
    return None

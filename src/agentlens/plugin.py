"""Claude Code plugin packaging helpers.

The repository root is the plugin root (``.claude-plugin/plugin.json``). Hooks in
``hooks/hooks.json`` call ``hooks/run.sh``, which finds a Python 3.10+ interpreter,
puts the plugin's own ``src/`` on ``PYTHONPATH`` and runs the standard-library-only
hook fast path. ``hooks.json`` is generated from :data:`HOOK_EVENTS` so the plugin
and ``agentlens install`` always register the same events.

Plugin hooks and settings hooks are not merged by Claude Code: if both are
installed, every event fires twice. :func:`plugin_enabled` lets ``status`` and
``install`` detect that. It only reads settings files; it never writes them.
"""

from __future__ import annotations

import json
from pathlib import Path

from agentlens.adapters.claude_code import HOOK_EVENTS

PLUGIN_NAME = "agentlens"
HOOK_COMMAND = 'sh "${CLAUDE_PLUGIN_ROOT}/hooks/run.sh" hook claude-code'
HOOK_TIMEOUT_S = 5
NO_TIMEOUT_EVENTS = {"SessionEnd"}  # keep the shared SessionEnd budget at its default


def hooks_json() -> dict:
    """The content of ``hooks/hooks.json``."""
    events = {}
    for event in HOOK_EVENTS:
        handler: dict = {"type": "command", "command": HOOK_COMMAND, "async": True}
        if event not in NO_TIMEOUT_EVENTS:
            handler["timeout"] = HOOK_TIMEOUT_S
        events[event] = [{"hooks": [handler]}]
    return {
        "description": "AgentLens: record session, tool and cache events locally (async, never blocks).",
        "hooks": events,
    }


def render_hooks_json() -> str:
    return json.dumps(hooks_json(), indent=2) + "\n"


def _enabled_in(path: Path) -> bool | None:
    """True/False if ``path`` sets agentlens@<marketplace> in enabledPlugins, else None."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    plugins = data.get("enabledPlugins") if isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        return None
    result = None
    for key, value in plugins.items():
        if isinstance(key, str) and key.split("@", 1)[0] == PLUGIN_NAME and isinstance(value, bool):
            result = value or bool(result)
    return result


def plugin_enabled(user_settings: Path, project_dir: Path | None = None) -> bool:
    """Whether the AgentLens plugin is enabled for this user/project.

    Precedence follows Claude Code: local overrides project overrides user.
    """
    candidates = [user_settings]
    if project_dir is not None:
        candidates += [project_dir / ".claude" / "settings.json", project_dir / ".claude" / "settings.local.json"]
    state = False
    for path in candidates:
        value = _enabled_in(path)
        if value is not None:
            state = value
    return state

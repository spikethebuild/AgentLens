"""Filesystem locations. Everything AgentLens writes lives under AGENTLENS_HOME."""

from __future__ import annotations

import os
from pathlib import Path

DIR_MODE = 0o700


def home() -> Path:
    env = os.environ.get("AGENTLENS_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".agentlens"


def spool_dir() -> Path:
    return home() / "spool"


def failed_dir() -> Path:
    return spool_dir() / "failed"


def logs_dir() -> Path:
    return home() / "logs"


def db_path() -> Path:
    return home() / "agentlens.db"


def lock_path() -> Path:
    return home() / "ingest.lock"


def salt_path() -> Path:
    return home() / "salt"


def config_path() -> Path:
    return home() / "config.json"


SCOPES = ("user", "project", "local")


def claude_settings_path() -> Path:
    """User-level Claude Code settings.

    ``AGENTLENS_CLAUDE_SETTINGS`` (tests, custom setups) wins; otherwise Claude
    Code's own ``CLAUDE_CONFIG_DIR`` moves the user settings directory.
    """
    env = os.environ.get("AGENTLENS_CLAUDE_SETTINGS")
    if env:
        return Path(env).expanduser()
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir).expanduser() / "settings.json"
    return Path.home() / ".claude" / "settings.json"


def scope_settings_path(scope: str, project_dir: Path | None = None) -> Path:
    """Settings file for a Claude Code scope: user, project or local."""
    if scope == "user":
        return claude_settings_path()
    base = (project_dir or Path.cwd()) / ".claude"
    if scope == "project":
        return base / "settings.json"
    if scope == "local":
        return base / "settings.local.json"
    raise ValueError(f"unknown scope {scope!r}; expected one of {', '.join(SCOPES)}")


def ensure_dir(path: Path) -> Path:
    """Create ``path`` with mode 0700.

    ``mkdir(parents=True)`` applies ``mode`` only to the leaf, so every
    directory from AGENTLENS_HOME down to ``path`` is created and tightened
    explicitly. Directories above AGENTLENS_HOME are created with defaults
    and never chmod-ed.
    """
    root = home()
    try:
        rel = path.relative_to(root)
        chain = [root] + [root.joinpath(*rel.parts[: i + 1]) for i in range(len(rel.parts))]
    except ValueError:
        chain = [path]
    chain[0].parent.mkdir(parents=True, exist_ok=True)
    for d in chain:
        d.mkdir(exist_ok=True, mode=DIR_MODE)
        try:
            os.chmod(d, DIR_MODE)
        except OSError:
            pass
    return path

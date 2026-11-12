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


def claude_settings_path() -> Path:
    """User-level Claude Code settings. Overridable for tests and custom setups."""
    env = os.environ.get("AGENTLENS_CLAUDE_SETTINGS")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".claude" / "settings.json"


def ensure_dir(path: Path) -> Path:
    """Create ``path`` (and parents) with mode 0700, tightening an existing dir."""
    path.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    try:
        os.chmod(path, DIR_MODE)
    except OSError:
        pass
    return path

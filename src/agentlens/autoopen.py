"""Open the dashboard in a new Terminal window when a Claude Code session starts.

Opt-in (``agentlens dashboard --auto-open on``), macOS only. Called from the hook
fast path on ``SessionStart`` with ``source == "startup"``; standard library only,
never raises, never waits.

How the window is opened: a ``.command`` file in ``$AGENTLENS_HOME/run`` is handed
to ``open -g -a Terminal``. Terminal runs it in a new window. This needs no
Automation (Apple Events) permission, and ``-g`` keeps the editor in front.

At most one dashboard: the TUI writes its PID to ``run/dashboard.pid`` while it
runs, and a launch stamp suppresses a second window when two sessions start
within a few seconds of each other.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from agentlens import paths

LAUNCH_COOLDOWN_S = 15.0


def run_dir() -> Path:
    return paths.home() / "run"


def pid_path() -> Path:
    return run_dir() / "dashboard.pid"


def _stamp_path() -> Path:
    return run_dir() / "dashboard.launch"


def script_path() -> Path:
    return run_dir() / "dashboard.command"


# ------------------------------------------------------------------ settings


def _read_config() -> dict:
    try:
        raw = json.loads(paths.config_path().read_text())
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_config(cfg: dict) -> None:
    path = paths.config_path()
    paths.ensure_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def settings() -> tuple[bool, str | None]:
    """(enabled, python that runs the dashboard)."""
    cfg = _read_config()
    py = cfg.get("dashboard_python")
    return cfg.get("open_dashboard") is True, py if isinstance(py, str) and py else None


def enable(python: str) -> None:
    cfg = _read_config()
    cfg["open_dashboard"] = True
    cfg["dashboard_python"] = python
    _write_config(cfg)


def disable() -> None:
    cfg = _read_config()
    cfg["open_dashboard"] = False
    _write_config(cfg)


# ---------------------------------------------------------- single instance


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def dashboard_running() -> bool:
    try:
        pid = int(pid_path().read_text().strip())
    except (OSError, ValueError):
        return False
    return pid > 0 and _alive(pid)


def mark_running() -> None:
    """Called by the TUI on start; :func:`clear_running` on exit."""
    try:
        paths.ensure_dir(run_dir())
        pid_path().write_text(f"{os.getpid()}\n")
    except OSError:
        pass


def clear_running() -> None:
    try:
        if pid_path().read_text().strip() == str(os.getpid()):
            pid_path().unlink()
    except OSError:
        pass


def _claim_launch(now: float) -> bool:
    """True if no other launch happened in the last LAUNCH_COOLDOWN_S seconds."""
    stamp = _stamp_path()
    try:
        if now - stamp.stat().st_mtime < LAUNCH_COOLDOWN_S:
            return False
    except OSError:
        pass
    try:
        paths.ensure_dir(run_dir())
        stamp.write_text(f"{now}\n")
    except OSError:
        return False
    return True


# ------------------------------------------------------------------ launch


def interactive_session(env: dict[str, str] | None = None) -> bool:
    """Best effort: skip `claude -p` / SDK runs.

    ``CLAUDE_CODE_ENTRYPOINT`` is not documented; it is ``cli`` in an interactive
    terminal session (observed in 2.1.323). Absent means unknown: allow.
    """
    env = os.environ if env is None else env
    entry = env.get("CLAUDE_CODE_ENTRYPOINT")
    return entry is None or entry == "cli"


def write_script(python: str) -> Path:
    home = str(paths.home())
    body = (
        "#!/bin/sh\n"
        "# Written by AgentLens: opens the dashboard (agentlens dashboard --auto-open off to stop).\n"
        f"export AGENTLENS_HOME={shlex.quote(home)}\n"
        "printf '\\033]0;AgentLens\\007'\n"
        f"exec {shlex.quote(python)} -m agentlens dashboard\n"
    )
    path = script_path()
    paths.ensure_dir(run_dir())
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
    with os.fdopen(fd, "w") as f:
        f.write(body)
    os.chmod(tmp, 0o700)
    os.replace(tmp, path)
    return path


def maybe_open(start_source: str | None, now: float | None = None) -> str:
    """Open the dashboard if enabled; returns what happened (for logs and tests)."""
    if start_source != "startup":
        return "skip: not a new session"
    enabled, python = settings()
    if not enabled:
        return "skip: off"
    if sys.platform != "darwin":
        return "skip: macOS only"
    if not interactive_session():
        return "skip: not an interactive session"
    if not python or not os.path.exists(python):
        return "skip: dashboard python missing (run `agentlens dashboard --auto-open on` again)"
    if dashboard_running():
        return "skip: already open"
    if not _claim_launch(time.time() if now is None else now):
        return "skip: just launched"
    script = write_script(python)
    subprocess.Popen(
        ["open", "-g", "-a", "Terminal", str(script)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, close_fds=True,
    )
    return "opened"

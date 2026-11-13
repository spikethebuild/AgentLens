"""Open the dashboard in a Terminal window: on request, or (opt-in) on session start.

On request: ``agentlens dashboard --window`` (the plugin's ``/agentlens-dashboard``
command runs it), which brings the window to the front.
Opt-in: ``agentlens dashboard --auto-open on`` opens it, in the background, when a
new Claude Code session starts (from the hook fast path on ``SessionStart`` with
``source == "startup"``). macOS only; standard library only; never waits.

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


def disable(python: str | None = None) -> None:
    cfg = _read_config()
    cfg["open_dashboard"] = False
    if python:
        cfg["dashboard_python"] = python
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


def dashboard_python() -> str | None:
    """The Python that runs the dashboard: the saved one, else this one if it has the `tui` extra."""
    _, saved = settings()
    if saved and os.path.exists(saved):
        return saved
    try:
        import importlib.util

        if importlib.util.find_spec("textual") is not None:
            return sys.executable
    except (ImportError, ValueError):
        pass
    return None


def _launch(python: str, background: bool) -> None:
    script = write_script(python)
    argv = ["open", "-a", "Terminal", str(script)]
    if background:
        argv.insert(1, "-g")
    subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, close_fds=True,
    )


def open_on_request() -> tuple[bool, str]:
    """(opened, one-line message) for `agentlens dashboard --window`."""
    if sys.platform != "darwin":
        return False, "Opening the dashboard in a window is supported on macOS only; run `agentlens` in a terminal."
    if dashboard_running():
        return False, "The AgentLens dashboard is already open in a Terminal window."
    python = dashboard_python()
    if python is None:
        return False, ("No Python with the dashboard installed was found. Install `agentlens-local[tui]` "
                       "and run `agentlens dashboard --auto-open off` once to save it.")
    _launch(python, background=False)
    return True, "Opened the AgentLens dashboard in a new Terminal window."


def remember_python(python: str) -> None:
    cfg = _read_config()
    cfg["dashboard_python"] = python
    _write_config(cfg)


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
    _launch(python, background=True)
    return "opened"

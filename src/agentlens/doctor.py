"""``agentlens doctor``: read-only health checks with a fix for each problem.

Nothing here edits settings or recorded data. The hook end-to-end test runs each
configured AgentLens hook command against a *temporary* AGENTLENS_HOME, so the
real spool and database are never touched. Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agentlens import installer, paths, plugin, spool

OK, WARN, FAIL = "ok", "warn", "FAIL"
SPOOL_STALE_S = 3600
QUIET_DAYS = 7
SMOKE_TIMEOUT_S = 15
_LOG_HEAD = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4}) pid=\d+ (.*)$")


@dataclass
class Check:
    level: str
    name: str
    detail: str
    fix: str = ""


def interpreter_of(command: str) -> str | None:
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    return parts[0] if parts else None


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _scope_flag(scope: str) -> str:
    return "" if scope == "user" else f" --scope {scope}"


# ------------------------------------------------------------------ checks


def check_python() -> Check:
    v = sys.version_info
    ok = v >= (3, 10)
    return Check(OK if ok else FAIL, "python", f"{v.major}.{v.minor}.{v.micro} ({sys.executable})",
                 "" if ok else "AgentLens needs Python 3.10 or newer")


def check_home() -> Check:
    home = paths.home()
    if not home.exists():
        return Check(OK, "data dir", f"{home} not created yet (created on the first recorded event)")
    if not os.access(home, os.W_OK):
        return Check(FAIL, "data dir", f"{home} is not writable", f"chmod u+w '{home}'")
    mode = home.stat().st_mode & 0o777
    if mode & 0o077:
        return Check(WARN, "data dir", f"{home} has mode {mode:o}; other users can read it",
                     f"chmod 700 '{home}'")
    return Check(OK, "data dir", f"{home} (mode 700)")


@dataclass
class ScopeState:
    scope: str
    path: Path
    status: installer.InstallStatus


def scope_states(project_dir: Path) -> list[ScopeState]:
    return [ScopeState(s, p, installer.status(p))
            for s in paths.SCOPES for p in [paths.scope_settings_path(s, project_dir)]]


def check_settings(states: list[ScopeState]) -> list[Check]:
    out = []
    for st in states:
        if not st.status.valid:
            out.append(Check(FAIL, f"{st.scope} settings", st.status.error or f"{st.path} is invalid",
                             f"fix the JSON in {st.path}; AgentLens will not edit it until then"))
            continue
        if not st.status.events:
            continue
        missing = st.status.missing
        if missing:
            out.append(Check(WARN, f"{st.scope} hooks", f"{st.path}: no hook for {', '.join(missing)}",
                             f"agentlens install{_scope_flag(st.scope)}"))
            continue
        py = interpreter_of(sorted(st.status.commands)[0]) if st.status.commands else None
        try:
            would = installer.install(st.path, python=py, repair=True, dry_run=True)
        except installer.SettingsError:
            would = None
        if would is not None and would.changed:
            what = ", ".join(would.repaired + would.removed + would.updated)
            out.append(Check(WARN, f"{st.scope} hooks",
                             f"{st.path}: AgentLens entries are duplicated or edited ({what})",
                             f"agentlens install --repair{_scope_flag(st.scope)} --dry-run   # then without --dry-run"))
        else:
            out.append(Check(OK, f"{st.scope} hooks", f"{st.path}: all {len(installer.HOOK_EVENTS)} events"))
    return out


def check_recording(states: list[ScopeState], plugin_on: bool) -> list[Check]:
    with_hooks = [st.scope for st in states if st.status.events]
    out = []
    if not plugin_on and not with_hooks:
        out.append(Check(FAIL, "recording", "neither the plugin nor settings hooks are installed",
                         "/plugin install agentlens@agentlens in Claude Code, or `agentlens install`"))
    elif plugin_on and with_hooks:
        out.append(Check(WARN, "recording",
                         f"plugin and settings hooks ({', '.join(with_hooks)}) are both active: every event fires twice",
                         " && ".join(f"agentlens uninstall{_scope_flag(s)}" for s in with_hooks)))
    else:
        out.append(Check(OK, "recording", "via plugin" if plugin_on else f"via settings hooks ({', '.join(with_hooks)})"))
    commands = {c for st in states for c in st.status.commands}
    if len(commands) > 1:
        out.append(Check(WARN, "hook commands",
                         f"{len(commands)} different AgentLens hook commands across scopes; each one runs",
                         "keep one scope: `agentlens uninstall --scope <scope>` for the others"))
    # disableAllHooks: Claude Code uses the value left after precedence (local > project > user).
    effective = None
    for st in states:
        value = _read_json(st.path).get("disableAllHooks")
        if isinstance(value, bool):
            effective = (value, st)
    if effective and effective[0]:
        st = effective[1]
        out.append(Check(FAIL, "hooks enabled", f"disableAllHooks is true (from {st.scope} settings): no hook runs",
                         f"remove disableAllHooks from {st.path}"))
    return out


def smoke_test(command: str) -> Check:
    """Run one configured hook command with a synthetic event into a temporary AGENTLENS_HOME."""
    py = interpreter_of(command)
    if not py or not (os.path.isfile(py) and os.access(py, os.X_OK)):
        return Check(FAIL, "hook test", f"interpreter {py} does not exist; every hook fails silently",
                     "agentlens install --repair (from the environment AgentLens is installed in)")
    with tempfile.TemporaryDirectory(prefix="agentlens-doctor-") as tmp:
        env = dict(os.environ, AGENTLENS_HOME=os.path.join(tmp, "home"))
        payload = json.dumps({"session_id": "agentlens-doctor", "hook_event_name": "Stop", "cwd": tmp})
        try:
            r = subprocess.run(["sh", "-c", command], input=payload, env=env, capture_output=True,
                               text=True, timeout=SMOKE_TIMEOUT_S, cwd=tmp)
        except subprocess.TimeoutExpired:
            return Check(FAIL, "hook test", f"{py}: the hook took longer than {SMOKE_TIMEOUT_S}s")
        spooled = list(Path(env["AGENTLENS_HOME"], "spool").glob("*.json"))
        if r.returncode == 0 and len(spooled) == 1:
            return Check(OK, "hook test", f"{py}: a test event was recorded (temporary data dir)")
        why = (r.stderr.strip().splitlines() or [f"exit {r.returncode}, nothing spooled"])[-1][:160]
        return Check(FAIL, "hook test", f"{py}: {why}",
                     f"check that `{py} -m agentlens --version` works; reinstall with `agentlens install --repair`")


def check_statusline() -> list[Check]:
    """Only when the AgentLens status line is installed (it is opt-in)."""
    command = installer.statusline_state()
    if command is None:
        return []
    py = interpreter_of(command)
    if not py or not (os.path.isfile(py) and os.access(py, os.X_OK)):
        return [Check(FAIL, "status line", f"interpreter {py} does not exist; your status bar shows nothing",
                      "agentlens statusline install (from the environment AgentLens is installed in)")]
    sample = {"session_id": "agentlens-doctor", "rate_limits": {
        "five_hour": {"used_percentage": 12.5, "resets_at": time.time() + 3600}}}
    with tempfile.TemporaryDirectory(prefix="agentlens-doctor-") as tmp:
        env = dict(os.environ, AGENTLENS_HOME=os.path.join(tmp, "home"))
        try:
            r = subprocess.run(["sh", "-c", command], input=json.dumps(sample), env=env, capture_output=True,
                               text=True, timeout=SMOKE_TIMEOUT_S, cwd=tmp)
        except subprocess.TimeoutExpired:
            return [Check(FAIL, "status line", f"took longer than {SMOKE_TIMEOUT_S}s")]
        recorded = Path(env["AGENTLENS_HOME"], "plan_usage.json").exists()
    if r.returncode == 0 and "5h 12%" in r.stdout and recorded:
        return [Check(OK, "status line", "agentlens statusline prints plan usage (tested with a sample)")]
    return [Check(FAIL, "status line", f"test run failed: {(r.stderr.strip() or r.stdout.strip())[:160]}",
                  "agentlens statusline install")]


def check_spool(now: float) -> list[Check]:
    out = []
    failed = spool.failed_count()
    if failed:
        out.append(Check(WARN, "spool", f"{failed} file(s) could not be ingested ({paths.failed_dir()})",
                         "inspect them, then delete the folder; they are never retried automatically"))
    stale = [p for p in spool.pending() if now - p.stat().st_mtime > SPOOL_STALE_S]
    if stale:
        out.append(Check(WARN, "spool", f"{len(stale)} event file(s) waiting longer than an hour", "agentlens ingest"))
    if not out:
        out.append(Check(OK, "spool", f"{len(spool.pending())} pending, none stuck"))
    return out


def check_database(now: float, expect_events: bool) -> list[Check]:
    db = paths.db_path()
    if not db.exists():
        return [Check(OK if not expect_events else WARN, "database",
                      "not created yet (no events recorded)",
                      "" if not expect_events else "start a Claude Code session, then run `agentlens status`")]
    from agentlens import store

    try:
        conn = store.connect()
    except Exception as exc:  # noqa: BLE001 - report, don't crash
        return [Check(FAIL, "database", f"{db}: {exc}",
                      "upgrade AgentLens if the schema is newer; otherwise move the file aside")]
    try:
        result = conn.execute("PRAGMA quick_check").fetchone()[0]
        last = store.totals(conn).last_event_ts
        out = [Check(OK if result == "ok" else FAIL, "database",
                     f"{db} (schema v{store.schema_version(conn)}, integrity {result})",
                     "" if result == "ok" else "move the database aside; AgentLens creates a new one")]
    finally:
        conn.close()
    if expect_events and last and now - last > QUIET_DAYS * 86400:
        out.append(Check(WARN, "last event", f"{(now - last) / 86400:.0f} days ago although hooks are installed",
                         "run `agentlens doctor` inside a project you use, or check the hook test above"))
    return out


def check_log(now: float) -> Check:
    log = paths.logs_dir() / "agentlens.log"
    recent: list[str] = []
    try:
        lines = log.read_text(errors="replace").splitlines()
    except OSError:
        lines = []
    for line in lines:
        m = _LOG_HEAD.match(line)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S%z").timestamp()
        except ValueError:
            continue
        if now - ts <= 86400:
            recent.append(m.group(2))
    if recent:
        return Check(WARN, "error log", f"{len(recent)} error(s) in the last 24h; last: {recent[-1][:120]}",
                     f"see {log}")
    return Check(OK, "error log", "no errors in the last 24h")


# ------------------------------------------------------------------ run


def run(project_dir: Path | None = None, smoke: bool = True) -> list[Check]:
    project_dir = project_dir or Path.cwd()
    now = time.time()
    states = scope_states(project_dir)
    plugin_on = plugin.plugin_enabled(paths.claude_settings_path(), project_dir)
    checks = [check_python(), check_home()]
    checks += check_recording(states, plugin_on)
    checks += check_settings(states)
    if smoke:
        for command in sorted({c for st in states for c in st.status.commands}):
            checks.append(smoke_test(command))
        checks += check_statusline()
    checks += check_spool(now)
    checks += check_database(now, expect_events=plugin_on or any(st.status.events for st in states))
    checks.append(check_log(now))
    return checks


def render(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks)
    out = ["AgentLens doctor"]
    for c in checks:
        out.append(f"  {c.level:<4}  {c.name:<{width}}  {c.detail}")
        if c.fix:
            out.append(f"  {'':<4}  {'':<{width}}  fix: {c.fix}")
    n = {lvl: sum(1 for c in checks if c.level == lvl) for lvl in (OK, WARN, FAIL)}
    out.append(f"\n{n[OK]} ok, {n[WARN]} warning(s), {n[FAIL]} failure(s). Doctor changes nothing.")
    return "\n".join(out)

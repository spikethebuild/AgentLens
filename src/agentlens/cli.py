"""``agentlens`` command line interface (v0.1)."""

from __future__ import annotations

import argparse
import os
import shlex
import sys
import time
from pathlib import Path

from agentlens import __version__, installer, paths, plugin, render, spool, store
from agentlens.ingest import ingest

READ_LOCK_WAIT_S = 2.0


def _sync(quiet: bool = False) -> None:
    """Drain the spool before any read so the output is current."""
    try:
        r = ingest(wait_s=READ_LOCK_WAIT_S)
    except Exception as exc:  # noqa: BLE001 - a read must still work on a stale DB
        from agentlens.errlog import log_error

        log_error("ingest before read failed", exc)
        print(f"warning: could not ingest the spool ({exc}); showing stored data", file=sys.stderr)
        return
    if r.locked and not quiet:
        print("note: another ingest is running; results may be a few events behind", file=sys.stderr)


def _settings_arg(args) -> Path | None:
    return Path(args.settings).expanduser() if getattr(args, "settings", None) else None


# ----------------------------------------------------------------- commands


def _plugin_on(args) -> bool:
    settings = _settings_arg(args) or paths.claude_settings_path()
    return plugin.plugin_enabled(settings, Path.cwd())


def cmd_install(args) -> int:
    if _plugin_on(args) and not args.force:
        print(
            "error: the AgentLens Claude Code plugin is enabled, and it already records every session.\n"
            "Installing settings hooks too would make every event fire twice.\n"
            "Use the plugin, or disable it first. Pass --force to install anyway.",
            file=sys.stderr,
        )
        return 1
    try:
        change = installer.install(_settings_arg(args), python=args.python)
    except installer.SettingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    path = render.short_path(str(change.settings_path), 80)
    if not change.changed:
        print(f"AgentLens hooks already installed in {path} ({len(change.unchanged)} events). Nothing changed.")
        return 0
    if change.added:
        print(f"Added AgentLens hooks for {len(change.added)} events to {path}: {', '.join(change.added)}")
    if change.updated:
        print(f"Updated the interpreter path for {len(change.updated)} events.")
    if change.backup:
        print(f"Backup of the previous file: {render.short_path(str(change.backup), 100)}")
    print("Hooks run async and never block Claude Code. New sessions are recorded automatically.")
    return 0


def cmd_uninstall(args) -> int:
    try:
        change = installer.uninstall(_settings_arg(args))
    except installer.SettingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    path = render.short_path(str(change.settings_path), 80)
    if not change.changed:
        print(f"No AgentLens hooks found in {path}. Nothing changed.")
        return 0
    print(f"Removed AgentLens hooks for {len(change.removed)} events from {path}.")
    if change.backup:
        print(f"Backup of the previous file: {render.short_path(str(change.backup), 100)}")
    print(f"Recorded data was kept in {render.short_path(str(paths.home()), 80)}.")
    return 0


def _interpreter_of(command: str) -> str | None:
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    return parts[0] if parts else None


DUPLICATE_WARNING = (
    "AgentLens is installed twice (plugin and settings hooks), so events fire twice. "
    "Run `agentlens uninstall` to keep only the plugin."
)


def cmd_status(args) -> int:
    _sync(quiet=True)
    st = installer.status(_settings_arg(args))
    plugin_on = _plugin_on(args)
    warnings: list[str] = []
    if plugin_on and st.events:
        warnings.append(DUPLICATE_WARNING)
    if args.brief:
        return _status_brief(st, plugin_on, warnings)
    n_events = len(installer.HOOK_EVENTS)

    if not st.valid:
        hooks_line = "settings file is not valid JSON; cannot check"
        warnings.append(st.error or "invalid settings")
    elif not st.events:
        hooks_line = "not installed (run `agentlens install`)"
    else:
        hooks_line = f"installed for {len(st.events)}/{n_events} events"
        if st.missing:
            warnings.append(f"missing hooks for: {', '.join(st.missing)} (run `agentlens install`)")
    interp_lines = []
    for cmd in sorted(st.commands):
        py = _interpreter_of(cmd)
        ok = bool(py) and os.path.isfile(py) and os.access(py, os.X_OK)
        interp_lines.append(f"{py}  {'ok' if ok else 'MISSING'}")
        if not ok:
            warnings.append(f"hook interpreter {py} does not exist; hooks will fail. Re-run `agentlens install`.")
    if st.disable_all_hooks:
        warnings.append("disableAllHooks is set in your Claude Code settings: no hooks run, so nothing is recorded")

    db = paths.db_path()
    rows = [
        ("version", __version__, ""),
        ("home", render.short_path(str(paths.home()), 80) + ("  (AGENTLENS_HOME)" if os.environ.get("AGENTLENS_HOME") else ""), ""),
        ("settings", render.short_path(str(st.settings_path), 80), ""),
        ("plugin", "enabled (records sessions)" if plugin_on else "not enabled", ""),
        ("settings hooks", hooks_line, ""),
    ]
    for line in interp_lines:
        rows.append(("interpreter", line, ""))
    rows.append(("spool", f"{len(spool.pending())} pending, {spool.failed_count()} failed", render.CALC))

    if db.exists():
        conn = store.connect()
        try:
            t = store.totals(conn)
            size = sum(p.stat().st_size for p in db.parent.glob(db.name + "*") if p.is_file())
            rows += [
                ("database", f"{render.short_path(str(db), 80)} (schema v{store.schema_version(conn)}, {size / 1024:.0f} KB)", ""),
                ("sessions", str(t.sessions), render.CALC),
                ("events", str(t.events), render.CALC),
                ("tool calls", str(t.tool_calls), render.CALC),
                ("last event", render.fmt_ts(t.last_event_ts), render.CLK if t.last_event_ts else ""),
            ]
        finally:
            conn.close()
    else:
        rows.append(("database", "not created yet (no events recorded)", ""))

    if spool.failed_count():
        warnings.append(f"{spool.failed_count()} spool file(s) could not be ingested; see {render.short_path(str(paths.failed_dir()), 80)}")
    log = paths.logs_dir() / "agentlens.log"
    if log.exists() and log.stat().st_size:
        rows.append(("error log", render.short_path(str(log), 80), ""))

    print("AgentLens status")
    print(render.kv(rows))
    for w in warnings:
        print(f"warning: {w}")
    return 0



def _last_log_error() -> str | None:
    log = paths.logs_dir() / "agentlens.log"
    try:
        with open(log, "rb") as fh:
            fh.seek(max(0, log.stat().st_size - 4096))
            lines = [ln for ln in fh.read().decode("utf-8", "replace").splitlines() if " ERROR " in ln]
    except OSError:
        return None
    return lines[-1][:160] if lines else None


def _status_brief(st, plugin_on: bool, warnings: list[str]) -> int:
    """At most four short lines: this output enters the model's context via the skill."""
    if plugin_on:
        via = "recording via plugin"
    elif st.events:
        via = "recording via settings hooks"
    else:
        via = "NOT recording (enable the plugin or run `agentlens install`)"
    line = f"AgentLens {__version__} · {via}"
    db = paths.db_path()
    if db.exists():
        conn = store.connect()
        try:
            t = store.totals(conn)
            # evidence-based: both installs actually delivered events in the last day
            seen = store.collectors_seen(conn, time.time() - 86400)
            if seen >= {"plugin", "settings"} and DUPLICATE_WARNING not in warnings:
                warnings.append(DUPLICATE_WARNING)
        finally:
            conn.close()
        line += f" · {t.sessions} sessions · last event {render.fmt_ts(t.last_event_ts, short=True)}"
    print(line)
    if spool.failed_count():
        warnings.append(f"{spool.failed_count()} spool file(s) failed to ingest")
    err = _last_log_error()
    if err:
        warnings.append(f"last error: {err}")
    for w in warnings[:3]:
        print(f"! {w}")
    return 0


def cmd_sessions(args) -> int:
    _sync()
    conn = store.connect()
    try:
        print(render.render_sessions(store.list_sessions(conn, args.limit)))
    finally:
        conn.close()
    return 0


def cmd_inspect(args) -> int:
    _sync()
    conn = store.connect()
    try:
        try:
            s = store.find_session(conn, args.session_id)
        except store.AmbiguousSession as exc:
            print(f"error: '{args.session_id}' matches several sessions:", file=sys.stderr)
            for sid in exc.args[0]:
                print(f"  {sid}", file=sys.stderr)
            return 2
        if s is None:
            print(f"error: no session matching '{args.session_id}'. Try `agentlens sessions`.", file=sys.stderr)
            return 1
        calls = store.session_tool_calls(conn, s["provider"], s["session_id"])
        events = store.session_events(conn, s["provider"], s["session_id"])
        print(render.render_inspect(s, calls, events, None if args.all else args.limit))
    finally:
        conn.close()
    return 0


def cmd_ingest(args) -> int:
    r = ingest(wait_s=0.0 if args.quiet else READ_LOCK_WAIT_S)
    if not args.quiet:
        if r.locked:
            print("Another ingest is running; nothing to do.")
        else:
            print(f"Ingested {r.files} spool file(s): {r.inserted} new event(s), "
                  f"{r.duplicates} duplicate(s), {r.failed} failed.")
    return 0


def run_dashboard(parser: argparse.ArgumentParser | None = None) -> int:
    """Open the TUI, or explain how to get it when the optional extra is missing."""
    import importlib

    try:
        tui = importlib.import_module("agentlens.tui")
    except ImportError:
        print(
            "The dashboard needs the optional TUI dependencies:\n"
            "  pip install 'agentlens[tui]'\n"
            "The plain commands below work without them.\n",
            file=sys.stderr,
        )
        (parser or build_parser()).print_help()
        return 1
    return tui.run()


def cmd_dashboard(args) -> int:
    return run_dashboard()


def cmd_hook(args) -> int:
    from agentlens.hook import run

    return run([args.provider])


# ------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agentlens",
        description="Local-first observability for AI coding agents. "
                    "Maximum visibility. Minimum token overhead.",
    )
    p.add_argument("--version", action="version", version=f"agentlens {__version__}")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    s = sub.add_parser("install", help="add AgentLens hooks to Claude Code user settings (fallback to the plugin)")
    s.add_argument("--settings", help="settings.json to edit (default: ~/.claude/settings.json)")
    s.add_argument("--force", action="store_true", help="install even if the AgentLens plugin is enabled")
    s.add_argument("--python", help="interpreter the hook should use (default: this one)")
    s.set_defaults(func=cmd_install)

    s = sub.add_parser("uninstall", help="remove AgentLens hooks (recorded data is kept)")
    s.add_argument("--settings", help="settings.json to edit")
    s.set_defaults(func=cmd_uninstall)

    s = sub.add_parser("status", help="show installation health and storage")
    s.add_argument("--settings", help="settings.json to check")
    s.add_argument("--brief", action="store_true", help="at most four lines (used by the plugin skill)")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("sessions", help="list recorded sessions")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_sessions)

    s = sub.add_parser("inspect", help="show one session in detail")
    s.add_argument("session_id", help="full session ID or a unique prefix")
    s.add_argument("--limit", type=int, default=60, help="timeline events to show (default 60)")
    s.add_argument("--all", action="store_true", help="show the full timeline")
    s.set_defaults(func=cmd_inspect)

    s = sub.add_parser("dashboard", help="open the terminal dashboard (same as running `agentlens` alone)")
    s.set_defaults(func=cmd_dashboard)

    s = sub.add_parser("ingest", help="move spooled events into the database")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser("hook", help=argparse.SUPPRESS)
    s.add_argument("provider")
    s.set_defaults(func=cmd_hook)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["hook"]:  # fast path, even via the console script
        from agentlens.hook import run

        return run(argv[1:])
    parser = build_parser()
    if not argv and sys.stdin.isatty() and sys.stdout.isatty():
        return run_dashboard(parser)
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)

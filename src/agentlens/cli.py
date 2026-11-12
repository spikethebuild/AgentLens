"""``agentlens`` command line interface (v0.1)."""

from __future__ import annotations

import argparse
import os
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
    if getattr(args, "settings", None):
        return Path(args.settings).expanduser()
    scope = getattr(args, "scope", None)
    if scope:
        return paths.scope_settings_path(scope, Path.cwd())
    return None


def _git_ignored(path: Path) -> bool | None:
    """True/False from `git check-ignore`; None outside a git repo or without git."""
    import subprocess

    try:
        r = subprocess.run(["git", "-C", str(Path.cwd()), "check-ignore", "-q", str(path)],
                           capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return {0: True, 1: False}.get(r.returncode)


def _scope_notes(args, settings_path: Path) -> list[str]:
    scope = getattr(args, "scope", None)
    notes = []
    if scope == "project":
        notes.append(
            "note: project settings are usually committed, and this hook names this machine's Python "
            "interpreter. Teammates need the same path. Prefer --scope local, or the plugin."
        )
    if scope == "local" and _git_ignored(settings_path) is False:
        notes.append(
            f"note: {settings_path} is not ignored by git. Add `.claude/settings.local.json` to "
            ".gitignore so it is not committed."
        )
    return notes


def _print_change_header(change, verb: str) -> None:
    path = render.short_path(str(change.settings_path), 80)
    if change.dry_run:
        print(f"Dry run: {verb} {path} would change as below. Nothing was written.")
        print(change.diff, end="" if change.diff.endswith("\n") else "\n")


# ----------------------------------------------------------------- commands


def _plugin_on(args) -> bool:
    user = Path(args.settings).expanduser() if getattr(args, "settings", None) else paths.claude_settings_path()
    return plugin.plugin_enabled(user, Path.cwd())


def cmd_install(args) -> int:
    if _plugin_on(args) and not args.force:
        print(
            "error: the AgentLens Claude Code plugin is enabled, and it already records every session.\n"
            "Installing settings hooks too would make every event fire twice.\n"
            "Use the plugin, or disable it first. Pass --force to install anyway.",
            file=sys.stderr,
        )
        return 1
    settings = _settings_arg(args)
    try:
        change = installer.install(settings, python=args.python, repair=args.repair, dry_run=args.dry_run)
    except installer.SettingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    path = render.short_path(str(change.settings_path), 80)
    if not change.changed:
        print(f"AgentLens hooks already installed in {path} ({len(change.unchanged)} events). Nothing changed.")
        return 0
    _print_change_header(change, "install into")
    would = "Would add" if change.dry_run else "Added"
    if change.added:
        print(f"{would} AgentLens hooks for {len(change.added)} events to {path}: {', '.join(change.added)}")
    if change.updated:
        print(f"{'Would update' if change.dry_run else 'Updated'} the interpreter path for {len(change.updated)} events.")
    if change.repaired:
        print(f"{'Would repair' if change.dry_run else 'Repaired'} AgentLens entries for {len(change.repaired)} "
              f"events: {', '.join(change.repaired)}")
    if change.removed:
        print(f"{'Would remove' if change.dry_run else 'Removed'} AgentLens hooks from events it no longer uses: "
              f"{', '.join(change.removed)}")
    if change.backup:
        print(f"Backup of the previous file: {render.short_path(str(change.backup), 100)}")
    for note in _scope_notes(args, change.settings_path):
        print(note)
    if not change.dry_run:
        print("Hooks run async and never block Claude Code. Claude Code picks up the change without a restart.")
    return 0


def cmd_uninstall(args) -> int:
    try:
        change = installer.uninstall(_settings_arg(args), dry_run=args.dry_run)
    except installer.SettingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    path = render.short_path(str(change.settings_path), 80)
    if not change.changed:
        print(f"No AgentLens hooks found in {path}. Nothing changed.")
        return 0
    _print_change_header(change, "uninstall from")
    print(f"{'Would remove' if change.dry_run else 'Removed'} AgentLens hooks for {len(change.removed)} events from {path}.")
    if change.backup:
        print(f"Backup of the previous file: {render.short_path(str(change.backup), 100)}")
    if not change.dry_run:
        print(f"Recorded data was kept in {render.short_path(str(paths.home()), 80)}.")
    return 0


def _interpreter_of(command: str) -> str | None:
    from agentlens.doctor import interpreter_of

    return interpreter_of(command)


DUPLICATE_WARNING = (
    "AgentLens is installed twice (plugin and settings hooks), so events fire twice. "
    "Run `agentlens uninstall` to keep only the plugin."
)


def cmd_status(args) -> int:
    _sync(quiet=True)
    st = installer.status(_settings_arg(args))
    plugin_on = _plugin_on(args)
    warnings: list[str] = []
    # Without --settings/--scope, also report hooks found in this project's settings files.
    others = []
    if not getattr(args, "settings", None) and not getattr(args, "scope", None):
        for scope in ("project", "local"):
            o = installer.status(paths.scope_settings_path(scope, Path.cwd()))
            if o.events or not o.valid:
                others.append((scope, o))
    if plugin_on and (st.events or any(o.events for _, o in others)):
        warnings.append(DUPLICATE_WARNING)
    if args.brief:
        return _status_brief(st, plugin_on, warnings, others)
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
    other_rows = []
    for scope, o in others:
        if not o.valid:
            other_rows.append((f"{scope} hooks", "settings file is not valid JSON; cannot check", ""))
            warnings.append(o.error or f"invalid {scope} settings")
            continue
        other_rows.append((f"{scope} hooks", f"installed for {len(o.events)}/{n_events} events "
                           f"({render.short_path(str(o.settings_path), 60)})", ""))
        if o.missing:
            warnings.append(f"{scope} settings miss hooks for: {', '.join(o.missing)} "
                            f"(run `agentlens install --scope {scope}`)")
    interp_lines = []
    for cmd in sorted(st.commands | {c for _, o in others for c in o.commands}):
        py = _interpreter_of(cmd)
        ok = bool(py) and os.path.isfile(py) and os.access(py, os.X_OK)
        interp_lines.append(f"{py}  {'ok' if ok else 'MISSING'}")
        if not ok:
            warnings.append(f"hook interpreter {py} does not exist; hooks will fail. Re-run `agentlens install`.")
    if st.disable_all_hooks or any(o.disable_all_hooks for _, o in others):
        warnings.append("disableAllHooks is set in your Claude Code settings: no hooks run, so nothing is recorded")

    db = paths.db_path()
    rows = [
        ("version", __version__, ""),
        ("home", render.short_path(str(paths.home()), 80) + ("  (AGENTLENS_HOME)" if os.environ.get("AGENTLENS_HOME") else ""), ""),
        ("settings", render.short_path(str(st.settings_path), 80), ""),
        ("plugin", "enabled (records sessions)" if plugin_on else "not enabled", ""),
        ("settings hooks", hooks_line, ""),
        *other_rows,
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


def _status_brief(st, plugin_on: bool, warnings: list[str], others=()) -> int:
    """At most four short lines: this output enters the model's context via the skill."""
    if plugin_on:
        via = "recording via plugin"
    elif st.events or others:
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


def cmd_doctor(args) -> int:
    from agentlens import doctor

    checks = doctor.run(Path.cwd(), smoke=not args.no_hook_test)
    print(doctor.render(checks))
    return 1 if any(c.level == doctor.FAIL for c in checks) else 0


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
        usage, reason = _session_usage(conn, s)
        print(render.render_inspect(s, calls, events, None if args.all else args.limit, usage, reason))
    finally:
        conn.close()
    return 0


def _session_usage(conn, s) -> tuple[dict, str | None]:
    from agentlens import cache

    usage = store.session_usage(conn, s["provider"], s["session_id"])
    reason = None if usage["requests"] else cache.usage_reason(conn, s["provider"], s["session_id"])
    return usage, reason


def _pick_session(conn, prefix: str | None):
    """A session by ID prefix, else the most recent one in this directory, else the most recent."""
    if prefix:
        return store.find_session(conn, prefix)
    cwd = os.path.realpath(os.getcwd())
    sessions = store.list_sessions(conn, 200)
    here = [s for s in sessions if s["project"] and os.path.realpath(s["project"]) == cwd]
    return (here or sessions or [None])[0]


def cmd_cache(args) -> int:
    from agentlens import cache

    _sync(quiet=args.brief)
    conn = store.connect()
    try:
        try:
            s = _pick_session(conn, args.session_id)
        except store.AmbiguousSession as exc:
            print(f"error: '{args.session_id}' matches several sessions: {', '.join(x[:8] for x in exc.args[0])}",
                  file=sys.stderr)
            return 2
        if s is None:
            msg = f"no session matching '{args.session_id}'" if args.session_id else "no sessions recorded yet"
            print(f"AgentLens cache: {msg}." if args.brief else f"error: {msg}.", file=sys.stdout if args.brief else sys.stderr)
            return 0 if args.brief else 1
        rep = cache.analyze(conn, s)
        print(cache.render_brief(rep) if args.brief else cache.render_report(rep, args.requests))
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

    def where(sp, verb: str) -> None:
        g = sp.add_mutually_exclusive_group()
        g.add_argument("--scope", choices=paths.SCOPES,
                       help=f"settings scope to {verb}: user (default, ~/.claude/settings.json), "
                            "project (.claude/settings.json) or local (.claude/settings.local.json)")
        g.add_argument("--settings", help=f"settings.json to {verb} (instead of --scope)")

    s = sub.add_parser("install", help="add AgentLens hooks to Claude Code settings (fallback to the plugin)")
    where(s, "edit")
    s.add_argument("--dry-run", action="store_true", help="show the change as a diff; write nothing")
    s.add_argument("--repair", action="store_true",
                   help="also normalize AgentLens's own entries (duplicates, edited fields, retired events)")
    s.add_argument("--force", action="store_true", help="install even if the AgentLens plugin is enabled")
    s.add_argument("--python", help="interpreter the hook should use (default: this one)")
    s.set_defaults(func=cmd_install)

    s = sub.add_parser("uninstall", help="remove AgentLens hooks (recorded data is kept)")
    where(s, "edit")
    s.add_argument("--dry-run", action="store_true", help="show the change as a diff; write nothing")
    s.set_defaults(func=cmd_uninstall)

    s = sub.add_parser("status", help="show installation health and storage")
    where(s, "check")
    s.add_argument("--brief", action="store_true", help="at most four lines (used by the plugin skill)")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("doctor", help="check the installation end to end and print a fix for each problem")
    s.add_argument("--no-hook-test", action="store_true",
                   help="skip running the configured hook command with a test event")
    s.set_defaults(func=cmd_doctor)

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

    s = sub.add_parser("cache", help="where prompt cache was rebuilt in a session, why, and the fix")
    s.add_argument("session_id", nargs="?", help="session ID or prefix (default: latest in this directory)")
    s.add_argument("--brief", action="store_true", help="at most five lines (used by the plugin skill)")
    s.add_argument("--requests", action="store_true", help="also list every API request")
    s.set_defaults(func=cmd_cache)

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

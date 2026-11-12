"""Terminal dashboard (``agentlens`` with no arguments). Needs the ``tui`` extra.

Read-only with respect to the user's configuration: it never touches Claude
Code settings and never makes network calls. Its only write is the normal
spool -> SQLite ingest, run under the existing ingest lock.

All text goes through ``rich.text.Text`` so provenance tags such as ``[obs]``
are shown literally instead of being parsed as console markup.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from typing import Iterable, Sequence

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    DataTable,
    Footer,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
    TabbedContent,
    TabPane,
)

from agentlens import render, store
from agentlens.ingest import ingest

HISTORY_LIMIT = 1000  # sessions loaded into the explorer
LIVE_INTERVAL_S = 1.0

GREEN, AMBER, RED = "#8fd18f", "#d7a54a", "#e06c6c"
TEXT, MUTED, DIM = "#c8d0c8", "#7c857c", "#555c55"
TAG_STYLE = {render.OBS: "#6fb3d2", render.CALC: "#a990d6", render.EST: AMBER, render.CLK: MUTED}

CSS = f"""
Screen {{ background: #0e1011; color: {TEXT}; }}
#titlebar {{ height: 1; background: #16191b; color: {GREEN}; padding: 0 1; text-style: bold; }}
#projects {{ width: 36; background: #111415; border-right: solid #2a2f31; }}
#projects > ListItem {{ background: #111415; padding: 0 1; }}
#projects > ListItem.-highlight {{ background: #1f3a26; }}
#filter {{ background: #111415; border: tall #2a2f31; color: {TEXT}; }}
#filter:focus {{ border: tall {GREEN}; }}
DataTable {{ background: #0e1011; color: {TEXT}; }}
DataTable > .datatable--header {{ background: #16191b; color: {MUTED}; text-style: bold; }}
DataTable > .datatable--cursor {{ background: #1f3a26; color: #eef3ee; }}
DataTable:focus > .datatable--cursor {{ background: #2a5536; }}
#legend {{ height: auto; color: {DIM}; padding: 0 1; }}
.split-top {{ height: 1fr; max-height: 12; }}
.detail {{ height: 2fr; border-top: solid #2a2f31; padding: 0 1; background: #111415; }}
#timeline {{ height: 1fr; }}
TabbedContent {{ height: 1fr; }}
TabPane {{ padding: 0; }}
"""


# --------------------------------------------------------------- formatting


def tagged(value: str, tag: str = "", style: str | None = None) -> Text:
    """A cell value followed by its provenance tag, both rendered literally."""
    t = Text()
    if value.startswith(render.UNAVAILABLE):
        t.append(value, style=f"italic {AMBER}")
    else:
        t.append(value, style=style or "")
    if tag:
        t.append(" " + tag, style=TAG_STYLE.get(tag, MUTED))
    return t


def kv_text(rows: Sequence[tuple[str, str, str]]) -> Text:
    width = max((len(k) for k, _, _ in rows), default=0)
    out = Text()
    for i, (k, v, tag) in enumerate(rows):
        if i:
            out.append("\n")
        out.append(k.ljust(width) + "  ", style=MUTED)
        out.append_text(tagged(v.replace("\n", "\n" + " " * (width + 2)), tag))
    return out


def status_style(status: str) -> str:
    if status.startswith(("failed", "interrupted")):
        return f"bold {RED}"
    if status.startswith("denied"):
        return AMBER
    if status == "ok":
        return GREEN
    return MUTED


def state_text(s: sqlite3.Row) -> Text:
    state = render.session_state(s)
    if state == "open":
        return Text("● open", style=GREEN)
    return Text(state, style=MUTED)


def session_key(provider: str, session_id: str) -> str:
    return f"{provider}\x1f{session_id}"


def legend() -> Text:
    return Text(render.LEGEND, style=DIM)


def _set_rows(table: DataTable, rows: list[tuple[str, list]]) -> None:
    """Replace a table's rows, updating in place when the keys are unchanged
    and keeping the cursor on the same key when they are not."""
    keys = [k for k, _ in rows]
    current = [k.value for k in table.rows]
    if keys == current:
        cols = list(table.columns)
        for key, cells in rows:
            for col, cell in zip(cols, cells):
                table.update_cell(key, col, cell)
        return
    cursor_key = current[table.cursor_row] if current and 0 <= table.cursor_row < len(current) else None
    table.clear()
    for key, cells in rows:
        table.add_row(*cells, key=key)
    if cursor_key in keys:
        table.move_cursor(row=keys.index(cursor_key))


# -------------------------------------------------------------- sessions


class SessionsScreen(Screen):
    """Session explorer: projects on the left, filterable session history."""

    BINDINGS = [
        Binding("slash", "focus_filter", "Filter"),
        Binding("f", "toggle_failed", "Failures only"),
        Binding("escape", "clear_filter", "Clear filter", show=False),
    ]

    COLUMNS = [
        "SESSION", "STARTED [clk]", "DURATION [calc]", "STATE", "FAILED [calc]",
        "TOOLS [calc]", "PROMPTS [calc]", "MODEL [obs]", "PROJECT",
    ]

    def __init__(self) -> None:
        super().__init__()
        self.sessions: list[sqlite3.Row] = []
        self.project: str | None = None  # None = all projects
        self.failed_only = False
        self.query_text = ""
        self._project_keys: list[str | None] = []

    def compose(self) -> ComposeResult:
        yield Static(id="titlebar")
        with Horizontal():
            yield ListView(id="projects")
            with Vertical():
                yield Input(placeholder="filter by id, project, model or state   ( / )", id="filter")
                yield DataTable(id="sessions", cursor_type="row")
                yield Static(legend(), id="legend")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#sessions", DataTable)
        for label in self.COLUMNS:
            table.add_column(Text(label), key=label)
        self.refresh_data()
        table.focus()

    def on_screen_resume(self) -> None:
        self.refresh_data()

    # data ------------------------------------------------------------

    def refresh_data(self) -> None:
        self.sessions = store.list_sessions(self.app.conn, HISTORY_LIMIT)
        self._refresh_projects()
        self._refresh_table()
        self._refresh_title()

    def _visible(self) -> list[sqlite3.Row]:
        words = self.query_text.lower().split()
        out = []
        for s in self.sessions:
            if self.project is not None and (s["project"] or "") != self.project:
                continue
            if self.failed_only and not s["tool_failures"]:
                continue
            if words:
                hay = " ".join(
                    str(x or "") for x in (
                        s["session_id"], s["project"], s["model"], s["provider"],
                        render.session_state(s),
                    )
                ).lower()
                if not all(w in hay for w in words):
                    continue
            out.append(s)
        return out

    def _refresh_table(self) -> None:
        rows = []
        for s in self._visible():
            dur, _ = render.session_duration(s)
            failed = s["tool_failures"]
            rows.append((session_key(s["provider"], s["session_id"]), [
                Text(s["session_id"][:8], style="bold"),
                tagged(render.fmt_ts(s["started_at"], short=True) if s["started_at"]
                       else f"{render.UNAVAILABLE}"),
                tagged(dur),
                state_text(s),
                Text(str(failed), style=f"bold {RED}" if failed else DIM),
                Text(str(s["tool_calls"])),
                Text(str(s["prompts"])),
                tagged(s["model"] or render.UNAVAILABLE, style=MUTED),
                Text(render.short_path(s["project"], 40), style=MUTED),
            ]))
        _set_rows(self.query_one("#sessions", DataTable), rows)

    def _refresh_projects(self) -> None:
        counts: dict[str, list[int]] = {}
        for s in self.sessions:
            c = counts.setdefault(s["project"] or "", [0, 0])
            c[0] += 1
            c[1] += 1 if s["tool_failures"] else 0
        keys: list[str | None] = [None, *sorted(counts)]
        labels = [Text.assemble(("All projects", "bold"), (f"  {len(self.sessions)}", MUTED))]
        for p in keys[1:]:
            n, nf = counts[p]
            t = Text(render.short_path(p, 24) if p else "(no project)")
            t.append(f"  {n}", style=MUTED)
            if nf:
                t.append(f"  {nf}✗", style=RED)
            labels.append(t)
        lv = self.query_one("#projects", ListView)
        if keys == self._project_keys and len(lv.children) == len(labels):
            for item, label in zip(lv.children, labels):
                item.query_one(Label).update(label)
            return
        self._project_keys = keys
        if self.project not in keys:
            self.project = None
        lv.clear()
        lv.extend(ListItem(Label(label)) for label in labels)
        lv.index = keys.index(self.project)

    def _refresh_title(self) -> None:
        t = Text("AgentLens", style=f"bold {GREEN}")
        t.append("  ·  sessions", style=MUTED)
        shown = len(self.query_one("#sessions", DataTable).rows)
        t.append(f"  ·  {shown} of {len(self.sessions)} shown", style=MUTED)
        if self.failed_only:
            t.append("  ·  failures only", style=RED)
        t.append_text(self.app.live_text())
        self.query_one("#titlebar", Static).update(t)

    # events ------------------------------------------------------------

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        idx = event.list_view.index
        if idx is None or idx >= len(self._project_keys):
            return
        project = self._project_keys[idx]
        if project != self.project:
            self.project = project
            self._refresh_table()
            self._refresh_title()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.query_one("#sessions", DataTable).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        self.query_text = event.value
        self._refresh_table()
        self._refresh_title()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.query_one("#sessions", DataTable).focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        provider, session_id = str(event.row_key.value).split("\x1f", 1)
        self.app.push_screen(SessionScreen(provider, session_id))

    def action_focus_filter(self) -> None:
        self.query_one("#filter", Input).focus()

    def action_clear_filter(self) -> None:
        f = self.query_one("#filter", Input)
        f.value = ""
        self.query_one("#sessions", DataTable).focus()

    def action_toggle_failed(self) -> None:
        self.failed_only = not self.failed_only
        self._refresh_table()
        self._refresh_title()


# ---------------------------------------------------------- session detail


class SessionScreen(Screen):
    """One session: overview, errors, tool calls (with full detail) and a live timeline."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("o", "tab('overview')", "Overview"),
        Binding("e", "tab('errors')", "Errors"),
        Binding("t", "tab('tools')", "Tool calls"),
        Binding("l", "tab('timeline')", "Timeline"),
    ]

    def __init__(self, provider: str, session_id: str) -> None:
        super().__init__()
        self.provider = provider
        self.session_id = session_id
        self.session: sqlite3.Row | None = None
        self.calls: list[sqlite3.Row] = []
        self.events: list[sqlite3.Row] = []
        self.last_rowid = 0

    def _load(self) -> list[sqlite3.Row]:
        """(Re)load the session and calls; return events not seen yet."""
        conn = self.app.conn
        self.session = store.find_session(conn, self.session_id)
        self.calls = store.session_tool_calls(conn, self.provider, self.session_id)
        new = store.session_events_after(conn, self.provider, self.session_id, self.last_rowid)
        if new:
            self.last_rowid = max(e["_rowid"] for e in new)
            self.events.extend(new)
            self.events.sort(key=lambda e: (e["ts"], e["_rowid"]))
        return new

    def compose(self) -> ComposeResult:
        self._load()
        initial = "errors" if render.error_events(self.events) else "overview"
        yield Static(id="titlebar")
        with TabbedContent(initial=initial):
            with TabPane("Overview", id="overview"):
                with VerticalScroll():
                    yield Static(id="overview-body")
            with TabPane("Errors", id="errors"):
                with Vertical(classes="split-top"):
                    yield DataTable(id="errors-table", cursor_type="row")
                with VerticalScroll(classes="detail"):
                    yield Static(id="error-detail")
            with TabPane("Tool calls", id="tools"):
                with Vertical(classes="split-top"):
                    yield DataTable(id="tools-table", cursor_type="row")
                with VerticalScroll(classes="detail"):
                    yield Static(id="tool-detail")
            with TabPane("Timeline", id="timeline"):
                yield DataTable(id="timeline-table", cursor_type="row")
        yield Static(legend(), id="legend")
        yield Footer()

    def on_mount(self) -> None:
        cols = {
            "#errors-table": ["TIME [clk]", "SOURCE", "ERROR"],
            "#tools-table": ["TIME [clk]", "TOOL", "STATUS", "DURATION", "INPUT (sanitized)"],
            "#timeline-table": ["TIME [clk]", "EVENT", "DETAIL"],
        }
        for sel, labels in cols.items():
            table = self.query_one(sel, DataTable)
            for label in labels:
                table.add_column(Text(label), key=label)
        self._fill_timeline(self.events)
        self._render_all()
        tab = self.query_one(TabbedContent).active
        self._focus_tab(tab)

    # rendering ----------------------------------------------------------

    def _refresh_title(self) -> None:
        s = self.session
        title = Text("AgentLens", style=f"bold {GREEN}")
        title.append(f"  ·  session {self.session_id}", style=TEXT)
        if s is not None:
            title.append("  ·  ")
            title.append_text(state_text(s))
            title.append(f"  ·  {render.short_path(s['project'], 40)}", style=MUTED)
        title.append_text(self.app.live_text())
        self.query_one("#titlebar", Static).update(title)

    def _render_all(self) -> None:
        self._refresh_title()
        s = self.session
        if s is None:
            self.query_one("#overview-body", Static).update(Text("Session not found.", style=RED))
            return
        self.query_one("#overview-body", Static).update(
            kv_text(render.session_overview_rows(s, self.calls, self.events))
        )
        self._fill_tools()
        self._fill_errors()
        n_err = len(render.error_events(self.events))
        tabs = self.query_one(TabbedContent)
        tabs.get_tab("errors").label = Text(f"Errors ({n_err})", style=RED if n_err else "")
        tabs.get_tab("tools").label = f"Tool calls ({len(self.calls)})"
        tabs.get_tab("timeline").label = f"Timeline ({len(self.events)})"

    def _fill_tools(self) -> None:
        rows = []
        for i, c in enumerate(self.calls):
            st = render.status_detail(c)
            rows.append((c["tool_use_id"] or f"#{i}", [
                tagged(render.fmt_ts(c["started_at"] or c["ended_at"], short=True)),
                Text(c["tool_name"] or "?", style="bold"),
                Text(st, style=status_style(st)),
                tagged(*render.duration_parts(c)),
                Text(render.clip(render.tool_summary(c["tool_name"], c["pre_data"]), 80)),
            ]))
        table = self.query_one("#tools-table", DataTable)
        _set_rows(table, rows)
        self._show_tool(table.cursor_row)

    def _fill_errors(self) -> None:
        rows = []
        for e in render.error_events(self.events):
            rows.append((e["event_id"], [
                tagged(render.fmt_ts(e["ts"], short=True)),
                Text(e["tool_name"] or "turn", style="bold"),
                Text(render.clip(render.error_message(e), 100), style=RED),
            ]))
        table = self.query_one("#errors-table", DataTable)
        _set_rows(table, rows)
        if not rows:
            self.query_one("#error-detail", Static).update(
                Text("No errors recorded in this session.", style=GREEN)
            )
        else:
            self._show_error(table.cursor_row)

    def _fill_timeline(self, events: Iterable[sqlite3.Row]) -> None:
        table = self.query_one("#timeline-table", DataTable)
        follow = table.row_count == 0 or table.cursor_row >= table.row_count - 1
        for e in events:
            style = RED if e["type"] in ("tool.failure", "turn.failure") else ""
            table.add_row(
                tagged(render.fmt_ts(e["ts"], short=True)),
                Text(e["type"], style=style or MUTED),
                Text(render.clip(str(render.event_detail(e)), 90), style=style),
                key=e["event_id"],
            )
        if follow and table.row_count:
            table.move_cursor(row=table.row_count - 1)

    def _call_for(self, tool_use_id: str | None) -> sqlite3.Row | None:
        return next((c for c in self.calls if tool_use_id and c["tool_use_id"] == tool_use_id), None)

    def _show_tool(self, idx: int) -> None:
        detail = self.query_one("#tool-detail", Static)
        if not self.calls or not 0 <= idx < len(self.calls):
            detail.update(Text("No tool calls recorded.", style=MUTED))
            return
        detail.update(kv_text(render.tool_call_fields(self.calls[idx])))

    def _show_error(self, idx: int) -> None:
        errors = render.error_events(self.events)
        if not 0 <= idx < len(errors):
            return
        e = errors[idx]
        call = self._call_for(e["tool_use_id"])
        if call is not None:
            body = kv_text(render.tool_call_fields(call))
        else:
            data = render.parse_data(e["data"])
            rows = [
                ("source", "turn failure (StopFailure)" if e["type"] == "turn.failure" else e["type"], ""),
                ("time", render.fmt_ts(e["ts"]), render.CLK),
                ("error", str(data.get("error") or render.UNAVAILABLE), render.OBS if data.get("error") else ""),
            ]
            if data.get("error_details"):
                rows.append(("details", str(data["error_details"]), render.OBS))
            body = kv_text(rows)
        self.query_one("#error-detail", Static).update(body)

    def _focus_tab(self, tab: str) -> None:
        target = {"errors": "#errors-table", "tools": "#tools-table", "timeline": "#timeline-table"}.get(tab)
        if target:
            self.query_one(target, DataTable).focus()

    # events ---------------------------------------------------------------

    def refresh_data(self) -> None:
        new = self._load()
        if not new:
            return
        self._fill_timeline(sorted(new, key=lambda e: (e["ts"], e["_rowid"])))
        self._render_all()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "tools-table":
            self._show_tool(event.cursor_row)
        elif event.data_table.id == "errors-table":
            self._show_error(event.cursor_row)

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        self._focus_tab(event.pane.id or "")

    def action_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = tab
        self._focus_tab(tab)

    def action_back(self) -> None:
        self.app.pop_screen()


# --------------------------------------------------------------------- app


class AgentLensApp(App):
    TITLE = "AgentLens"
    CSS = CSS
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "sync", "Sync now"),
        Binding("p", "toggle_live", "Pause/resume live"),
    ]

    def __init__(self, live: bool = True, interval: float = LIVE_INTERVAL_S) -> None:
        super().__init__()
        self.live = live
        self.interval = interval
        self.conn: sqlite3.Connection | None = None
        self.last_sync: float | None = None
        self.sync_error: str | None = None
        self._syncing = False
        self._seen_rowid = 0

    def on_mount(self) -> None:
        self.conn = store.connect()
        self._seen_rowid = store.max_event_rowid(self.conn)
        self.push_screen(SessionsScreen())
        self.set_interval(self.interval, self._tick)

    def on_unmount(self) -> None:
        if self.conn is not None:
            self.conn.close()

    def live_text(self) -> Text:
        if self.sync_error:
            return Text(f"  ·  sync error: {self.sync_error}", style=RED)
        if not self.live:
            return Text("  ·  live paused (p)", style=AMBER)
        t = Text("  ·  ● live", style=GREEN)
        if self.last_sync:
            t.append(f" (synced {time.strftime('%H:%M:%S', time.localtime(self.last_sync))})", style=DIM)
        return t

    async def _tick(self) -> None:
        if self.live:
            await self.sync()

    async def sync(self) -> None:
        """Drain the spool (never waiting on the lock) and refresh if anything new is stored.

        Refresh is driven by the DB's max rowid, not by this ingest's result, so
        events ingested by a hook-triggered ingest are picked up too.
        """
        if self._syncing or self.conn is None:
            return
        self._syncing = True
        try:
            try:
                await asyncio.to_thread(ingest, 0.0)
                self.sync_error = None
            except Exception as exc:  # noqa: BLE001 - keep the dashboard usable on a stale DB
                from agentlens.errlog import log_error

                log_error("tui: ingest failed", exc)
                self.sync_error = str(exc)[:80]
            self.last_sync = time.time()
            top = store.max_event_rowid(self.conn)
            changed = top != self._seen_rowid
            self._seen_rowid = top
            screen = self.screen
            if changed and hasattr(screen, "refresh_data"):
                screen.refresh_data()
            elif hasattr(screen, "_refresh_title"):
                screen._refresh_title()
        finally:
            self._syncing = False

    async def action_sync(self) -> None:
        await self.sync()

    def action_toggle_live(self) -> None:
        self.live = not self.live
        screen = self.screen
        if hasattr(screen, "_refresh_title"):
            screen._refresh_title()


def run() -> int:
    AgentLensApp().run()
    return 0

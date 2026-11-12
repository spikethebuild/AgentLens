"""Terminal dashboard (``agentlens`` with no arguments). Needs the ``tui`` extra.

Read-only with respect to the user's configuration: it never touches Claude
Code settings and never makes network calls. Its only write is the normal
spool -> SQLite ingest, run under the existing ingest lock.

All table and detail text goes through ``rich.text.Text`` so provenance tags
such as ``[obs]`` are shown literally instead of being parsed as markup;
border titles are escaped for the same reason.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from typing import Iterable, Sequence

from rich.markup import escape
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.theme import Theme
from textual.widgets import (
    DataTable,
    Digits,
    Footer,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
    TabbedContent,
    TabPane,
)

from agentlens import cache, render, store
from agentlens.ingest import ingest

HISTORY_LIMIT = 1000  # sessions loaded into the explorer
FEED_LIMIT = 200      # rows kept in the live activity feed
LIVE_INTERVAL_S = 1.0

# Pastel violet palette. Status colours are reserved for status and always
# paired with an icon and a word, never colour alone.
BG, SURFACE, RAISED = "#13111c", "#1a1726", "#221e33"
LINE, LINE_HI = "#352f4d", "#b8a6f2"
VIOLET, LAVENDER = "#c3b1ff", "#a996f0"
TEXT, MUTED, DIM = "#e2ddf3", "#9a93b8", "#655f80"
MINT, PEACH, ROSE = "#9fe0c0", "#f3c48f", "#f5a3b5"

THEME = Theme(
    name="agentlens-violet",
    primary=VIOLET,
    secondary=LAVENDER,
    accent=VIOLET,
    foreground=TEXT,
    background=BG,
    surface=SURFACE,
    panel=RAISED,
    success=MINT,
    warning=PEACH,
    error=ROSE,
    dark=True,
)

CSS = f"""
Screen {{ background: {BG}; color: {TEXT}; }}

#topbar {{ height: 1; padding: 0 1; background: {BG}; }}
#brand {{ width: 1fr; }}
#livestatus {{ width: auto; }}

.panel {{
    border: round {LINE};
    border-title-color: {MUTED};
    border-title-style: bold;
    border-subtitle-color: {DIM};
    background: {BG};
    padding: 0 1;
}}
.panel:focus-within {{ border: round {LINE_HI}; border-title-color: {VIOLET}; }}

#tiles {{ height: 5; }}
.tile {{
    width: 1fr; height: 5;
    border: round {LINE};
    border-title-color: {MUTED};
    border-subtitle-color: {DIM};
    padding: 0 1;
    align: center middle;
}}
.tile Digits {{ color: {VIOLET}; width: auto; }}
.tile.-bad Digits {{ color: {ROSE}; }}
.tile.-bad {{ border: round #6b3a4a; }}
.tile.-muted Digits {{ color: {DIM}; }}

#body {{ height: 3fr; min-height: 8; }}
#projects-panel {{ width: 32; }}
#projects {{ background: {BG}; height: 1fr; }}
#projects > ListItem {{ background: {BG}; padding: 0 0 1 0; }}
#projects > ListItem.-highlight {{ background: {RAISED}; }}
#projects:focus > ListItem.-highlight {{ background: #3a2f5c; }}

#filter {{ display: none; background: {SURFACE}; border: round {LINE_HI}; color: {TEXT}; height: 3; }}
#filter.-visible {{ display: block; }}

#feed-panel {{ height: 2fr; min-height: 6; }}

DataTable {{ background: {BG}; color: {TEXT}; scrollbar-size-vertical: 1; }}
DataTable > .datatable--header {{ background: {BG}; color: {MUTED}; text-style: bold; }}
DataTable > .datatable--cursor {{ background: {RAISED}; color: {TEXT}; }}
DataTable:focus > .datatable--cursor {{ background: #3a2f5c; color: #f4f0ff; }}
DataTable > .datatable--hover {{ background: {SURFACE}; }}

#legend {{ display: none; height: auto; color: {DIM}; padding: 0 2; }}
#legend.-visible {{ display: block; }}

TabbedContent {{ height: 1fr; }}
TabPane {{ padding: 0; }}
Tabs {{ background: {BG}; }}
Tab {{ color: {MUTED}; }}
Tab.-active {{ color: {VIOLET}; text-style: bold; }}
Underline > .underline--bar {{ color: {VIOLET}; background: {LINE}; }}

.list-panel {{ height: 1fr; max-height: 14; }}
.detail-panel {{ height: 2fr; }}
#overview-panel {{ height: 1fr; }}
#timeline-panel {{ height: 1fr; }}

Footer {{ background: {SURFACE}; }}
"""


# --------------------------------------------------------------- formatting


def tagged(value: str, tag: str = "", style: str | None = None) -> Text:
    """A value followed by its (dimmed) provenance tag, both shown literally."""
    t = Text()
    if value.startswith(render.UNAVAILABLE):
        t.append(value, style=f"italic {PEACH}")
    else:
        t.append(value, style=style or "")
    if tag:
        t.append(" " + tag, style=DIM)
    return t


def header(label: str) -> Text:
    """Column header: name in the header style, provenance tag dimmed."""
    name, _, tag = label.partition(" [")
    t = Text(name)
    if tag:
        t.append(" [" + tag, style=f"not bold {DIM}")
    return t


def kv_table(rows: Sequence[tuple[str, str, str]]) -> Table:
    """label / value / provenance rows; long values wrap under their own column."""
    grid = Table.grid(padding=(0, 2), expand=True)
    grid.add_column(style=MUTED, no_wrap=True)
    grid.add_column(overflow="fold", ratio=1)
    grid.add_column(no_wrap=True)
    for k, v, tag in rows:
        grid.add_row(Text(k), tagged(v), Text(tag, style=DIM))
    return grid


def status_cell(status: str) -> Text:
    """Status with an icon and a word, so it never relies on colour alone."""
    if status.startswith(("failed", "interrupted")):
        return Text("✗ " + status, style=ROSE)
    if status.startswith("denied"):
        return Text("⊘ " + status, style=PEACH)
    if status == "ok":
        return Text("✓ ok", style=MINT)
    return Text("… " + status, style=MUTED)


def state_cell(s: sqlite3.Row) -> Text:
    if render.session_state(s) == "open":
        return Text("● open", style=VIOLET)
    return Text("ended", style=DIM)


def failed_cell(n: int) -> Text:
    return Text(f"{n} ✗", style=f"bold {ROSE}") if n else Text("·", style=DIM)


def split_duration(ms: float | None) -> tuple[str, str]:
    """(digits, unit) for a big-number tile."""
    if ms is None:
        return "-", ""
    if ms < 1000:
        return f"{ms:.0f}", "ms"
    s = ms / 1000
    if s < 60:
        return f"{s:.1f}", "s"
    m, sec = divmod(int(round(s)), 60)
    if m < 60:
        return f"{m}:{sec:02d}", "m:s"
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}", "h:m"


def split_tokens(n: int | None) -> tuple[str, str]:
    """(digits, unit) for a token-count tile; Digits cannot draw letters."""
    if n is None:
        return "-", ""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}", "M"
    if n >= 1000:
        return f"{n / 1000:.1f}", "k"
    return str(n), ""


def clock(ts: float | None) -> str:
    """HH:MM:SS for dense tables (full date stays in the detail panels)."""
    return render.fmt_ts(ts, short=True)[6:] if ts is not None else render.UNAVAILABLE


def session_key(provider: str, session_id: str) -> str:
    return f"{provider}\x1f{session_id}"


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


def _add_columns(table: DataTable, labels: Iterable[str]) -> None:
    for label in labels:
        table.add_column(header(label), key=label)


# ----------------------------------------------------------------- widgets


class TopBar(Horizontal):
    def compose(self) -> ComposeResult:
        yield Static(id="brand")
        yield Static(id="livestatus")

    def show(self, crumbs: Text) -> None:
        brand = Text("◆ AgentLens", style=f"bold {VIOLET}")
        if crumbs.plain:
            brand.append("  ›  ", style=DIM)
            brand.append_text(crumbs)
        self.query_one("#brand", Static).update(brand)
        self.query_one("#livestatus", Static).update(self.app.live_text())


class Tile(Vertical):
    """A big number with a label (title) and unit/provenance (subtitle)."""

    def __init__(self, label: str, tag: str, id: str) -> None:
        super().__init__(classes="tile", id=id)
        self.label = label
        self.tag = tag
        self.value = "-"

    def compose(self) -> ComposeResult:
        yield Digits("-")

    def on_mount(self) -> None:
        self.border_title = escape(self.label)
        self.border_subtitle = escape(self.tag)

    def set(self, value: str, unit: str = "", tone: str = "") -> None:
        self.value = value
        self.query_one(Digits).update(value)
        if value == "-":
            self.border_subtitle = escape(render.UNAVAILABLE)
        else:
            self.border_subtitle = escape(" ".join(x for x in (unit, self.tag) if x))
        self.set_class(tone == "bad", "-bad")
        self.set_class(tone == "muted", "-muted")


def legend() -> Static:
    return Static(Text(render.LEGEND, style=DIM), id="legend")


# ---------------------------------------------------------------- home


class SessionsScreen(Screen):
    """Home: headline tiles, projects, session history and a live activity feed."""

    BINDINGS = [
        Binding("slash", "focus_filter", "Filter"),
        Binding("f", "toggle_failed", "Failures only"),
        Binding("question_mark", "toggle_legend", "Legend"),
        Binding("escape", "clear_filter", "Clear filter", show=False),
    ]

    COLUMNS = [
        "SESSION", "STARTED [clk]", "DURATION [calc]", "STATE", "TOOLS [calc]", "FAILED [calc]",
        "TOKENS IN/OUT [obs]",
    ]
    FEED_COLUMNS = ["TIME [clk]", "SESSION", "EVENT", "DETAIL"]

    def __init__(self) -> None:
        super().__init__()
        self.sessions: list[sqlite3.Row] = []
        self.project: str | None = None  # None = all projects
        self.failed_only = False
        self.query_text = ""
        self._project_keys: list[str | None] = []
        self.feed_rowid = 0

    def compose(self) -> ComposeResult:
        yield TopBar(id="topbar")
        with Horizontal(id="tiles"):
            yield Tile("Sessions", "[calc]", id="t-sessions")
            yield Tile("Open now", "[calc]", id="t-open")
            yield Tile("Tool calls", "[calc]", id="t-tools")
            yield Tile("Failed", "[calc]", id="t-failed")
            yield Tile("Failure rate", "[calc]", id="t-rate")
            yield Tile("Avg tool time", "[calc]", id="t-avg")
        with Horizontal(id="body"):
            with Vertical(id="projects-panel", classes="panel"):
                yield ListView(id="projects")
            with Vertical(id="sessions-panel", classes="panel"):
                yield Input(placeholder="type to filter by id, project, model or state · esc clears", id="filter")
                yield DataTable(id="sessions", cursor_type="row")
        with Vertical(id="feed-panel", classes="panel"):
            yield DataTable(id="feed", cursor_type="row", show_header=False)
        yield legend()
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#projects-panel").border_title = "Projects"
        self.query_one("#sessions-panel").border_title = "Sessions"
        feed_panel = self.query_one("#feed-panel")
        feed_panel.border_title = "Live activity"
        feed_panel.border_subtitle = escape("times [clk] · enter opens the session")
        _add_columns(self.query_one("#sessions", DataTable), self.COLUMNS)
        _add_columns(self.query_one("#feed", DataTable), self.FEED_COLUMNS)
        self._append_feed(store.recent_events(self.app.conn, 50))
        self.refresh_data()
        self.query_one("#sessions", DataTable).focus()

    def on_screen_resume(self) -> None:
        self.refresh_data()

    # data ------------------------------------------------------------

    def refresh_data(self) -> None:
        self.sessions = store.list_sessions(self.app.conn, HISTORY_LIMIT)
        self._append_feed(store.events_after(self.app.conn, self.feed_rowid, FEED_LIMIT))
        self._refresh_projects()
        self._refresh_view()

    def _refresh_view(self) -> None:
        self._refresh_table()
        self._refresh_tiles()
        self._refresh_title()

    def _in_project(self) -> list[sqlite3.Row]:
        if self.project is None:
            return self.sessions
        return [s for s in self.sessions if (s["project"] or "") == self.project]

    def _visible(self) -> list[sqlite3.Row]:
        words = self.query_text.lower().split()
        out = []
        for s in self._in_project():
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

    def _refresh_tiles(self) -> None:
        scope = self._in_project()
        n_open = sum(1 for s in scope if render.session_state(s) == "open")
        n_tools = sum(s["tool_calls"] for s in scope)
        n_failed = sum(s["tool_failures"] for s in scope)
        self.query_one("#t-sessions", Tile).set(str(len(scope)))
        self.query_one("#t-open", Tile).set(str(n_open), tone="" if n_open else "muted")
        self.query_one("#t-tools", Tile).set(str(n_tools))
        self.query_one("#t-failed", Tile).set(str(n_failed), tone="bad" if n_failed else "muted")
        rate = self.query_one("#t-rate", Tile)
        if n_tools:
            rate.set(f"{100 * n_failed / n_tools:.0f}", "%", tone="bad" if n_failed else "")
        else:
            rate.set("-")
        stats = store.tool_time_stats(self.app.conn, self.project)
        value, unit = split_duration(stats.avg_ms)
        self.query_one("#t-avg", Tile).set(value, unit)

    def _refresh_table(self) -> None:
        rows = []
        for s in self._visible():
            dur, _ = render.session_duration(s)
            rows.append((session_key(s["provider"], s["session_id"]), [
                Text(s["session_id"][:8], style=f"bold {TEXT}"),
                tagged(render.fmt_ts(s["started_at"], short=True) if s["started_at"]
                       else render.UNAVAILABLE),
                tagged(dur),
                state_cell(s),
                Text(str(s["tool_calls"])),
                failed_cell(s["tool_failures"]),
                Text(f"{render.fmt_tokens(s['tokens_in'])} / {render.fmt_tokens(s['tokens_out'])}")
                if s["api_requests"] else Text(render.UNAVAILABLE, style=DIM),
            ]))
        _set_rows(self.query_one("#sessions", DataTable), rows)
        panel = self.query_one("#sessions-panel")
        shown, total = len(rows), len(self._in_project())
        bits = [f"{shown} of {total}" if shown != total else f"{total}"]
        if self.failed_only:
            bits.append("failures only")
        if self.query_text:
            bits.append(f"filter: {self.query_text}")
        panel.border_subtitle = escape(" · ".join(bits))

    def _refresh_projects(self) -> None:
        counts: dict[str, list[int]] = {}
        for s in self.sessions:
            c = counts.setdefault(s["project"] or "", [0, 0])
            c[0] += 1
            c[1] += 1 if s["tool_failures"] else 0
        keys: list[str | None] = [None, *sorted(counts)]
        labels = [Text.assemble(("All projects", f"bold {TEXT}"), (f"  {len(self.sessions)}", DIM))]
        for p in keys[1:]:
            n, nf = counts[p]
            name = p.rstrip("/").rsplit("/", 1)[-1] if p else "(no project)"
            t = Text(render.clip(name, 20), style=TEXT)
            t.append(f"  {n}", style=DIM)
            if nf:
                t.append(f"  {nf} ✗", style=ROSE)
            if p:
                t.append("\n" + render.short_path(p, 28), style=DIM)
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

    def _append_feed(self, events: Sequence[sqlite3.Row]) -> None:
        if not events:
            return
        table = self.query_one("#feed", DataTable)
        follow = table.row_count == 0 or table.cursor_row >= table.row_count - 1
        for e in events:
            if e["event_id"] in table.rows:
                continue
            bad = e["type"] in ("tool.failure", "turn.failure")
            table.add_row(
                Text(clock(e["ts"]), style=DIM),
                Text(e["session_id"][:8], style=MUTED),
                Text(("✗ " if bad else "") + e["type"], style=ROSE if bad else LAVENDER),
                Text(render.clip(str(render.event_detail(e)), 80), style=ROSE if bad else TEXT),
                key=e["event_id"],
            )
        self.feed_rowid = max(self.feed_rowid, max(e["_rowid"] for e in events))
        while table.row_count > FEED_LIMIT:
            table.remove_row(next(iter(table.rows)))
        if follow and table.row_count:
            table.move_cursor(row=table.row_count - 1, animate=False)

    def _refresh_title(self) -> None:
        crumbs = Text("all projects" if self.project is None else render.short_path(self.project, 50), style=MUTED)
        self.query_one(TopBar).show(crumbs)

    # events ------------------------------------------------------------

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        idx = event.list_view.index
        if idx is None or idx >= len(self._project_keys):
            return
        project = self._project_keys[idx]
        if project != self.project:
            self.project = project
            self._refresh_view()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.query_one("#sessions", DataTable).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        self.query_text = event.value
        self._refresh_table()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.query_one("#sessions", DataTable).focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "sessions":
            provider, session_id = str(event.row_key.value).split("\x1f", 1)
        elif event.data_table.id == "feed":
            row = self.app.conn.execute(
                "SELECT provider, session_id FROM events WHERE event_id = ?", (event.row_key.value,)
            ).fetchone()
            if row is None:
                return
            provider, session_id = row
        else:
            return
        self.app.push_screen(SessionScreen(provider, session_id))

    def action_focus_filter(self) -> None:
        f = self.query_one("#filter", Input)
        f.add_class("-visible")
        f.focus()

    def action_clear_filter(self) -> None:
        f = self.query_one("#filter", Input)
        f.value = ""
        f.remove_class("-visible")
        self.query_one("#sessions", DataTable).focus()

    def action_toggle_failed(self) -> None:
        self.failed_only = not self.failed_only
        self._refresh_table()

    def action_toggle_legend(self) -> None:
        self.query_one("#legend").toggle_class("-visible")


# ---------------------------------------------------------- session detail


class SessionScreen(Screen):
    """One session: headline tiles, then errors, tool calls, timeline and overview."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("e", "tab('errors')", "Errors"),
        Binding("t", "tab('tools')", "Tool calls"),
        Binding("l", "tab('timeline')", "Timeline"),
        Binding("o", "tab('overview')", "Overview"),
        Binding("question_mark", "toggle_legend", "Legend"),
    ]

    def __init__(self, provider: str, session_id: str) -> None:
        super().__init__()
        self.provider = provider
        self.session_id = session_id
        self.session: sqlite3.Row | None = None
        self.calls: list[sqlite3.Row] = []
        self.events: list[sqlite3.Row] = []
        self.usage: dict = {}
        self.usage_reason: str | None = None
        self.last_rowid = 0
        # what the detail panels currently show (kv rows)
        self.tool_detail_rows: list[tuple[str, str, str]] = []
        self.error_detail_rows: list[tuple[str, str, str]] = []

    def _load(self) -> list[sqlite3.Row]:
        """(Re)load the session and calls; return events not seen yet."""
        conn = self.app.conn
        self.session = store.find_session(conn, self.session_id)
        self.calls = store.session_tool_calls(conn, self.provider, self.session_id)
        self.usage = store.session_usage(conn, self.provider, self.session_id)
        self.usage_reason = (None if self.usage["requests"]
                             else cache.usage_reason(conn, self.provider, self.session_id))
        new = store.session_events_after(conn, self.provider, self.session_id, self.last_rowid)
        if new:
            self.last_rowid = max(e["_rowid"] for e in new)
            self.events.extend(new)
            self.events.sort(key=lambda e: (e["ts"], e["_rowid"]))
        return new

    def compose(self) -> ComposeResult:
        self._load()
        initial = "errors" if render.error_events(self.events) else "tools"
        yield TopBar(id="topbar")
        with Horizontal(id="tiles"):
            yield Tile("Duration", "[calc]", id="t-duration")
            yield Tile("Tool calls", "[calc]", id="t-tools")
            yield Tile("Failed", "[calc]", id="t-failed")
            yield Tile("Prompts", "[calc]", id="t-prompts")
            yield Tile("Tool time", "[calc]", id="t-tooltime")
            yield Tile("Tokens in", "[obs]", id="t-tokens")
        with TabbedContent(initial=initial):
            with TabPane("Errors", id="errors"):
                with Vertical(classes="panel list-panel", id="errors-panel"):
                    yield DataTable(id="errors-table", cursor_type="row")
                with VerticalScroll(classes="panel detail-panel", id="error-detail-panel"):
                    yield Static(id="error-detail")
            with TabPane("Tool calls", id="tools"):
                with Vertical(classes="panel list-panel", id="tools-panel"):
                    yield DataTable(id="tools-table", cursor_type="row")
                with VerticalScroll(classes="panel detail-panel", id="tool-detail-panel"):
                    yield Static(id="tool-detail")
            with TabPane("Timeline", id="timeline"):
                with Vertical(classes="panel", id="timeline-panel"):
                    yield DataTable(id="timeline-table", cursor_type="row")
            with TabPane("Overview", id="overview"):
                with VerticalScroll(classes="panel", id="overview-panel"):
                    yield Static(id="overview-body")
        yield legend()
        yield Footer()

    def on_mount(self) -> None:
        _add_columns(self.query_one("#errors-table", DataTable), ["TIME [clk]", "SOURCE", "ERROR"])
        _add_columns(self.query_one("#tools-table", DataTable),
                     ["TIME [clk]", "TOOL", "STATUS", "DURATION", "INPUT (sanitized)"])
        _add_columns(self.query_one("#timeline-table", DataTable), ["TIME [clk]", "EVENT", "DETAIL"])
        self.query_one("#errors-panel").border_title = "Failures"
        self.query_one("#error-detail-panel").border_title = "Details"
        self.query_one("#tools-panel").border_title = "Tool calls"
        self.query_one("#tool-detail-panel").border_title = "Selected call"
        self.query_one("#timeline-panel").border_title = "Timeline"
        self.query_one("#overview-panel").border_title = "Session"
        self._fill_timeline(self.events)
        self._render_all()
        self._focus_tab(self.query_one(TabbedContent).active)

    # rendering ----------------------------------------------------------

    def _refresh_title(self) -> None:
        s = self.session
        crumbs = Text(self.session_id[:8], style=f"bold {TEXT}")
        if s is not None:
            crumbs.append("  ")
            crumbs.append_text(state_cell(s))
            if s["model"]:
                crumbs.append(f"  ·  {s['model']}", style=MUTED)
            crumbs.append(f"  ·  {render.short_path(s['project'], 40)}", style=DIM)
        self.query_one(TopBar).show(crumbs)

    def _render_tiles(self) -> None:
        s = self.session
        assert s is not None
        dur = self.query_one("#t-duration", Tile)
        if s["started_at"] is None:
            dur.set("-")
        else:
            is_open = render.session_state(s) == "open"
            end = s["last_seen"] if is_open else s["ended_at"]
            value, unit = split_duration((end - s["started_at"]) * 1000)
            dur.set(value, f"{unit} so far" if is_open else unit)
        failed = sum(1 for c in self.calls if c["status"] in ("failed", "interrupted"))
        self.query_one("#t-tools", Tile).set(str(len(self.calls)))
        self.query_one("#t-failed", Tile).set(str(failed), tone="bad" if failed else "muted")
        self.query_one("#t-prompts", Tile).set(str(s["prompts"]))
        timed = [c["duration_ms"] for c in self.calls if c["duration_ms"] is not None]
        value, unit = split_duration(sum(timed) if timed else None)
        self.query_one("#t-tooltime", Tile).set(value, f"{unit} · sum of {len(timed)}" if timed else "")
        if self.usage.get("requests"):
            value, unit = split_tokens(self.usage["input"])
            out = render.fmt_tokens(self.usage["output"])
            self.query_one("#t-tokens", Tile).set(value, f"{unit} · out {out}".strip(" ·"))
        else:
            self.query_one("#t-tokens", Tile).set("-")

    def _render_all(self) -> None:
        self._refresh_title()
        s = self.session
        if s is None:
            self.query_one("#overview-body", Static).update(Text("Session not found.", style=ROSE))
            return
        self._render_tiles()
        self.query_one("#overview-body", Static).update(
            kv_table(render.session_overview_rows(s, self.calls, self.events, self.usage, self.usage_reason))
        )
        self._fill_tools()
        self._fill_errors()
        n_err = len(render.error_events(self.events))
        tabs = self.query_one(TabbedContent)
        tabs.get_tab("errors").label = Text.assemble(
            "Errors ", (f"{n_err}", f"bold {ROSE}" if n_err else DIM)
        )
        tabs.get_tab("tools").label = Text.assemble("Tool calls ", (str(len(self.calls)), DIM))
        tabs.get_tab("timeline").label = Text.assemble("Timeline ", (str(len(self.events)), DIM))

    def _fill_tools(self) -> None:
        rows = []
        for i, c in enumerate(self.calls):
            rows.append((c["tool_use_id"] or f"#{i}", [
                Text(clock(c["started_at"] or c["ended_at"]), style=DIM),
                Text(c["tool_name"] or "?", style=f"bold {TEXT}"),
                status_cell(render.status_detail(c)),
                tagged(*render.duration_parts(c)),
                Text(render.clip(render.tool_summary(c["tool_name"], c["pre_data"]), 80), style=MUTED),
            ]))
        table = self.query_one("#tools-table", DataTable)
        _set_rows(table, rows)
        self._show_tool(table.cursor_row)

    def _fill_errors(self) -> None:
        rows = []
        for e in render.error_events(self.events):
            rows.append((e["event_id"], [
                Text(clock(e["ts"]), style=DIM),
                Text("✗ " + (e["tool_name"] or "turn"), style=f"bold {ROSE}"),
                Text(render.clip(render.error_message(e), 100)),
            ]))
        table = self.query_one("#errors-table", DataTable)
        _set_rows(table, rows)
        if not rows:
            self.error_detail_rows = []
            self.query_one("#error-detail", Static).update(
                Text("✓ No errors recorded in this session.", style=MINT)
            )
        else:
            self._show_error(table.cursor_row)

    def _fill_timeline(self, events: Iterable[sqlite3.Row]) -> None:
        table = self.query_one("#timeline-table", DataTable)
        follow = table.row_count == 0 or table.cursor_row >= table.row_count - 1
        for e in events:
            bad = e["type"] in ("tool.failure", "turn.failure")
            table.add_row(
                Text(clock(e["ts"]), style=DIM),
                Text(("✗ " if bad else "") + e["type"], style=ROSE if bad else LAVENDER),
                Text(render.clip(str(render.event_detail(e)), 90), style=ROSE if bad else TEXT),
                key=e["event_id"],
            )
        if follow and table.row_count:
            table.move_cursor(row=table.row_count - 1, animate=False)

    def _call_for(self, tool_use_id: str | None) -> sqlite3.Row | None:
        return next((c for c in self.calls if tool_use_id and c["tool_use_id"] == tool_use_id), None)

    def _show_tool(self, idx: int) -> None:
        detail = self.query_one("#tool-detail", Static)
        if not self.calls or not 0 <= idx < len(self.calls):
            self.tool_detail_rows = []
            detail.update(Text("No tool calls recorded.", style=MUTED))
            return
        self.tool_detail_rows = render.tool_call_fields(self.calls[idx])
        detail.update(kv_table(self.tool_detail_rows))

    def _show_error(self, idx: int) -> None:
        errors = render.error_events(self.events)
        if not 0 <= idx < len(errors):
            return
        e = errors[idx]
        call = self._call_for(e["tool_use_id"])
        if call is not None:
            rows = render.tool_call_fields(call)
        else:
            data = render.parse_data(e["data"])
            rows = [
                ("source", "turn failure (StopFailure)" if e["type"] == "turn.failure" else e["type"], ""),
                ("time", render.fmt_ts(e["ts"]), render.CLK),
                ("error", str(data.get("error") or render.UNAVAILABLE), render.OBS if data.get("error") else ""),
            ]
            if data.get("error_details"):
                rows.append(("details", str(data["error_details"]), render.OBS))
        self.error_detail_rows = rows
        self.query_one("#error-detail", Static).update(kv_table(rows))

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

    def action_toggle_legend(self) -> None:
        self.query_one("#legend").toggle_class("-visible")

    def action_back(self) -> None:
        self.app.pop_screen()


# --------------------------------------------------------------------- app


class AgentLensApp(App):
    TITLE = "AgentLens"
    CSS = CSS
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "sync", "Sync"),
        Binding("p", "toggle_live", "Pause live"),
    ]

    def __init__(self, live: bool = True, interval: float = LIVE_INTERVAL_S) -> None:
        super().__init__()
        self.register_theme(THEME)
        self.theme = THEME.name
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
            return Text(f"sync error: {self.sync_error}", style=ROSE)
        if not self.live:
            return Text("❚❚ paused", style=PEACH)
        t = Text("● live", style=MINT)
        if self.last_sync:
            t.append(f"  {time.strftime('%H:%M:%S', time.localtime(self.last_sync))}", style=DIM)
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
            if hasattr(screen, "_refresh_title"):
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

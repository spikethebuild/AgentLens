"""Terminal dashboard: driven headlessly through Textual's test pilot."""

from __future__ import annotations

import asyncio
import copy
import sys

import pytest

pytest.importorskip("textual")

from conftest import load_fixture  # noqa: E402

from agentlens import cli, paths, spool, store  # noqa: E402
from agentlens.adapters.claude_code import normalize  # noqa: E402
from agentlens.tui import AgentLensApp, SessionScreen, SessionsScreen  # noqa: E402

SID = "3f2a9c1e-5b7d-4e8a-9c0f-1a2b3c4d5e6f"
OTHER = "aaaabbbb-0000-4000-8000-000000000001"
SEQUENCE = [
    "session_start", "user_prompt_submit",
    "pre_tool_use_bash", "post_tool_use_bash",
    "pre_tool_use_bash_fail", "post_tool_use_failure_bash",
    "pre_tool_use_write", "post_tool_use_write",
    "stop_failure", "stop", "session_end",
]
T0 = 1_790_000_000.0


def events_for(names, session_id=SID, cwd=None, t0=T0):
    out = []
    for i, name in enumerate(names):
        payload = copy.deepcopy(load_fixture(name))
        payload["session_id"] = session_id
        if cwd:
            payload["cwd"] = cwd
        out += normalize(payload, t0 + i)
    return out


def seed(*batches):
    conn = store.connect()
    try:
        for b in batches:
            store.insert_events(conn, b)
    finally:
        conn.close()


def drive(coro_fn, size=(160, 40)):
    async def main():
        app = AgentLensApp(live=False)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await coro_fn(app, pilot)
    asyncio.run(main())


def cell_texts(table):
    return [[str(c) for c in table.get_row(k)] for k in table.rows]


def open_session(app, pilot, prefix):
    async def go():
        table = app.screen.query_one("#sessions")
        keys = [k.value for k in table.rows]
        table.move_cursor(row=next(i for i, k in enumerate(keys) if prefix in k))
        table.focus()
        await pilot.press("enter")
        await pilot.pause()
    return go()


# ------------------------------------------------------------- explorer


def test_session_list_shows_history_with_provenance():
    seed(events_for(SEQUENCE))

    async def check(app, pilot):
        assert isinstance(app.screen, SessionsScreen)
        table = app.screen.query_one("#sessions")
        labels = [str(c.label) for c in table.columns.values()]
        assert "STARTED [clk]" in labels and "FAILED [calc]" in labels  # literal, not markup
        rows = cell_texts(table)
        assert len(rows) == 1
        row = rows[0]
        assert row[0] == SID[:8]
        assert row[3] == "ended"  # reason is in the session overview
        assert row[5] == "1 ✗"  # one failed tool call: icon + number, not colour alone
        assert "[obs] reported by Claude Code" in str(app.screen.query_one("#legend").render())
        tiles = {t.id: t.value for t in app.screen.query("Tile")}
        assert tiles["t-sessions"] == "1" and tiles["t-tools"] == "3" and tiles["t-failed"] == "1"
        assert tiles["t-rate"] == "33"
        assert app.screen.query_one("#feed").row_count == len(events_for(SEQUENCE))

    drive(check)


def test_filter_failures_only_and_projects():
    seed(
        events_for(SEQUENCE, cwd="/work/alpha"),
        events_for(["session_start", "pre_tool_use_bash", "post_tool_use_bash"],
                   session_id=OTHER, cwd="/work/beta", t0=T0 + 100),
    )

    async def check(app, pilot):
        screen = app.screen
        table = screen.query_one("#sessions")
        assert table.row_count == 2

        await pilot.press("f")  # failures only
        assert [r[0] for r in cell_texts(table)] == [SID[:8]]
        await pilot.press("f")
        assert table.row_count == 2

        await pilot.press("slash")
        await pilot.press(*"beta")
        await pilot.pause()
        assert [r[0] for r in cell_texts(table)] == [OTHER[:8]]
        await pilot.press("escape")
        await pilot.pause()
        assert table.row_count == 2

        # project grouping: "All", then each project sorted
        assert screen._project_keys == [None, "/work/alpha", "/work/beta"]
        projects = screen.query_one("#projects")
        projects.index = 2
        await pilot.pause()
        assert [r[0] for r in cell_texts(table)] == [OTHER[:8]]

    drive(check)


# --------------------------------------------------------- session detail


def test_session_opens_on_errors_with_full_detail():
    seed(events_for(SEQUENCE))

    async def check(app, pilot):
        await open_session(app, pilot, SID)
        screen = app.screen
        assert isinstance(screen, SessionScreen)
        tabs = screen.query_one("TabbedContent")
        assert tabs.active == "errors"
        errors = screen.query_one("#errors-table")
        assert errors.row_count == 2  # failed Bash call + StopFailure
        rows = {k: (v, tag) for k, v, tag in screen.error_detail_rows}
        assert rows["status"][0].startswith("failed (exit")
        assert rows["exit code"][1] == "[obs]" and rows["started"][1] == "[clk]"
        assert rows["error"][0]  # full message, not clipped

        errors.move_cursor(row=1)
        await pilot.pause()
        assert screen.error_detail_rows[0][1] == "turn failure (StopFailure)"

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, SessionsScreen)

    drive(check)


def test_tool_viewer_shows_input_status_and_duration_provenance():
    seed(events_for(SEQUENCE))

    async def check(app, pilot):
        await open_session(app, pilot, SID)
        screen = app.screen
        await pilot.press("t")
        await pilot.pause()
        assert screen.query_one("TabbedContent").active == "tools"
        tools = screen.query_one("#tools-table")
        assert tools.row_count == 3
        tools.move_cursor(row=0)
        await pilot.pause()
        rows = {k: (v, tag) for k, v, tag in screen.tool_detail_rows}
        assert "input.command" in rows
        assert rows["status"][0] == "ok"
        assert rows["duration"][1] in ("[obs]", "[calc]")
        assert {t.id: t.value for t in screen.query("Tile")}["t-tools"] == "3"

    drive(check)


def test_unpaired_tool_call_shows_unavailable_duration():
    seed(events_for(["session_start", "pre_tool_use_edit"]))

    async def check(app, pilot):
        await open_session(app, pilot, SID)
        assert app.screen.query_one("TabbedContent").active == "tools"  # no errors
        rows = {k: v for k, v, _ in app.screen.tool_detail_rows}
        assert rows["duration"] == "unavailable (no result recorded)"
        assert "No errors recorded" in str(app.screen.query_one("#error-detail").render())

    drive(check)


# ------------------------------------------------------------------ live


def test_live_sync_appends_events_without_rebuilding_timeline():
    seed(events_for(SEQUENCE[:4]))

    async def check(app, pilot):
        await open_session(app, pilot, SID)
        screen = app.screen
        timeline = screen.query_one("#timeline-table")
        before = timeline.row_count
        first_key = next(iter(timeline.rows))

        spool.write_events(events_for(["pre_tool_use_bash_fail", "post_tool_use_failure_bash"], t0=T0 + 50))
        await app.sync()
        await pilot.pause()

        assert timeline.row_count == before + 2
        assert next(iter(timeline.rows)) == first_key
        assert timeline.cursor_row == timeline.row_count - 1  # follows the tail
        assert screen.query_one("#errors-table").row_count == 1
        assert not spool.pending()

    drive(check)


def test_live_sync_shows_new_sessions_and_events_ingested_elsewhere():
    seed(events_for(SEQUENCE))

    async def check(app, pilot):
        table = app.screen.query_one("#sessions")
        assert table.row_count == 1
        # another process ingested this already: only the DB changed
        seed(events_for(["session_start"], session_id=OTHER, t0=T0 + 500))
        await app.sync()
        await pilot.pause()
        assert table.row_count == 2
        assert app.screen.query_one("#feed").row_count == len(events_for(SEQUENCE)) + 1

    drive(check)


def test_filter_box_hidden_until_slash_and_legend_toggles():
    seed(events_for(SEQUENCE))

    async def check(app, pilot):
        f = app.screen.query_one("#filter")
        assert not f.display
        await pilot.press("slash")
        assert f.display and f.has_focus
        await pilot.press("escape")
        assert not f.display
        legend = app.screen.query_one("#legend")
        assert not legend.display
        await pilot.press("question_mark")
        assert legend.display

    drive(check)


def test_dashboard_never_touches_claude_settings(isolated_env):
    seed(events_for(SEQUENCE))

    async def check(app, pilot):
        await app.sync()
        await open_session(app, pilot, SID)

    drive(check)
    assert not isolated_env["settings"].exists()
    assert not paths.claude_settings_path().exists()


# ------------------------------------------------------------------- cli


def test_no_args_without_tty_prints_help(capsys):
    assert cli.main([]) == 0
    assert "usage: agentlens" in capsys.readouterr().out


def test_dashboard_without_textual_explains_extra(monkeypatch, capsys):
    import agentlens

    monkeypatch.setitem(sys.modules, "agentlens.tui", None)  # import fails
    monkeypatch.delattr(agentlens, "tui", raising=False)
    assert cli.main(["dashboard"]) == 1
    captured = capsys.readouterr()
    assert "agentlens[tui]" in captured.err
    assert "usage: agentlens" in captured.out

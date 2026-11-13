"""Terminal dashboard: driven headlessly through Textual's test pilot."""

from __future__ import annotations

import asyncio
import copy
import sys

import pytest

pytest.importorskip("textual")

from conftest import load_fixture  # noqa: E402

from agentlens import cache, cli, paths, spool, store  # noqa: E402
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
        new = ["session_start", "pre_tool_use_bash", "post_tool_use_bash"]
        seed(events_for(new, session_id=OTHER, t0=T0 + 500))
        await app.sync()
        await pilot.pause()
        assert table.row_count == 2
        assert app.screen.query_one("#feed").row_count == len(events_for(SEQUENCE)) + len(events_for(new))

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


# --------------------------------------------------------- usage panels


def test_home_shows_active_session_and_plan_usage(tmp_path):
    import json
    import time

    from agentlens import statusline
    from agentlens.ingest import ingest
    from transcripts import request_lines, write

    now = time.time()
    path = write(tmp_path / "t.jsonl", request_lines("r1", now - 60, read=0, write=40_000, out=900)
                 + request_lines("r2", now - 30, read=40_000, write=500, out=400))
    start = copy.deepcopy(load_fixture("session_start"))
    start.update(session_id=OTHER, transcript_path=str(path))
    spool.write_events(normalize(start, now - 90))
    ingest()
    paths.ensure_dir(paths.home())
    statusline.snapshot_path().write_text(json.dumps(statusline.parse(
        {"session_id": OTHER, "rate_limits": {"five_hour": {"used_percentage": 23.5, "resets_at": now + 3600}}},
        now)))

    async def check(app, pilot):
        active = " ".join(f"{k} {v}" for k, v, _ in app.screen.active_rows)
        plan = " ".join(f"{k} {v}" for k, v, _ in app.screen.plan_rows)
        assert OTHER[:8] in active and "tokens in 81k" in active and "cache hit" in active
        assert "24% used" in plan and "tokens in 5h window" in plan
    drive(check, size=(200, 50))


def test_home_usage_panels_when_nothing_is_available():
    async def check(app, pilot):
        assert "no active session" in app.screen.active_rows[0][1]
        assert "agentlens statusline install" in app.screen.plan_rows[0][1]
    drive(check)


# ------------------------------------------------ v0.2 review: short sessions, coach, tokens & cache


def test_short_empty_sessions_are_hidden_until_a():
    seed(events_for(SEQUENCE),
         events_for(["session_start", "session_end"], session_id=OTHER, t0=T0 + 500))

    async def check(app, pilot):
        table = app.screen.query_one("#sessions")
        assert [r[0] for r in cell_texts(table)] == [SID[:8]]
        assert "1 short hidden · a shows" in str(app.screen.query_one("#sessions-panel").border_subtitle)
        tiles = {t.id: t.value for t in app.screen.query("Tile")}
        assert tiles["t-sessions"] == "2"  # hidden from the list, never from the counts
        table.focus()
        await pilot.press("a")
        assert table.row_count == 2
        await pilot.press("a")
        assert table.row_count == 1

    drive(check)


def test_failure_rate_tile_puts_percent_in_its_title():
    seed(events_for(SEQUENCE))

    async def check(app, pilot):
        rate = app.screen.query_one("#t-rate")
        assert rate.value == "33" and "%" in str(rate.border_title)

    drive(check)


def _usage_session(tmp_path, sid=OTHER, cwd=None):
    """A session with a transcript: request r3 loses the cache after a compaction."""
    from agentlens.ingest import ingest
    from transcripts import request_lines, write

    lines = (request_lines("r1", T0 + 10, read=0, write=40_000, session_id=sid)
             + request_lines("r2", T0 + 20, read=40_000, write=500, session_id=sid)
             + request_lines("r3", T0 + 40, read=0, write=12_000, session_id=sid))
    path = write(tmp_path / "t.jsonl", lines)
    start = copy.deepcopy(load_fixture("session_start"))
    start.update(session_id=sid, transcript_path=str(path))
    if cwd:
        start["cwd"] = cwd
    pre = copy.deepcopy(load_fixture("pre_compact"))
    pre.update(session_id=sid)
    post = copy.deepcopy(load_fixture("post_compact"))
    post.update(session_id=sid)
    spool.write_events(normalize(start, T0) + normalize(pre, T0 + 25) + normalize(post, T0 + 30))
    ingest()


def test_session_tokens_and_cache_tab_marks_misses_with_evidence(tmp_path):
    _usage_session(tmp_path)

    async def check(app, pilot):
        await open_session(app, pilot, OTHER[:8])
        screen = app.screen
        assert isinstance(screen, SessionScreen)
        await pilot.press("c")
        await pilot.pause()
        table = screen.query_one("#cache-table")
        rows = cell_texts(table)
        assert [r[1] for r in rows] == ["40k", "41k", "12k"]
        assert rows[0][6] == "first request: cache built"
        assert rows[2][6].startswith("✗ miss · compaction")
        assert "CONTEXT [calc]" in [str(c.label) for c in table.columns.values()]
        assert "1 ✗" in str(screen.query_one("TabbedContent").get_tab("cache").label)
        table.move_cursor(row=2)
        await pilot.pause()
        detail = dict((k, v) for k, v, _ in screen.cache_detail_rows)
        assert detail["request id"] == "r3" and "compaction" in detail["cache miss"]
        assert any(k == "evidence" for k, _, _ in screen.cache_detail_rows)
        assert any(f.rule == "compaction" or "compact" in f.title.lower() for f in screen.session_findings)

    drive(check, size=(200, 50))


def test_session_without_usage_explains_why(tmp_path):
    seed(events_for(SEQUENCE))

    async def check(app, pilot):
        await open_session(app, pilot, SID[:8])
        await pilot.press("c")
        await pilot.pause()
        assert app.screen.query_one("#cache-table").row_count == 0
        assert "unavailable" in str(app.screen.query_one("#cache-detail").render())

    drive(check)


def test_home_coach_panel_lists_findings_with_evidence(tmp_path, monkeypatch):
    from agentlens import coach

    finding = cache.Finding(rule="long_context", severity="warn", ts=T0, title="Long context in session abcd1234",
                            impact="12 requests above 200k [calc]", impact_tokens=900_000,
                            evidence=["requests r1…r12, 2026-09-21 10:00–11:00"], fix="compact earlier")
    real = coach.analyze
    monkeypatch.setattr(coach, "analyze", lambda *a, **kw: real(*a, **kw).__class__(
        "last 14 days · all projects", [], 12, [], {}, [finding]))
    seed(events_for(SEQUENCE))

    async def check(app, pilot):
        body = str(app.screen.query_one("#coach-body").render())
        assert "Long context in session abcd1234" in body
        assert "evidence: requests r1…r12" in body and "fix: compact earlier" in body
        assert "[calc]" in body  # provenance shown literally, not parsed as markup

    drive(check)


def test_home_coach_panel_when_nothing_to_report():
    seed(events_for(SEQUENCE))

    async def check(app, pilot):
        assert "No findings" in str(app.screen.query_one("#coach-body").render())

    drive(check)


def test_prompts_tab_ranks_prompts_by_tokens(tmp_path):
    from agentlens.ingest import ingest
    from transcripts import request_lines, write

    lines = (request_lines("r1", T0 + 11, read=0, write=10_000, session_id=OTHER)
             + request_lines("r2", T0 + 31, read=10_000, write=90_000, session_id=OTHER))
    path = write(tmp_path / "t.jsonl", lines)
    evs = []
    for name, ts, extra in (("session_start", T0, {"transcript_path": str(path)}),
                            ("user_prompt_submit", T0 + 10, {"prompt": "small"}),
                            ("user_prompt_submit", T0 + 30, {"prompt": "a much bigger ask"})):
        p = copy.deepcopy(load_fixture(name))
        p.update(session_id=OTHER, **extra)
        evs += normalize(p, ts)
    spool.write_events(evs)
    ingest()

    async def check(app, pilot):
        await open_session(app, pilot, OTHER[:8])
        await pilot.press("u")
        await pilot.pause()
        screen = app.screen
        rows = cell_texts(screen.query_one("#prompts-table"))
        assert [r[0] for r in rows] == ["2", "1"]  # largest first
        assert rows[0][2] == "17 chars (text not stored)" and rows[0][4] == "100k" and rows[0][5] == "91%"
        detail = dict((k, v) for k, v, _ in screen.prompt_detail_rows)
        assert "r2" in detail["largest request"] and "capture_prompts" in detail["text"]
        await pilot.press("s")
        assert [r[0] for r in cell_texts(screen.query_one("#prompts-table"))] == ["1", "2"]

    drive(check, size=(200, 50))

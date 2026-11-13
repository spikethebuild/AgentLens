"""Opt-in status line: plan usage parsing, recording, output, install/uninstall, doctor, render."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentlens import cli, doctor, installer, paths, render, spool, statusline, store
from agentlens.ingest import ingest

PY = sys.executable
NOW = time.time()


def payload(five=23.5, week=41.2, five_reset=NOW + 3600, week_reset=NOW + 5 * 86400, **extra):
    limits = {}
    if five is not None:
        limits["five_hour"] = {"used_percentage": five, "resets_at": five_reset}
    if week is not None:
        limits["seven_day"] = {"used_percentage": week, "resets_at": week_reset}
    p = {
        "session_id": "sess-1", "cwd": "/work/proj", "transcript_path": "/secret/transcript.jsonl",
        "model": {"id": "claude-opus-5-5"}, "cost": {"total_cost_usd": 1.23},
        "workspace": {"current_dir": "/work/proj"}, "rate_limits": limits,
    }
    p.update(extra)
    return p


def run_cli(monkeypatch, capsys, body) -> str:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw)))
    assert cli.main(["statusline"]) == 0
    return capsys.readouterr().out


# ------------------------------------------------------------------ parsing


def test_parse_keeps_only_documented_windows():
    snap = statusline.parse(payload(), NOW)
    assert set(snap) == {"captured_at", "session_id", "cwd", "windows"}
    assert snap["windows"]["five_hour"] == {"used_percentage": 23.5, "resets_at": NOW + 3600}
    assert set(snap["windows"]) == {"five_hour", "seven_day"}


@pytest.mark.parametrize("bad", [
    {}, {"rate_limits": None}, {"rate_limits": {}}, {"rate_limits": {"five_hour": {"used_percentage": "x"}}},
    {"rate_limits": {"five_hour": {"used_percentage": 140}}}, {"rate_limits": {"five_hour": {"used_percentage": True}}},
    [], "text",
])
def test_parse_rejects_missing_or_malformed(bad):
    assert statusline.parse(bad, NOW) is None


# ----------------------------------------------------------- run end to end


def test_run_prints_line_records_snapshot_and_spools_on_change(monkeypatch, capsys):
    out = run_cli(monkeypatch, capsys, payload())
    assert out.startswith("session 24% · resets ") and "week 41%" in out
    snap = statusline.load_snapshot()
    assert snap["windows"]["seven_day"]["used_percentage"] == 41.2
    assert oct(statusline.snapshot_path().stat().st_mode & 0o777) == "0o600"
    assert len(spool.pending()) == 1
    run_cli(monkeypatch, capsys, payload())               # same rounded values: no new event
    assert len(spool.pending()) == 1
    run_cli(monkeypatch, capsys, payload(five=30.0))      # changed: new event
    assert len(spool.pending()) == 2
    ingest()
    conn = store.connect()
    rows = conn.execute("SELECT type, data FROM events WHERE type = 'plan.usage'").fetchall()
    conn.close()
    assert len(rows) == 2 and json.loads(rows[-1]["data"])["windows"]["five_hour"]["used_percentage"] == 30.0


def test_nothing_but_windows_reaches_disk(monkeypatch, capsys):
    run_cli(monkeypatch, capsys, payload())
    blob = statusline.snapshot_path().read_text() + "".join(p.read_text() for p in spool.pending())
    for forbidden in ("/secret/transcript.jsonl", "claude-opus-5-5", "total_cost_usd", "1.23"):
        assert forbidden not in blob


@pytest.mark.parametrize("body", [b"", b"{not json", json.dumps({"session_id": "x"}).encode(), b"x" * 10])
def test_never_blank_never_fails(monkeypatch, capsys, body):
    out = run_cli(monkeypatch, capsys, body)
    assert out.strip()  # always prints something


def test_no_rate_limits_says_not_reported(monkeypatch, capsys):
    assert run_cli(monkeypatch, capsys, payload(five=None, week=None)).strip() == "AgentLens · plan usage not reported"


def test_expired_window_is_dropped():
    snap = statusline.parse(payload(five_reset=NOW - 10), NOW - 100)
    assert set(statusline.current_windows(snap, NOW)) == {"seven_day"}
    assert statusline.render_line(snap, NOW) == "week 41%"


def test_previous_status_line_runs_below(monkeypatch, capsys):
    paths.ensure_dir(paths.home())
    statusline.previous_path().write_text(json.dumps({"statusLine": {"type": "command", "command": "cat >/dev/null; echo mine"}}))
    out = run_cli(monkeypatch, capsys, payload()).splitlines()
    assert out[0].startswith("session 24%") and out[1] == "mine"


def test_failing_or_slow_previous_status_line_is_skipped(monkeypatch, capsys):
    paths.ensure_dir(paths.home())
    monkeypatch.setattr(statusline, "WRAPPED_TIMEOUT_S", 0.5)
    for cmd in ("exit 3", "sleep 5"):
        statusline.previous_path().write_text(json.dumps({"statusLine": {"command": cmd}}))
        out = run_cli(monkeypatch, capsys, payload()).splitlines()
        assert len(out) == 1 and out[0].startswith("session")


def test_subprocess_fast_path(subprocess_env):
    r = subprocess.run([PY, "-m", "agentlens", "statusline"], input=json.dumps(payload()), env=subprocess_env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and r.stdout.startswith("session 24%")


# ------------------------------------------------------------ install


def test_install_uninstall_fresh(isolated_env, capsys):
    settings = isolated_env["settings"]
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"model": "opus"}))
    before = settings.read_bytes()
    assert cli.main(["statusline", "install", "--dry-run", "--python", PY]) == 0
    assert "Would set" in capsys.readouterr().out and settings.read_bytes() == before
    assert cli.main(["statusline", "install", "--python", PY]) == 0
    doc = json.loads(settings.read_text())
    assert doc["statusLine"] == {"type": "command", "command": installer.statusline_command(PY)}
    assert doc["model"] == "opus"
    assert installer.statusline_state() == installer.statusline_command(PY)
    assert not installer.install_statusline(python=PY).changed  # idempotent
    assert cli.main(["statusline", "uninstall"]) == 0
    assert "statusLine" not in json.loads(settings.read_text())


def test_install_keeps_and_restores_previous(isolated_env, capsys):
    settings = isolated_env["settings"]
    settings.parent.mkdir(parents=True)
    mine = {"type": "command", "command": "~/.claude/statusline.sh", "padding": 2}
    settings.write_text(json.dumps({"statusLine": mine}))
    assert cli.main(["statusline", "install", "--python", PY]) == 0
    out = capsys.readouterr().out
    assert "~/.claude/statusline.sh" in out and "restored by" in out
    doc = json.loads(settings.read_text())
    assert doc["statusLine"]["padding"] == 2 and "agentlens statusline" in doc["statusLine"]["command"]
    assert json.loads(statusline.previous_path().read_text())["statusLine"] == mine
    assert oct(statusline.previous_path().stat().st_mode & 0o777) == "0o600"
    change = installer.uninstall_statusline()
    assert change.updated == ["statusLine"]
    assert json.loads(settings.read_text())["statusLine"] == mine
    assert not statusline.previous_path().exists()


def test_install_refuses_invalid_settings(isolated_env, capsys):
    settings = isolated_env["settings"]
    settings.parent.mkdir(parents=True)
    settings.write_text("{oops")
    assert cli.main(["statusline", "install"]) == 1
    assert settings.read_text() == "{oops"


def test_uninstall_leaves_foreign_status_line(isolated_env):
    settings = isolated_env["settings"]
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "mine.sh"}}))
    assert not installer.uninstall_statusline().changed


# ------------------------------------------------------------- doctor / render


def test_doctor_tests_installed_status_line(tmp_path, monkeypatch):
    src = str(Path(__file__).resolve().parent.parent / "src")
    monkeypatch.setenv("PYTHONPATH", src + os.pathsep + os.environ.get("PYTHONPATH", ""))
    monkeypatch.chdir(tmp_path)
    assert doctor.check_statusline() == []  # opt-in: no check when not installed
    installer.install_statusline(python=PY)
    checks = doctor.check_statusline()
    assert checks[0].level == doctor.OK, checks[0].detail
    assert statusline.load_snapshot() is None  # the test ran in a temporary home
    installer.install_statusline(python="/gone/python")
    assert doctor.check_statusline()[0].level == doctor.FAIL


def test_plan_usage_rows():
    rows = render.plan_usage_rows(None, False, NOW)
    assert rows[0][1].startswith("unavailable (enable with `agentlens statusline install`")
    assert "not reported yet" in render.plan_usage_rows(None, True, NOW)[0][1]
    snap = statusline.parse(payload(), NOW - 120)
    rows = {k: (v, t) for k, v, t in render.plan_usage_rows(snap, True, NOW, {"requests": 3, "input": 2_100_000, "output": 48_000})}
    assert rows["current session (5h)"][0].startswith("24% used") and rows["current session (5h)"][1] == "[obs]"
    assert rows["current week"][0].startswith("41% used")
    assert "2 min ago" in rows["as of"][0] and rows["as of"][1] == "[clk]"
    assert rows["tokens this session"] == (
        "2.10M in / 48k out (this machine)", "[calc]")
    assert not any("limit" in v.lower() and "token" in v.lower() for v, _ in rows.values())
    expired = statusline.parse(payload(five_reset=NOW - 1, week_reset=NOW - 1), NOW - 100)
    assert "have reset" in render.plan_usage_rows(expired, True, NOW)[0][1]


def test_tokens_since(tmp_path):
    from transcripts import request_lines, write
    import copy
    from conftest import load_fixture
    from agentlens.adapters.claude_code import normalize

    path = write(tmp_path / "t.jsonl", request_lines("a", 1000.0, read=10, write=5, inp=1, out=7)
                 + request_lines("b", 2000.0, read=100, write=0, inp=2, out=9))
    p = copy.deepcopy(load_fixture("session_start"))
    p["transcript_path"] = str(path)
    spool.write_events(normalize(p, 900.0))
    ingest()
    conn = store.connect()
    assert store.tokens_since(conn, 1500.0) == {"requests": 1, "input": 102, "output": 9}
    assert store.tokens_since(conn, 0)["requests"] == 2
    conn.close()

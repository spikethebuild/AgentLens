from __future__ import annotations

import io
import json
import os
import stat
import statistics
import subprocess
import sys
import time

import pytest

from agentlens import hook, paths, spool
from agentlens.events import Event

PY = sys.executable


def run_hook(env, stdin: bytes, args=("hook", "claude-code")):
    return subprocess.run(
        [PY, "-m", "agentlens", *args], input=stdin, env=env,
        capture_output=True, timeout=30,
    )


def spooled():
    return [e for p in spool.pending() for e in spool.read_events(p)]


def test_real_subprocess_writes_redacted_spool(subprocess_env, fixture):
    payload = fixture("pre_tool_use_bash")
    secret = "gh" + "p_" + "Q" * 36
    payload["tool_input"]["command"] += f" --header 'x: {secret}'"
    r = run_hook(subprocess_env, json.dumps(payload).encode())
    assert r.returncode == 0 and r.stdout == b"" and r.stderr == b""
    files = spool.pending()
    assert len(files) == 1
    raw = files[0].read_text()
    assert secret not in raw and "fixture-not-a-secret-123" not in raw
    (ev,) = spool.read_events(files[0])
    assert ev.type == "tool.pre" and ev.tool_use_id == "toolu_01BashAAA"


def test_spool_permissions(subprocess_env, fixture):
    run_hook(subprocess_env, json.dumps(fixture("stop")).encode())
    (f,) = spool.pending()
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(paths.spool_dir()).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(paths.home()).st_mode) == 0o700


@pytest.mark.parametrize(
    "stdin, args",
    [
        (b"", ("hook", "claude-code")),
        (b"not json {{{", ("hook", "claude-code")),
        (b"[1,2,3]", ("hook", "claude-code")),
        (b'{"hook_event_name": "Stop"}', ("hook", "claude-code")),
        (b"\xff\xfe\x00garbage", ("hook", "claude-code")),
        (b'{"session_id":"s","hook_event_name":"Stop"}', ("hook", "nope")),
        (b'{"session_id":"s","hook_event_name":"Stop"}', ("hook",)),
    ],
)
def test_bad_input_always_exits_zero_and_logs(subprocess_env, stdin, args):
    r = run_hook(subprocess_env, stdin, args)
    assert r.returncode == 0 and r.stdout == b""
    assert spool.pending() == []
    assert (paths.logs_dir() / "agentlens.log").exists()


def test_unwritable_spool_still_exits_zero(subprocess_env, fixture, tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file, not a directory")
    env = dict(subprocess_env, AGENTLENS_HOME=str(blocker / "home"))
    r = run_hook(env, json.dumps(fixture("stop")).encode())
    assert r.returncode == 0 and r.stdout == b"" and r.stderr == b""


def test_session_end_spawns_detached_ingest(monkeypatch, fixture):
    calls = []
    monkeypatch.setattr(hook, "_spawn_ingest", lambda: calls.append(1))
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(fixture("session_end")).encode())))
    assert hook.run(["claude-code"]) == 0
    assert calls == [1]
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(fixture("stop")).encode())))
    hook.run(["claude-code"])
    assert calls == [1]


def test_spawn_failure_is_swallowed(monkeypatch, fixture):
    def boom():
        raise OSError("fork failed")

    monkeypatch.setattr(hook, "_spawn_ingest", boom)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(fixture("session_end")).encode())))
    assert hook.run(["claude-code"]) == 0
    assert len(spooled()) == 1  # event was spooled before the spawn


def test_atomic_write_leaves_no_tmp(subprocess_env, fixture):
    for name in ("session_start", "stop", "session_end"):
        run_hook(subprocess_env, json.dumps(fixture(name)).encode())
    names = [p.name for p in paths.spool_dir().iterdir() if p.is_file()]
    assert not any(n.startswith(".tmp-") for n in names)


def test_fast_path_does_not_import_sqlite(subprocess_env):
    code = (
        "import sys, io; sys.stdin = io.TextIOWrapper(io.BytesIO(b'{\"session_id\":\"s\",\"hook_event_name\":\"Stop\"}'));"
        "sys.argv=['agentlens','hook','claude-code'];"
        "import runpy\n"
        "try:\n runpy.run_module('agentlens', run_name='__main__')\n"
        "except SystemExit as e:\n assert e.code == 0, e.code\n"
        "heavy = [m for m in ('sqlite3','argparse','subprocess') if m in sys.modules]\n"
        "print(','.join(heavy))"
    )
    r = subprocess.run([PY, "-c", code], env=subprocess_env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == ""


def test_hook_latency(subprocess_env, fixture):
    body = json.dumps(fixture("post_tool_use_bash")).encode()
    times = []
    for _ in range(5):
        t = time.perf_counter()
        run_hook(subprocess_env, body)
        times.append(time.perf_counter() - t)
    median = statistics.median(times)
    print(f"\nhook wall time median: {median * 1000:.0f} ms")
    assert median < 1.0

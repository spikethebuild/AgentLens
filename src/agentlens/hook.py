"""Hook fast path: ``python -m agentlens hook <provider>``.

Read the payload from stdin, normalize and redact it, write one spool file,
exit 0. It never touches SQLite and never exits non-zero: Claude Code treats
exit 2 as "block", and AgentLens must never block the agent.

On SessionEnd it also starts a fully detached ``agentlens ingest``, because
SessionEnd hooks run under a short budget (1.5 s by default) and may be
cancelled. If that spawn is lost, the next CLI read ingests the spool anyway.
"""

from __future__ import annotations

import json
import os
import sys
import time

MAX_PAYLOAD_BYTES = 32 * 1024 * 1024

ADAPTERS = {"claude-code"}


def _spawn_ingest() -> None:
    import os
    import subprocess

    subprocess.Popen(
        [sys.executable, "-m", "agentlens", "ingest", "--quiet"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        env=os.environ.copy(),
    )


def handle(provider: str, raw: bytes, received_at: float) -> None:
    if provider not in ADAPTERS:
        raise ValueError(f"unknown provider {provider!r}")
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"payload too large ({len(raw)} bytes)")
    payload = json.loads(raw)

    from agentlens import spool
    from agentlens.adapters import claude_code

    events = claude_code.normalize(payload, received_at)
    # Which install fired this copy. Plugin and settings hooks both run if both are
    # installed (Claude Code does not merge them), so `status` reports the overlap.
    via = "plugin" if os.environ.get("AGENTLENS_VIA") == "plugin" else "settings"
    for ev in events:
        ev.data["collector"] = via
    spool.write_events(events)
    if any(e.type == "session.end" for e in events):
        _spawn_ingest()


def run(argv: list[str]) -> int:
    received_at = time.time()  # collector time: hooks carry no timestamp
    try:
        provider = argv[0] if argv else ""
        raw = sys.stdin.buffer.read(MAX_PAYLOAD_BYTES + 1)
        handle(provider, raw, received_at)
    except BaseException as exc:  # noqa: BLE001 - must never fail the agent
        try:
            from agentlens.errlog import log_error

            log_error(f"hook {argv!r} failed: {type(exc).__name__}", exc)
        except BaseException:  # noqa: BLE001
            pass
    return 0

"""Best-effort error log in $AGENTLENS_HOME/logs. Never raises."""

from __future__ import annotations

import os
import time
import traceback

from agentlens import paths

MAX_BYTES = 1_000_000


def log_error(context: str, exc: BaseException | None = None) -> None:
    try:
        from agentlens.redact import redact

        log_dir = paths.ensure_dir(paths.logs_dir())
        path = log_dir / "agentlens.log"
        try:
            if path.stat().st_size > MAX_BYTES:
                os.replace(path, log_dir / "agentlens.log.1")
        except FileNotFoundError:
            pass
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        detail = ""
        if exc is not None:
            detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        line = redact(f"{stamp} pid={os.getpid()} {context}\n{detail}")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8", "replace"))
        finally:
            os.close(fd)
    except Exception:  # noqa: BLE001 - logging must never break the caller
        pass

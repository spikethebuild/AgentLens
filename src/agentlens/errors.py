"""Deterministic one-line summaries of tool failure text.

``PostToolUseFailure.error`` carries the failed command's whole output (for Bash:
``Exit code N`` and then stdout/stderr), which can include file contents. AgentLens
keeps only a headline line chosen by fixed rules, plus the size of the original.
Standard library only: this runs on the hook path.
"""

from __future__ import annotations

import re

HEADLINE_LIMIT = 200

_EXIT_LINE = re.compile(r"^Exit code (\d+)\s*$")
# Tier 1: a line that *starts* like an error report.
_STARTS_LIKE_ERROR = re.compile(
    r"^(?:[\w.]*(?:Error|Exception)\b|error\b|ERROR\b|fatal\b|FATAL\b|panic\b|npm ERR!|E\s{2,}|"
    r"[\w./-]+: (?:error|fatal)\b)"
)
# Tier 2: a line that mentions a failure anywhere.
_MENTIONS_ERROR = re.compile(
    r"(?i)\b(?:error|failed|failure|not found|no such file|no module named|permission denied|"
    r"command not found|cannot|unable to)\b"
)


def _tidy(line: str) -> str:
    """Drop a leading JSON blob glued to a message, e.g. ``{...}gh: Not Found (HTTP 404)``."""
    line = line.strip()
    if line.startswith("{") and "}" in line:
        tail = line[line.rindex("}") + 1:].strip()
        if tail:
            return tail
    return line


def headline(text: str) -> str:
    """The most informative single line of an error text ('' if it has none)."""
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln and not _EXIT_LINE.match(ln)]
    if not lines:
        return ""
    for pattern in (_STARTS_LIKE_ERROR, _MENTIONS_ERROR):
        hits = [ln for ln in lines if pattern.search(_tidy(ln))]
        if hits:
            return _tidy(hits[-1])
    return _tidy(lines[-1])


def exit_code(text: str) -> int | None:
    first = text.lstrip().split("\n", 1)[0]
    m = _EXIT_LINE.match(first.strip())
    return int(m.group(1)) if m else None

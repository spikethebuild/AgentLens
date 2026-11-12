#!/bin/sh
# AgentLens launcher for Claude Code plugin hooks and skills.
#
#   run.sh hook claude-code   called by hooks/hooks.json; ALWAYS exits 0
#   run.sh <cli args>         e.g. `run.sh status --brief` from a skill
#
# Finds Python 3.10+ (AGENTLENS_PYTHON first), puts the plugin's bundled src/ on
# PYTHONPATH and runs `python -m agentlens`. The hook path needs only the
# standard library, so nothing has to be pip-installed. If no interpreter is
# found, a hook drains stdin, logs one line and exits 0: never block the agent.

root="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." 2>/dev/null && pwd)}"
home="${AGENTLENS_HOME:-$HOME/.agentlens}"

ok_python() {
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

find_python() {
    cache="$home/run/python"
    if [ -z "$AGENTLENS_PYTHON" ] && [ -r "$cache" ]; then
        cached=$(cat "$cache" 2>/dev/null)
        if [ -n "$cached" ] && [ -x "$cached" ]; then echo "$cached"; return 0; fi
    fi
    for c in "$AGENTLENS_PYTHON" python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
        [ -n "$c" ] || continue
        p=$(command -v "$c" 2>/dev/null) || continue
        # macOS: /usr/bin/python3 without the Command Line Tools pops an install dialog.
        if [ "$p" = /usr/bin/python3 ] && [ "$(uname -s)" = Darwin ] && ! xcode-select -p >/dev/null 2>&1; then
            continue
        fi
        if ok_python "$p"; then
            if [ -z "$AGENTLENS_PYTHON" ]; then
                (umask 077 && mkdir -p "$home/run" && printf '%s\n' "$p" > "$cache") 2>/dev/null
            fi
            echo "$p"
            return 0
        fi
    done
    return 1
}

py=$(find_python)

if [ "$1" = hook ]; then
    if [ -z "$py" ]; then
        cat >/dev/null 2>&1
        (umask 077 && mkdir -p "$home/logs" &&
            printf '%s ERROR plugin hook: no Python 3.10+ found (set AGENTLENS_PYTHON)\n' \
                "$(date '+%Y-%m-%dT%H:%M:%S')" >> "$home/logs/agentlens.log") 2>/dev/null
        exit 0
    fi
    AGENTLENS_VIA=plugin PYTHONPATH="$root/src${PYTHONPATH:+:$PYTHONPATH}" "$py" -s -m agentlens "$@" 2>/dev/null
    exit 0
fi

if [ -z "$py" ]; then
    echo "AgentLens: Python 3.10+ not found. Install it or set AGENTLENS_PYTHON."
    exit 0
fi
PYTHONPATH="$root/src${PYTHONPATH:+:$PYTHONPATH}" exec "$py" -s -m agentlens "$@"

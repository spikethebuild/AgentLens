"""User configuration in $AGENTLENS_HOME/config.json. Missing or broken config means defaults."""

from __future__ import annotations

import json
from typing import Any

from agentlens import paths

DEFAULTS: dict[str, Any] = {
    # Store (redacted, truncated) prompt text. Off by default: only length + keyed hash.
    "capture_prompts": False,
}


def load() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    try:
        raw = json.loads(paths.config_path().read_text())
    except (OSError, ValueError):
        return cfg
    if isinstance(raw, dict):
        for key, default in DEFAULTS.items():
            if key in raw and isinstance(raw[key], type(default)):
                cfg[key] = raw[key]
    return cfg

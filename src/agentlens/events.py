"""Normalized, provider-agnostic event schema.

Adapters turn provider payloads into ``Event`` objects. Everything downstream
(spool, ingest, store, CLI) only ever sees this shape.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

SCHEMA_VERSION = 1


class Provenance(str, Enum):
    """Where a value comes from. Every metric AgentLens shows carries one."""

    OBSERVED = "observed"  # reported by the provider
    CALCULATED = "calculated"  # derived deterministically by AgentLens
    ESTIMATED = "estimated"  # approximated by AgentLens


class Category(str, Enum):
    SESSION = "session"
    PROMPT = "prompt"
    TOOL = "tool"
    FILE = "file"
    USAGE = "usage"
    PERFORMANCE = "performance"
    ERROR = "error"
    TASK = "task"
    APPROVAL = "approval"


class TsSource(str, Enum):
    COLLECTOR = "collector"  # receive time measured by AgentLens (hooks carry no timestamp)
    PROVIDER = "provider"  # timestamp supplied by the provider


@dataclass(frozen=True)
class Event:
    event_id: str
    provider: str
    session_id: str
    project: str | None
    category: Category
    type: str
    ts: float
    ts_source: TsSource
    source: str
    provenance: Provenance
    tool_name: str | None = None
    tool_use_id: str | None = None
    status: str | None = None
    duration_ms: int | None = None
    data: dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["category"] = self.category.value
        d["provenance"] = self.provenance.value
        d["ts_source"] = self.ts_source.value
        return d

    @classmethod
    def from_dict(cls, d: Any) -> "Event":
        """Validate and rebuild an event (e.g. from a spool file). Raises ValueError."""
        if not isinstance(d, dict):
            raise ValueError("event must be an object")
        try:
            ev = cls(
                event_id=_req_str(d, "event_id"),
                provider=_req_str(d, "provider"),
                session_id=_req_str(d, "session_id"),
                project=_opt_str(d, "project"),
                category=Category(d["category"]),
                type=_req_str(d, "type"),
                ts=float(d["ts"]),
                ts_source=TsSource(d["ts_source"]),
                source=_req_str(d, "source"),
                provenance=Provenance(d["provenance"]),
                tool_name=_opt_str(d, "tool_name"),
                tool_use_id=_opt_str(d, "tool_use_id"),
                status=_opt_str(d, "status"),
                duration_ms=_opt_int(d, "duration_ms"),
                data=d.get("data") or {},
                schema_version=int(d.get("schema_version", SCHEMA_VERSION)),
            )
        except (KeyError, TypeError) as exc:
            raise ValueError(f"invalid event: {exc!r}") from exc
        if not isinstance(ev.data, dict):
            raise ValueError("event data must be an object")
        if len(ev.event_id) != 64:
            raise ValueError("event_id must be a sha256 hex digest")
        return ev


def _req_str(d: dict, key: str) -> str:
    v = d[key]
    if not isinstance(v, str) or not v:
        raise ValueError(f"{key} must be a non-empty string")
    return v


def _opt_str(d: dict, key: str) -> str | None:
    v = d.get(key)
    if v is None:
        return None
    if not isinstance(v, str):
        raise ValueError(f"{key} must be a string")
    return v


def _opt_int(d: dict, key: str) -> int | None:
    v = d.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f"{key} must be a number")
    return int(v)


def _sha256(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8", "surrogatepass"))
        h.update(b"\x1f")  # unit separator so ("ab","c") != ("a","bc")
    return h.hexdigest()


def tool_event_id(provider: str, session_id: str, event_name: str, tool_use_id: str) -> str:
    """Stable ID for a tool event: the same hook firing twice yields the same ID."""
    return _sha256(provider, session_id, event_name, tool_use_id)


def payload_event_id(provider: str, payload: dict, ts: float) -> str:
    """ID for non-tool events, computed in memory; only the digest is persisted.

    A payload carrying a ``prompt_id`` is already unique to one turn, so its ID is
    the canonical payload alone. Then the plugin's and the settings' copies of the
    same hook (Claude Code runs both) get one ID even when they are received in
    different seconds. Payloads without one (SessionStart, SessionEnd,
    InstructionsLoaded, ...) add the timestamp truncated to the second, so a real
    repeat later in the session is kept.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    if isinstance(payload.get("prompt_id"), str) and payload["prompt_id"]:
        return _sha256(provider, canonical)
    return _sha256(provider, canonical, str(int(ts)))

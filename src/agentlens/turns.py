"""Token use per prompt: which prompt led to the most tokens.

A turn is everything from one ``prompt.submit`` event to the next. Each API
request (main conversation and subagents) is attributed to the turn whose
window holds its timestamp. Attribution is by time, so it is ``[calc]``; the
token counts themselves are observed. Requests before the first recorded
prompt (a resumed session, a slash command that fires no UserPromptSubmit)
form their own turn with no prompt.

Prompt text is shown only when the user opted in (``capture_prompts``); by
default only its length is known. Stdlib only.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from agentlens import render, store


@dataclass
class Turn:
    index: int                      # 1-based prompt number; 0 = before the first prompt
    start: float
    end: float | None               # next prompt's time, None for the last turn
    prompt_event: str | None        # event_id of the prompt.submit, None for turn 0
    prompt_chars: int | None
    prompt_text: str | None         # only with capture_prompts
    requests: list[sqlite3.Row] = field(default_factory=list)       # main conversation
    sub_requests: list[sqlite3.Row] = field(default_factory=list)   # subagents
    tools: dict[str, int] = field(default_factory=dict)
    failures: int = 0

    @property
    def all_requests(self) -> list[sqlite3.Row]:
        return self.requests + self.sub_requests

    @property
    def tokens_in(self) -> int:
        """Input processed: uncached + cache read + cache write, all requests [obs sums]."""
        return sum(_ctx(r) or 0 for r in self.all_requests)

    @property
    def cache_write(self) -> int:
        return sum(r["cache_write_tokens"] or 0 for r in self.all_requests)

    @property
    def output(self) -> int:
        return sum(r["output_tokens"] or 0 for r in self.all_requests)

    @property
    def peak_context(self) -> int | None:
        sizes = [c for c in (_ctx(r) for r in self.requests) if c is not None]
        return max(sizes) if sizes else None

    def prompt_label(self, width: int = 60) -> str:
        if self.index == 0:
            return "(before the first recorded prompt)"
        if self.prompt_text:
            return render.clip(" ".join(self.prompt_text.split()), width)
        if self.prompt_chars is not None:
            return f"{self.prompt_chars} chars (text not stored)"
        return f"{render.UNAVAILABLE} (prompt length not recorded)"


def _ctx(r: sqlite3.Row) -> int | None:
    parts = (r["input_tokens"], r["cache_read_tokens"], r["cache_write_tokens"])
    return sum(parts) if all(v is not None for v in parts) else None


def session_turns(conn: sqlite3.Connection, provider: str, session_id: str,
                  events: list[sqlite3.Row] | None = None) -> list[Turn]:
    """Turns in time order. Turn 0 is kept only when it has requests or tool calls."""
    if events is None:
        events = store.session_events(conn, provider, session_id)
    events = sorted(events, key=lambda e: e["ts"])
    prompts = [e for e in events if e["type"] == "prompt.submit"]
    turns = [Turn(0, events[0]["ts"] if events else 0.0, prompts[0]["ts"] if prompts else None,
                  None, None, None)]
    for i, p in enumerate(prompts):
        data = render.parse_data(p["data"])
        chars = data.get("prompt_chars")
        text = data.get("prompt_text")
        turns.append(Turn(
            i + 1, p["ts"], prompts[i + 1]["ts"] if i + 1 < len(prompts) else None, p["event_id"],
            chars if isinstance(chars, int) else None, text if isinstance(text, str) and text else None,
        ))
    starts = [t.start for t in turns[1:]]

    def turn_at(ts: float) -> Turn:
        idx = 0
        for k, s in enumerate(starts):
            if ts >= s:
                idx = k + 1
            else:
                break
        return turns[idx]

    for r in store.session_requests(conn, provider, session_id, sidechain=None):
        t = turn_at(r["ts"])
        (t.sub_requests if r["is_sidechain"] else t.requests).append(r)
    for e in events:
        if e["type"] == "tool.pre" and e["tool_name"]:
            t = turn_at(e["ts"])
            t.tools[e["tool_name"]] = t.tools.get(e["tool_name"], 0) + 1
        elif e["type"] == "tool.failure":
            turn_at(e["ts"]).failures += 1
    if not turns[0].all_requests and not turns[0].tools:
        turns.pop(0)
    return turns

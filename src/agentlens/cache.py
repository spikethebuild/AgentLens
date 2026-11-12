"""Cache coach: where prompt cache was rebuilt in a session, why, and the fix.

Deterministic local analysis of recorded data. No LLM calls.

Inputs:
* ``api_requests``: per-request token usage read from the transcript
  (provider-reported numbers, so "observed"; the transcript format itself is
  undocumented, which reports say).
* hook events: compactions, model switches, instruction files loaded, config
  changes, resumes and the ``effort`` level, all documented hook fields.

Only the main conversation is analysed for cache misses (subagents keep their
own short-lived caches), as Claude Code's own ``/usage`` prompt-cache line does.

A **miss** reuses Claude Code's documented definition for its status line: a
request that read from cache more than 5% and at least 2,000 tokens less than
it could have. "Could have" = what the previous request left in the cache
(its cache reads + cache writes). Each miss is then attributed to the first
recorded explanation found between the two requests: compaction, model
switch, idle gap beyond the TTL, effort change. Anything else is reported as
an *unexplained* rebuild; tool/MCP changes have no hook, so AgentLens never
claims that cause.

Impact is reported in tokens. "Re-processed" tokens are calculated as
(expected cached - actually read). Dollars appear only as Claude Code's own
``estimated_cache_write_usd`` from a hook payload, labelled as its estimate.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from agentlens import render, store

MISS_MIN_TOKENS = 2000
MISS_MIN_FRACTION = 0.05
TTL_5M, TTL_1H = 300.0, 3600.0
LOW_HIT_RATIO = 0.70
LOW_HIT_MIN_REQUESTS = 10
INSTRUCTIONS_MAX_LINES = 200  # the costs docs: "aim to keep CLAUDE.md under 200 lines"
BYTES_PER_TOKEN_EST = 4       # rough English/markdown ratio; always labelled [est]
REPEATED_COMPACTIONS = 3
USER_SWITCH_SOURCES = {"command", "picker", "sdk"}
# Models whose cache survives an effort change (prompt-caching docs, 2026-11-12)
EFFORT_SAFE_PREFIXES = ("claude-fable-5", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5")

FIXES = {
    "model_switch": "Pick the model before you start, or switch right after /clear; each model has its own cache.",
    "compaction": "Use /clear between unrelated tasks; every compaction rewrites the cached conversation.",
    "idle": "Resume within the cache TTL; after a long break the whole conversation is re-cached.",
    "effort_change": "Set effort at the start of the session; changing it mid-session re-caches the conversation.",
    "unexplained": "Keep MCP servers and settings stable mid-session; with tool search on, new servers don't break the cache.",
    "instructions": "Trim CLAUDE.md to essentials; move rarely needed rules to .claude/rules/ with paths: so they load on demand.",
    "low_hit": "See the findings above for what broke the cache most.",
}


@dataclass
class Miss:
    request: sqlite3.Row
    prev: sqlite3.Row
    expected: int
    shortfall: int
    cause: str = "unexplained"
    ttl_s: float | None = None
    ttl_source: str = ""
    evidence: list[str] = field(default_factory=list)


@dataclass
class Finding:
    rule: str
    severity: str  # "warn" | "info"
    ts: float
    title: str
    impact: str
    impact_tokens: int | None
    evidence: list[str]
    fix: str


@dataclass
class Report:
    session: sqlite3.Row
    requests: list[sqlite3.Row]
    subagent_requests: int
    usage_status: str  # "" when usage data is available, else why not
    totals: dict
    hit_ratio: float | None
    ttl: str
    misses: list[Miss]
    findings: list[Finding]


# --------------------------------------------------------------- helpers


def _n(v: int | None) -> int:
    return v or 0


def _complete(r: sqlite3.Row) -> bool:
    return all(r[k] is not None for k in ("input_tokens", "cache_read_tokens", "cache_write_tokens"))


def fmt_tokens(n: int | None) -> str:
    if n is None:
        return render.UNAVAILABLE
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 10_000:
        return f"{n / 1000:.0f}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _short(event_or_req_id: str) -> str:
    return event_or_req_id[:12]


def _ttl_before(requests: list[sqlite3.Row], idx: int) -> tuple[float | None, str]:
    """TTL in effect for the cache the request at ``idx`` could read, from the
    most recent cache-write split before it (calculated from observed fields)."""
    for r in reversed(requests[:idx]):
        if _n(r["cache_write_1h_tokens"]) > 0:
            return TTL_1H, "1h cache writes"
        if _n(r["cache_write_5m_tokens"]) > 0:
            return TTL_5M, "5m cache writes"
    return None, ""


def _events_between(events: list[sqlite3.Row], t0: float, t1: float, types: set[str]) -> list[sqlite3.Row]:
    return [e for e in events if e["type"] in types and t0 <= e["ts"] <= t1]


def _effort_changed(events: list[sqlite3.Row], t0: float, t1: float) -> sqlite3.Row | None:
    """The first event in (t0, t1] whose effort level differs from the level before t0."""
    before = None
    for e in events:
        level = render.parse_data(e["data"]).get("effort")
        if not level:
            continue
        if e["ts"] <= t0:
            before = level
        elif e["ts"] <= t1 and before is not None and level != before:
            return e
    return None


# --------------------------------------------------------------- analysis


def find_misses(requests: list[sqlite3.Row], events: list[sqlite3.Row]) -> list[Miss]:
    misses: list[Miss] = []
    for i in range(1, len(requests)):
        prev, cur = requests[i - 1], requests[i]
        if not (_complete(prev) and _complete(cur)):
            continue
        expected = prev["cache_read_tokens"] + prev["cache_write_tokens"]
        shortfall = expected - cur["cache_read_tokens"]
        if shortfall < MISS_MIN_TOKENS or shortfall <= MISS_MIN_FRACTION * expected:
            continue
        m = Miss(cur, prev, expected, shortfall)
        t0, t1 = prev["ts"], cur["ts"]
        compact = _events_between(events, t0, t1, {"session.compact", "session.compacted"})
        switch = _events_between(events, t0, t1, {"model.switch"})
        if compact:
            m.cause = "compaction"
            m.evidence = [f"event {_short(e['event_id'])} ({e['type']})" for e in compact]
        elif switch or (prev["model"] and cur["model"] and prev["model"] != cur["model"]):
            m.cause = "model_switch"
            m.evidence = [f"event {_short(e['event_id'])} (PostModelSwitch)" for e in switch] or [
                f"model {prev['model']} → {cur['model']} in transcript"
            ]
        else:
            m.ttl_s, m.ttl_source = _ttl_before(requests, i)
            gap = t1 - t0
            if m.ttl_s is not None and gap > m.ttl_s:
                m.cause = "idle"
            elif m.ttl_s is None and gap > TTL_1H:
                m.cause, m.ttl_source = "idle", "gap exceeds the longest TTL"
            else:
                eff = _effort_changed(events, t0, t1)
                if eff is not None and not (cur["model"] or "").startswith(EFFORT_SAFE_PREFIXES):
                    m.cause = "effort_change"
                    m.evidence = [f"event {_short(eff['event_id'])} (effort {render.parse_data(eff['data']).get('effort')})"]
                else:
                    cfg = _events_between(events, t0, t1, {"config.change"})
                    m.evidence = [f"event {_short(e['event_id'])} (ConfigChange "
                                  f"{render.parse_data(e['data']).get('config_source', '?')}) nearby" for e in cfg]
        m.evidence = [f"request {_short(prev['request_id'])} → {_short(cur['request_id'])}"] + m.evidence
        misses.append(m)
    return misses


def _miss_impact(m: Miss) -> str:
    return (f"re-processed {fmt_tokens(m.shortfall)} tokens {render.CALC} "
            f"(cache written {fmt_tokens(m.request['cache_write_tokens'])} {render.OBS})")


def build_findings(session: sqlite3.Row, requests: list[sqlite3.Row], events: list[sqlite3.Row],
                   misses: list[Miss], hit_ratio: float | None, usage_ok: bool) -> list[Finding]:
    findings: list[Finding] = []
    explained_switch_events: set[str] = set()

    # model switches (hook-observed), with or without usage data
    for e in events:
        if e["type"] != "model.switch":
            continue
        d = render.parse_data(e["data"])
        if d.get("switch_source") not in USER_SWITCH_SOURCES:
            continue
        miss = next((m for m in misses if m.cause == "model_switch"
                     and m.prev["ts"] <= e["ts"] <= m.request["ts"]), None)
        bits = []
        if miss is not None:
            bits.append(_miss_impact(miss))
        elif not usage_ok:
            bits.append(f"tokens {render.UNAVAILABLE} (no usage data)")
        else:
            bits.append("no cache rebuild recorded after it")
        if isinstance(d.get("estimated_cache_write_usd"), (int, float)):
            bits.append(f"Claude Code estimate ${d['estimated_cache_write_usd']:.2f} {render.OBS}")
        if isinstance(d.get("context_tokens"), int):
            bits.append(f"context {fmt_tokens(d['context_tokens'])} {render.OBS}")
        evidence = [f"event {_short(e['event_id'])} (PostModelSwitch, {d.get('switch_source')})"]
        if miss is not None:
            evidence += miss.evidence[:1]
            explained_switch_events.add(e["event_id"])
        findings.append(Finding(
            "model_switch", "warn" if miss is not None or not usage_ok else "info", e["ts"],
            f"Model switch {d.get('from_model', '?')} → {d.get('to_model', '?')}",
            " · ".join(bits), miss.shortfall if miss else None, evidence, FIXES["model_switch"],
        ))

    for m in misses:
        if m.cause == "model_switch":
            if any(ev.startswith("event") for ev in m.evidence[1:]):
                continue  # reported above from the hook event
            title = f"Model changed {m.prev['model']} → {m.request['model']} (seen in transcript)"
            findings.append(Finding("model_switch", "warn", m.request["ts"], title, _miss_impact(m),
                                    m.shortfall, m.evidence, FIXES["model_switch"]))
        elif m.cause == "compaction":
            findings.append(Finding("compaction", "info", m.request["ts"], "Compaction rewrote the cache",
                                    _miss_impact(m), m.shortfall, m.evidence, FIXES["compaction"]))
        elif m.cause == "idle":
            gap = m.request["ts"] - m.prev["ts"]
            ttl = "unknown" if m.ttl_s is None else ("1h" if m.ttl_s == TTL_1H else "5m")
            title = f"Idle {render.fmt_duration_ms(gap * 1000)} > {ttl} cache TTL ({m.ttl_source}) {render.CALC}"
            findings.append(Finding("idle", "warn", m.request["ts"], title, _miss_impact(m),
                                    m.shortfall, m.evidence, FIXES["idle"]))
        elif m.cause == "effort_change":
            findings.append(Finding("effort_change", "warn", m.request["ts"], "Effort changed mid-session",
                                    _miss_impact(m), m.shortfall, m.evidence, FIXES["effort_change"]))
        else:
            findings.append(Finding(
                "unexplained", "warn", m.request["ts"],
                "Unexplained cache rebuild (no recorded compaction, model switch, idle gap or effort change)",
                _miss_impact(m) + " · cause unknown: tool/MCP or system-prompt changes are not reported by hooks",
                m.shortfall, m.evidence, FIXES["unexplained"],
            ))

    # resume after the cache expired (observed by Claude Code itself)
    for e in events:
        d = render.parse_data(e["data"])
        if e["type"] == "session.start" and d.get("start_source") in ("resume", "fork") and d.get("prompt_cache_likely_expired") is True:
            bits = [f"idle {render.fmt_duration_ms(d['seconds_since_last_response'] * 1000)} {render.OBS}"
                    if isinstance(d.get("seconds_since_last_response"), (int, float)) else ""]
            if isinstance(d.get("context_tokens"), int):
                bits.append(f"re-sends {fmt_tokens(d['context_tokens'])} tokens {render.OBS}")
            if isinstance(d.get("estimated_cache_write_usd"), (int, float)):
                bits.append(f"Claude Code estimate ${d['estimated_cache_write_usd']:.2f} {render.OBS}")
            findings.append(Finding("idle", "warn", e["ts"], "Resumed after the prompt cache expired",
                                    " · ".join(b for b in bits if b), d.get("context_tokens"),
                                    [f"event {_short(e['event_id'])} (SessionStart {d.get('start_source')})"],
                                    FIXES["idle"]))

    # repeated compaction without usage data to attribute it
    compactions = [e for e in events if e["type"] == "session.compacted"] or \
                  [e for e in events if e["type"] == "session.compact"]
    if len(compactions) >= REPEATED_COMPACTIONS:
        findings.append(Finding(
            "compaction", "warn", compactions[-1]["ts"], f"{len(compactions)} compactions in one session",
            f"each one rewrites the cached conversation {render.CALC}", None,
            [f"event {_short(e['event_id'])}" for e in compactions[:5]], FIXES["compaction"],
        ))
    elif compactions and not usage_ok:
        for e in compactions:
            findings.append(Finding("compaction", "info", e["ts"], "Compaction rewrote the cache",
                                    f"tokens {render.UNAVAILABLE} (no usage data)", None,
                                    [f"event {_short(e['event_id'])} ({e['type']})"], FIXES["compaction"]))

    # instruction files (CLAUDE.md, rules) loaded into the cached prefix
    loads = {}
    for e in events:
        if e["type"] != "instructions.loaded":
            continue
        d = render.parse_data(e["data"])
        if d.get("load_reason") not in ("session_start", "compact", "include") or not d.get("file_path"):
            continue
        loads[d["file_path"]] = (e, d)
    big = [(e, d) for e, d in loads.values()
           if isinstance(d.get("file_size"), dict) and _n(d["file_size"].get("lines")) > INSTRUCTIONS_MAX_LINES]
    if big:
        total_bytes = sum(_n(d["file_size"].get("bytes")) for _, d in loads.values() if isinstance(d.get("file_size"), dict))
        est = total_bytes // BYTES_PER_TOKEN_EST
        rebuilds = 1 + sum(1 for m in misses if m.cause != "unexplained") if usage_ok else None
        impact = f"≈{fmt_tokens(est)} tokens of instructions {render.EST} (bytes ÷ {BYTES_PER_TOKEN_EST})"
        if rebuilds:
            impact += f", written to cache {rebuilds}× this session {render.CALC}"
        names = ", ".join(f"{render.short_path(d['file_path'], 40)} ({d['file_size']['lines']} lines)" for _, d in big)
        findings.append(Finding(
            "instructions", "warn" if est >= 2000 else "info", min(e["ts"] for e, _ in big),
            f"Large instruction files: {names}", impact, est * rebuilds if rebuilds else None,
            [f"event {_short(e['event_id'])} (InstructionsLoaded)" for e, _ in big], FIXES["instructions"],
        ))

    # low cache-hit ratio, attributed
    if hit_ratio is not None and len(requests) >= LOW_HIT_MIN_REQUESTS and hit_ratio < LOW_HIT_RATIO:
        by_cause: dict[str, int] = {}
        for m in misses:
            by_cause[m.cause] = by_cause.get(m.cause, 0) + m.shortfall
        parts = ", ".join(f"{c.replace('_', ' ')} {fmt_tokens(t)}" for c, t in sorted(by_cause.items(), key=lambda x: -x[1]))
        findings.append(Finding(
            "low_hit", "warn", requests[-1]["ts"],
            f"Low cache-hit ratio {hit_ratio:.0%} (threshold {LOW_HIT_RATIO:.0%}) {render.CALC}",
            f"re-processed by cause: {parts or 'no individual misses; likely short or fast-changing prompts'} {render.CALC}",
            sum(by_cause.values()) or None, [f"{len(requests)} main-conversation requests"],
            FIXES[max(by_cause, key=by_cause.get)] if by_cause else FIXES["low_hit"],
        ))

    findings.sort(key=lambda f: f.ts)
    return findings


def analyze(conn: sqlite3.Connection, session: sqlite3.Row) -> Report:
    provider, sid = session["provider"], session["session_id"]
    requests = store.session_requests(conn, provider, sid, sidechain=False)
    sub = len(store.session_requests(conn, provider, sid, sidechain=True))
    events = store.session_events(conn, provider, sid)

    usage_status = ""
    if not requests:
        transcripts = [t for t in store.session_transcripts(conn, provider, sid) if not t["is_sidechain"]]
        if not transcripts:
            usage_status = "no transcript path recorded for this session"
        elif any(t["error"] for t in transcripts):
            usage_status = next(t["error"] for t in transcripts if t["error"])
        else:
            usage_status = "transcript has no usage entries (format may have changed in this Claude Code version)"

    complete = [r for r in requests if _complete(r)]
    totals = {
        "read": sum(r["cache_read_tokens"] for r in complete),
        "write": sum(r["cache_write_tokens"] for r in complete),
        "input": sum(r["input_tokens"] for r in complete),
        "output": sum(_n(r["output_tokens"]) for r in complete),
        "incomplete": len(requests) - len(complete),
    }
    denom = totals["read"] + totals["write"] + totals["input"]
    hit_ratio = totals["read"] / denom if denom else None
    w1h = sum(_n(r["cache_write_1h_tokens"]) for r in requests)
    w5m = sum(_n(r["cache_write_5m_tokens"]) for r in requests)
    ttl = "1h" if w1h and not w5m else "5m" if w5m and not w1h else "mixed" if w1h and w5m else render.UNAVAILABLE
    misses = find_misses(requests, events)
    findings = build_findings(session, requests, events, misses, hit_ratio, bool(requests))
    return Report(session, requests, sub, usage_status, totals, hit_ratio, ttl, misses, findings)


# --------------------------------------------------------------- rendering


def render_report(rep: Report, show_requests: bool = False) -> str:
    s = rep.session
    out = [f"Cache report · session {s['session_id']}"]
    models = sorted({r["model"] for r in rep.requests if r["model"]})
    if s["model"]:
        model = (s["model"], render.OBS)
    elif models:
        model = (", ".join(models) + " (from transcript)", render.OBS)
    else:
        model = (render.UNAVAILABLE, "")
    rows = [
        ("project", s["project"] or render.UNAVAILABLE, render.OBS if s["project"] else ""),
        ("model", *model),
    ]
    if rep.usage_status:
        rows.append(("usage", f"{render.UNAVAILABLE} ({rep.usage_status})", ""))
    else:
        t = rep.totals
        rows += [
            ("requests", f"{len(rep.requests)} main conversation"
                         + (f", {rep.subagent_requests} subagent" if rep.subagent_requests else ""), render.CALC),
            ("cache hit ratio", f"{rep.hit_ratio:.1%}" if rep.hit_ratio is not None else render.UNAVAILABLE, render.CALC),
            ("cache read", f"{fmt_tokens(t['read'])} tokens", render.OBS),
            ("cache written", f"{fmt_tokens(t['write'])} tokens", render.OBS),
            ("uncached input", f"{fmt_tokens(t['input'])} tokens", render.OBS),
            ("output", f"{fmt_tokens(t['output'])} tokens", render.OBS),
            ("cache TTL", rep.ttl + (" (from cache-write split)" if rep.ttl != render.UNAVAILABLE else ""), render.CALC),
            ("cache misses", f"{len(rep.misses)}"
                             + (f", {fmt_tokens(sum(m.shortfall for m in rep.misses))} tokens re-processed" if rep.misses else ""),
             render.CALC),
        ]
        if t["incomplete"]:
            rows.append(("incomplete records", f"{t['incomplete']} requests missing usage fields (excluded)", ""))
    out.append(render.kv(rows))
    out.append("  source: transcript usage fields (undocumented format; may change between Claude Code versions)"
               if not rep.usage_status else "")

    out.append("\nFindings")
    if not rep.findings:
        out.append("  none: no cache rebuilds or waste patterns recorded" if not rep.usage_status
                   else "  none from hook events; usage-based rules need transcript data")
    for f in rep.findings:
        mark = "✗" if f.severity == "warn" else "·"
        out.append(f"  {mark} {render.fmt_ts(f.ts, short=True)} [clk]  {f.title}")
        out.append(f"      impact    {f.impact}")
        out.append(f"      evidence  {'; '.join(f.evidence)}")
        out.append(f"      fix       {f.fix}")

    if show_requests and rep.requests:
        miss_ids = {m.request["request_id"]: m.cause for m in rep.misses}
        body = [[
            render.fmt_ts(r["ts"], short=True), r["request_id"][:14], r["model"] or "?",
            fmt_tokens(r["cache_read_tokens"]), fmt_tokens(r["cache_write_tokens"]),
            fmt_tokens(r["input_tokens"]), fmt_tokens(r["output_tokens"]),
            miss_ids.get(r["request_id"], ""),
        ] for r in rep.requests]
        out.append("\nRequests (main conversation)")
        out.append(render.table(["TIME [obs]", "REQUEST", "MODEL", "READ", "WRITE", "UNCACHED", "OUTPUT", "MISS"], body))
    out.append("\n" + render.LEGEND)
    return "\n".join(x for x in out if x is not None)


def render_brief(rep: Report, max_findings: int = 3) -> str:
    """At most five short lines: this text enters the model's context via the skill."""
    s = rep.session
    head = f"AgentLens cache · {s['session_id'][:8]}"
    if rep.usage_status:
        head += f" · usage {render.UNAVAILABLE} ({rep.usage_status[:60]})"
    else:
        hr = f"{rep.hit_ratio:.0%}" if rep.hit_ratio is not None else render.UNAVAILABLE
        head += f" · {len(rep.requests)} req · hit {hr} [calc] · {len(rep.misses)} misses"
    lines = [head]
    ranked = sorted(rep.findings, key=lambda f: (f.severity != "warn", -(f.impact_tokens or 0)))
    for f in ranked[:max_findings]:
        tok = f" ({fmt_tokens(f.impact_tokens)} tok)" if f.impact_tokens else ""
        lines.append(f"{'✗' if f.severity == 'warn' else '·'} {render.fmt_ts(f.ts, short=True)[6:]} "
                     f"{render.clip(f.title, 70)}{tok}")
    if ranked:
        lines.append(f"fix: {render.clip(ranked[0].fix, 110)}")
    else:
        lines.append("no cache waste found" if not rep.usage_status else "no hook-based findings")
    return "\n".join(lines[:5])

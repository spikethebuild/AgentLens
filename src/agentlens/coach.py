"""Cost coach: deterministic suggestions to waste fewer tokens, each with its evidence.

Rules (all local, no model calls; thresholds are constants below):
  long_context     sessions that kept running far above LONG_CONTEXT_TOKENS
  start_context    how big every session already is on its first request
  plugin_overhead  enabled plugins whose skill/agent/command listing is large
                   but that were never used in the sessions analysed
  large_output     Bash calls whose output flooded the context
  cache:*          the cache report's warnings for each session (cache.py)

Where an enabled plugin ships a skill aimed at the same problem (for example
ECC's ``strategic-compact``), the fix names it. That is optional: no plugin is
required and AgentLens never runs one.
"""

from __future__ import annotations

import os
import sqlite3
import statistics
import time
from dataclasses import dataclass
from pathlib import Path

from agentlens import cache, inventory, render, store
from agentlens.cache import Finding

fmt_tokens = render.fmt_tokens

DEFAULT_DAYS = 14
LONG_CONTEXT_TOKENS = 200_000
LONG_CONTEXT_MIN_REQUESTS = 10
LONG_CONTEXT_MIN_SHARE = 0.30
LONG_CONTEXT_SHOWN = 3          # sessions listed one by one; the rest are summed in one finding
START_CONTEXT_WARN = 50_000
PLUGIN_LISTING_WARN = 2_000      # estimated tokens
PLUGIN_UNUSED_MIN_SESSIONS = 3
LARGE_OUTPUT_CHARS = 20_000
CHARS_PER_TOKEN_EST = 4

# A rule's problem -> plugin skills that address it, if such a plugin is enabled.
RELATED_SKILLS = {
    "long_context": ("strategic-compact",),
    "start_context": ("context-budget",),
}


@dataclass
class SkillUse:
    name: str          # as invoked, e.g. "ecc:strategic-compact"
    count: int
    growth: list[int]  # context growth across each call, tokens [calc]


@dataclass
class CoachReport:
    scope: str
    sessions: list[sqlite3.Row]
    requests: int
    plugins: list[inventory.PluginInfo]
    skill_uses: dict[str, SkillUse]
    findings: list[Finding]


def _same_dir(a: str | None, b: str) -> bool:
    return bool(a) and os.path.realpath(a) == os.path.realpath(b)


def _ctx(r: sqlite3.Row) -> int:
    return (r["input_tokens"] or 0) + (r["cache_read_tokens"] or 0) + (r["cache_write_tokens"] or 0)


def _related(plugins: list[inventory.PluginInfo], rule: str) -> str:
    for skill in RELATED_SKILLS.get(rule, ()):
        cmd = inventory.find_skill(plugins, skill)
        if cmd:
            return f" Installed plugin skill: {cmd}."
    return ""


# ------------------------------------------------------------------ rules


def long_context(s: sqlite3.Row, reqs: list[sqlite3.Row], plugins) -> Finding | None:
    big = [r for r in reqs if _ctx(r) >= LONG_CONTEXT_TOKENS]
    total_read = sum(r["cache_read_tokens"] or 0 for r in reqs)
    if len(big) < LONG_CONTEXT_MIN_REQUESTS or not total_read:
        return None
    big_read = sum(r["cache_read_tokens"] or 0 for r in big)
    share = big_read / total_read
    if share < LONG_CONTEXT_MIN_SHARE:
        return None
    first = big[0]
    peak = max(big, key=_ctx)
    return Finding(
        "long_context", "warn", first["ts"],
        f"Session {s['session_id'][:8]} ran {len(big)} of {len(reqs)} requests above "
        f"{fmt_tokens(LONG_CONTEXT_TOKENS)} context (peak {fmt_tokens(_ctx(peak))}) {render.CALC}",
        f"those requests re-read {fmt_tokens(big_read)} of {fmt_tokens(total_read)} cache-read tokens "
        f"({share:.0%}) {render.CALC}",
        big_read,
        [f"session {s['session_id'][:8]}", f"first above: request {first['request_id'][:14]} "
         f"at {render.fmt_ts(first['ts'], short=True)}", f"peak: request {peak['request_id'][:14]}"],
        "Every tool call re-reads the whole conversation. /compact at the next task boundary, or start a "
        "new session for an unrelated task." + _related(plugins, "long_context"),
    )


def start_context(firsts: list[tuple[sqlite3.Row, sqlite3.Row]], plugins) -> Finding | None:
    if not firsts:
        return None
    sizes = [_ctx(r) for _, r in firsts]
    med = int(statistics.median(sizes))
    if med < START_CONTEXT_WARN:
        return None
    heavy = sorted(((p.listing_tokens_est or 0, p.name) for p in plugins), reverse=True)
    top = ", ".join(f"{name} ≈{fmt_tokens(t)}" for t, name in heavy[:3] if t >= PLUGIN_LISTING_WARN)
    return Finding(
        "start_context", "warn", firsts[-1][1]["ts"],
        f"Sessions start at {fmt_tokens(med)} tokens of context (median first request, "
        f"{len(firsts)} sessions) {render.OBS}",
        f"re-read on every request of every session; range {fmt_tokens(min(sizes))}–{fmt_tokens(max(sizes))} "
        f"{render.OBS}" + (f" · largest plugin listings: {top} {render.EST}" if top else ""),
        None,
        [f"session {s['session_id'][:8]}: request {r['request_id'][:14]} {fmt_tokens(_ctx(r))}"
         for s, r in firsts[-5:]],
        "Run /context to see what fills it. Disable plugins you don't use in this project (/plugin) and "
        "trim CLAUDE.md." + _related(plugins, "start_context"),
    )


def plugin_overhead(plugins, uses: dict[str, SkillUse], n_sessions: int, agent_uses: dict[str, int],
                    first_ts: float) -> list[Finding]:
    out = []
    for p in plugins:
        est = p.listing_tokens_est
        if est is None or est < PLUGIN_LISTING_WARN:
            continue
        prefix, mcp_prefix = p.name + ":", f"mcp__plugin_{p.name}_"
        skill_calls = sum(u.count for k, u in uses.items() if k.startswith(prefix))
        other_calls = sum(n for k, n in agent_uses.items() if k.startswith((prefix, mcp_prefix)))
        if skill_calls or other_calls or n_sessions < PLUGIN_UNUSED_MIN_SESSIONS:
            continue
        listed = f"{len(p.skills)} skills, {len(p.agents)} agents, {len(p.commands)} commands"
        hooks_note = (f" · its {p.hook_handlers} hook handlers still run every session; what they are worth "
                      f"to you is not measured" if p.hook_handlers else "")
        out.append(Finding(
            "plugin_overhead", "warn", first_ts,
            f"Plugin {p.name}: ≈{fmt_tokens(est)} tokens of skills/agents/commands listed in every session, "
            f"none called in {n_sessions} sessions",
            f"{listed} visible to the model {render.EST} (upper bound: Claude Code shortens long listings)"
            + hooks_note,
            None,
            [f"0 Skill, Agent or MCP tool calls naming '{p.name}' in {n_sessions} sessions {render.OBS}",
             f"files: {render.short_path(str(p.install_path), 50)}"],
            f"If you rely only on its hooks, install just those (or disable {p.key} for this project in "
            "/plugin). Slash commands you type are not recorded, so check those first.",
        ))
    return out


def large_output(rows: list[tuple[int, str]], first_ts: float) -> Finding | None:
    if not rows:
        return None
    total = sum(chars for chars, _ in rows)
    rows = sorted(rows, key=lambda x: -x[0])
    return Finding(
        "large_output", "warn" if len(rows) >= 3 else "info", first_ts,
        f"{len(rows)} Bash calls returned ≥{LARGE_OUTPUT_CHARS // 1000}k characters {render.OBS}",
        f"≈{fmt_tokens(total // CHARS_PER_TOKEN_EST)} tokens added to context {render.EST} (chars ÷ "
        f"{CHARS_PER_TOKEN_EST}), then re-read by every later request",
        total // CHARS_PER_TOKEN_EST,
        [f"{chars // 1000}k chars: {render.clip(cmd, 60)}" for chars, cmd in rows[:3]],
        "Ask for narrower output: pipe through head, tail or grep, use quiet flags, or limit test output.",
    )


def _fold(findings: list[Finding]) -> list[Finding]:
    """Per rule, list the LONG_CONTEXT_SHOWN biggest findings and sum the rest into one."""
    by_rule: dict[str, list[Finding]] = {}
    for f in findings:
        by_rule.setdefault(f.rule, []).append(f)
    out: list[Finding] = []
    for rule, group in by_rule.items():
        group.sort(key=lambda f: -(f.impact_tokens or 0))
        out += group[:LONG_CONTEXT_SHOWN]
        rest = group[LONG_CONTEXT_SHOWN:]
        if not rest:
            continue
        total = sum(f.impact_tokens or 0 for f in rest)
        label = "ran long above " + fmt_tokens(LONG_CONTEXT_TOKENS) + " context" if rule == "long_context" \
            else f"had the same finding ({rule})"
        out.append(Finding(
            rule, "warn" if any(f.severity == "warn" for f in rest) else "info", rest[0].ts,
            f"{len(rest)} more sessions {label} {render.CALC}",
            (f"{fmt_tokens(total)} tokens in total {render.CALC}" if total else "see each session's report"),
            None,  # ranked after the sessions it summarises; the total is in the text
            [f.evidence[0] for f in rest[:8]] + ([f"… {len(rest) - 8} more"] if len(rest) > 8 else []),
            rest[0].fix,
        ))
    return out


# ------------------------------------------------------------------ analysis


def _skill_uses(events: list[sqlite3.Row], reqs: list[sqlite3.Row], uses: dict[str, SkillUse],
                agent_uses: dict[str, int]) -> None:
    for e in events:
        if e["type"] != "tool.pre":
            continue
        d = render.parse_data(e["data"]).get("input") or {}
        if e["tool_name"] == "Skill" and isinstance(d.get("skill"), str):
            name = d["skill"]
            u = uses.setdefault(name, SkillUse(name, 0, []))
            u.count += 1
            before = [r for r in reqs if r["ts"] <= e["ts"]]
            after = [r for r in reqs if r["ts"] > e["ts"]]
            if before and after:
                u.growth.append(_ctx(after[0]) - _ctx(before[-1]))
        elif e["tool_name"] in ("Agent", "Task") and isinstance(d.get("subagent_type"), str):
            agent_uses[d["subagent_type"]] = agent_uses.get(d["subagent_type"], 0) + 1
        elif (e["tool_name"] or "").startswith("mcp__plugin_"):
            # a plugin's MCP tools are named mcp__plugin_<plugin>_<server>__<tool>
            agent_uses[e["tool_name"]] = agent_uses.get(e["tool_name"], 0) + 1


def analyze(conn: sqlite3.Connection, project: str | None, days: int = DEFAULT_DAYS,
            session_prefix: str | None = None, now: float | None = None,
            plugins: list[inventory.PluginInfo] | None = None) -> CoachReport:
    now = time.time() if now is None else now
    if session_prefix:
        s = store.find_session(conn, session_prefix)
        sessions = [s] if s is not None else []
        scope = f"session {s['session_id'][:8]}" if s else f"session {session_prefix} (not found)"
    else:
        since = now - days * 86400
        sessions = [s for s in store.list_sessions(conn, 10_000)
                    if s["last_seen"] >= since and (project is None or _same_dir(s["project"], project))]
        sessions.sort(key=lambda s: s["first_seen"])
        scope = f"last {days} days · " + (render.short_path(project, 50) if project else "all projects")
    if plugins is None:
        plugins = inventory.inventory(Path(project) if project else None)

    findings: list[Finding] = []
    firsts: list[tuple[sqlite3.Row, sqlite3.Row]] = []
    uses: dict[str, SkillUse] = {}
    agent_uses: dict[str, int] = {}
    outputs: list[tuple[int, str]] = []
    n_requests = 0
    for s in sessions:
        provider, sid = s["provider"], s["session_id"]
        reqs = [r for r in store.session_requests(conn, provider, sid, sidechain=False)
                if r["cache_read_tokens"] is not None]
        events = store.session_events(conn, provider, sid)
        n_requests += len(reqs)
        if reqs:
            firsts.append((s, reqs[0]))
            f = long_context(s, reqs, plugins)
            if f:
                findings.append(f)
        _skill_uses(events, reqs, uses, agent_uses)
        commands = {e["tool_use_id"]: (render.parse_data(e["data"]).get("input") or {}).get("command", "")
                    for e in events if e["type"] == "tool.pre" and e["tool_name"] == "Bash"}
        for e in events:
            if e["type"] == "tool.post" and e["tool_name"] == "Bash":
                resp = render.parse_data(e["data"]).get("response") or {}
                chars = sum(v for k, v in resp.items() if k in ("stdout_chars", "stderr_chars") and isinstance(v, int))
                if chars >= LARGE_OUTPUT_CHARS:
                    outputs.append((chars, str(commands.get(e["tool_use_id"], "") or "(command not recorded)")))
        for cf in cache.analyze(conn, s).findings:
            if cf.severity == "warn" and cf.rule not in ("low_hit",):
                cf.rule = "cache:" + cf.rule
                cf.evidence = [f"session {sid[:8]}"] + cf.evidence
                findings.append(cf)

    first_ts = sessions[0]["first_seen"] if sessions else now
    f = start_context(firsts, plugins)
    if f:
        findings.append(f)
    findings += plugin_overhead(plugins, uses, len(sessions), agent_uses, first_ts)
    f = large_output(outputs, first_ts)
    if f:
        findings.append(f)
    findings = _fold(findings)
    findings.sort(key=lambda f: (f.severity != "warn", -(f.impact_tokens or 0), f.ts))
    return CoachReport(scope, sessions, n_requests, plugins, uses, findings)


# ------------------------------------------------------------------ rendering


def render_report(rep: CoachReport) -> str:
    out = [f"Cost coach · {rep.scope}",
           render.kv([
               ("sessions", str(len(rep.sessions)), render.CALC),
               ("API requests", f"{rep.requests} main conversation", render.CALC),
               ("plugins enabled", ", ".join(p.name for p in rep.plugins) or "none", render.OBS if rep.plugins else ""),
           ])]
    out.append("\nSuggestions")
    if not rep.sessions:
        out.append("  none: no sessions in scope (try --all-projects or --days)")
    elif not rep.findings:
        out.append("  none: no waste patterns found in these sessions")
    for f in rep.findings:
        mark = "✗" if f.severity == "warn" else "·"
        out.append(f"  {mark} {f.title}")
        out.append(f"      impact    {f.impact}")
        out.append(f"      evidence  {'; '.join(f.evidence)}")
        out.append(f"      fix       {f.fix}")

    used = sorted((u for u in rep.skill_uses.values() if ":" in u.name), key=lambda u: -u.count)
    if used:
        out.append("\nPlugin skills used")
        for u in used:
            growth = (f", context +{fmt_tokens(int(statistics.median(u.growth)))} per call (median) {render.CALC}"
                      if u.growth else "")
            out.append(f"  {u.name} ×{u.count}{growth}")
    out.append("\nThresholds: long context ≥" + fmt_tokens(LONG_CONTEXT_TOKENS)
               + f", start ≥{fmt_tokens(START_CONTEXT_WARN)}, plugin listing ≥{fmt_tokens(PLUGIN_LISTING_WARN)} "
               f"{render.EST}, Bash output ≥{LARGE_OUTPUT_CHARS // 1000}k chars.")
    out.append("Token counts come from transcript usage fields (undocumented format; may change between versions).")
    out.append("\n" + render.LEGEND)
    return "\n".join(out)


def render_brief(rep: CoachReport, max_findings: int = 3) -> str:
    """At most five short lines: this text enters the model's context via the skill."""
    lines = [f"AgentLens coach · {len(rep.sessions)} sessions · {rep.requests} req · "
             f"{len(rep.findings)} suggestions"]
    for f in rep.findings[:max_findings]:
        lines.append(f"{'✗' if f.severity == 'warn' else '·'} {render.clip(f.title, 100)}")
    if rep.findings:
        lines.append(f"fix: {render.clip(rep.findings[0].fix, 120)}")
    else:
        lines.append("no waste patterns found")
    return "\n".join(lines[:5])

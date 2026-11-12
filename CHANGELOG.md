# Changelog

All notable changes to AgentLens are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow the
milestones in `CLAUDE.md` (see "Versioning").

## [Unreleased]

Nothing has been published yet. The first release will be 0.1.1 and will contain
everything below.

### Added
- Hook collector for Claude Code: 18 hook events (sessions, prompts, tools,
  approvals, subagents, turn failures, compaction, model switches, instruction
  files loaded, config changes), normalized with provenance, redacted before
  anything is written, async and always exiting 0.
- Spool → ingest → SQLite pipeline with a file lock and event-ID deduplication.
- CLI: `install`, `uninstall`, `status [--brief]`, `sessions`, `inspect`,
  `ingest`, `cache`, `dashboard`.
- Per-request token usage read incrementally from session transcripts (byte
  offset per file; each API request counted once; counts only, never content).
- Cache report (`agentlens cache`): cache misses attributed to compaction, model
  switch, idle gap beyond the TTL or effort change, with evidence and fixes.
- Claude Code plugin packaging (built ahead of its v0.2.1 milestone):
  `.claude-plugin/plugin.json`, `marketplace.json`, `hooks/hooks.json`,
  `hooks/run.sh`, `/agentlens:status` and `/agentlens:cache` skills.
- Early Textual dashboard (`agentlens` with no arguments, optional `tui` extra).

### Changed
- Roadmap revised (second revision): v0.1.x Release → v0.2 TUI, then Agent
  Replay → v0.2.1 plugin → v0.3 Analytics, then Cost Coach → v0.4 Insights,
  then CLAUDE.md Autotuner → v0.5 Codex, then Cursor.
- New rule: every suggestion AgentLens shows cites the sessions and events it is
  based on and shows its provenance.

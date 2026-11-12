# Changelog

All notable changes to AgentLens are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow the
milestones in `CLAUDE.md` (see "Versioning").

## [0.1.1] - Unreleased

First public release, published to PyPI as `agentlens-local`.

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
- Packaging and release automation: PyPI distribution `agentlens-local` with a
  single version source, CI on Ubuntu and macOS (Python 3.10–3.14, plus a
  standard-library-only job), a tag-triggered release workflow using Trusted
  Publishing with a TestPyPI rehearsal, `scripts/release_tools.py` and
  `RELEASING.md`.
- `agentlens install|uninstall|status --scope user|project|local`. Project scope
  warns that the hook names this machine's interpreter; local scope warns when
  git does not ignore `.claude/settings.local.json`.
- `--dry-run` for `install` and `uninstall`: prints a unified diff, writes nothing.
- `agentlens install --repair`: normalizes AgentLens's own entries (duplicates,
  edited fields, stale interpreter, events it no longer registers).
- `agentlens doctor`: read-only checks with a fix per problem, including an
  end-to-end hook test against a temporary data directory; exits 1 on failure.
- Per-session token totals (API requests, input with cache read/written/uncached,
  output, cache hit ratio) in `inspect`, a TOKENS IN/OUT column in `sessions`, and
  the dashboard (session list column, "Tokens in" tile, overview).

### Changed
- Version reset from the unpublished 0.3.0 to 0.1.1 to follow the milestone labels.
- Roadmap revised (second revision): v0.1.x Release → v0.2 TUI, then Agent
  Replay → v0.2.1 plugin → v0.3 Analytics, then Cost Coach → v0.4 Insights,
  then CLAUDE.md Autotuner → v0.5 Codex, then Cursor.
- New rule: every suggestion AgentLens shows cites the sessions and events it is
  based on and shows its provenance.

### Fixed
- The user settings path now follows Claude Code's `CLAUDE_CONFIG_DIR`.
- `inspect` no longer says tokens are unavailable when transcript usage exists.

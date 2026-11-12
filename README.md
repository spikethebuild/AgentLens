# AgentLens

**Find out where your coding agent's tokens and cache went, and how to waste less.**

AgentLens is a local-first observability tool for AI coding agents. It records what Claude Code does (sessions, prompts, tool calls, failures, approvals, compactions, model switches, instruction files loaded) through Claude Code's official hooks and stores it in a local SQLite database. It makes **zero LLM calls**, never blocks the agent, and never stores file contents, prompt text or secrets.

> Status: **v0.2 plugin packaging**. Recording, the CLI and a terminal dashboard work. The cache coach (v0.3: where prompt cache was rebuilt, why, and the fix) is next. Codex support is planned for v0.5.

## Install

Requires Python 3.10+ on macOS or Linux. Recording needs only the standard library, so nothing is pip-installed for the plugin.

### As a Claude Code plugin (recommended)

In Claude Code:

```
/plugin marketplace add SiddharthDevare10/AgentLens
/plugin install agentlens@agentlens
```

Then run `/reload-plugins` or start a new session. Every session is recorded from then on. `/agentlens:status` prints a short health check.

The plugin's hooks run `hooks/run.sh`, which finds Python 3.10+ (set `AGENTLENS_PYTHON` to choose one), runs the bundled collector and **always exits 0**. If no Python is found, it logs one line to `~/.agentlens/logs/agentlens.log` and does nothing else. Recorded data lives in `~/.agentlens` (or `AGENTLENS_HOME`), so it survives uninstalling or updating the plugin.

### Fallback: settings hooks

For the CLI and dashboard in your own terminal, or if you don't use plugins:

```bash
git clone https://github.com/SiddharthDevare10/AgentLens && cd AgentLens
python3 -m venv .venv && .venv/bin/pip install -e '.[tui]'   # or -e . for CLI only
.venv/bin/agentlens status
.venv/bin/agentlens install      # only if you are NOT using the plugin
```

`install` adds hooks to `~/.claude/settings.json` (backup first, atomic write, idempotent). It records the absolute path of the interpreter you run it with; if you move that virtualenv, `agentlens status` reports it as `MISSING`.

**Don't use both.** Claude Code runs plugin hooks and settings hooks side by side, so every event would fire twice. `agentlens install` refuses while the plugin is enabled (override with `--force`), and `agentlens status` warns if both are active. To switch to the plugin, run `agentlens uninstall`.

## Dashboard

Run `agentlens` with no arguments (or `agentlens dashboard`) to open the terminal dashboard.

- **Home:** headline tiles (sessions, open now, tool calls, failed, failure rate, average tool time) for the selected project. Below them are your projects, every recorded session (open or ended, newest first) and a live activity feed across all sessions. `/` opens a filter (ID, project, model or state), `f` shows only sessions with failed tool calls, `Enter` opens a session.
- **Session:** tiles for duration, tool calls, failures, prompts and total tool time. It opens on **Errors** when the session has any. Each failed call shows its full sanitized command, exit code, status, duration and complete error message. The other tabs are **Tool calls** (select a call to see everything recorded about it), **Timeline** and **Overview**.
- **Live:** while the dashboard is open it drains the spool every second and appends new events as they happen. `p` pauses, `r` syncs now, `?` shows the provenance legend, `Esc` goes back, `q` quits.

Every value carries the same `[obs]`/`[calc]`/`[est]`/`[clk]` labels as the CLI. The dashboard never edits Claude Code settings and makes no network calls. Sessions stay in the database after Claude Code exits, so you can review them at any time.

## Commands

| Command | What it does |
|---|---|
| `agentlens install [--settings PATH] [--force]` | Fallback to the plugin. Adds async hooks for 18 Claude Code events (the same set the plugin registers). Backs up `settings.json` first and writes it atomically. Running it again changes nothing. Refuses to touch a settings file that is not valid JSON. |
| `agentlens uninstall [--settings PATH]` | Removes only handlers whose command contains `agentlens hook`. Keeps recorded data. |
| `agentlens status [--brief]` | Plugin and settings-hook installation, interpreter health, spool and database state, and warnings (double install, `disableAllHooks`). `--brief` prints at most four lines. |
| `agentlens sessions [--limit N]` | Recent sessions: start, duration, state, prompts, tool calls, failures. |
| `agentlens inspect <id-or-prefix> [--all]` | One session in detail: overview, tool calls, errors, timeline. |
| `agentlens` / `agentlens dashboard` | Opens the terminal dashboard (needs the `tui` extra). Without a terminal, prints help. |
| `agentlens ingest` | Moves spooled events into the database. Runs automatically before every read. |

## Where the numbers come from

Every metric carries a label:

| Label | Meaning |
|---|---|
| `[obs]` | Reported by Claude Code, such as the model, `duration_ms` on a tool result, or the end reason. |
| `[calc]` | Derived by AgentLens from collected events, such as counts, session duration, or tool duration paired from Pre/Post events. |
| `[est]` | Estimated. Nothing in v0.1 is estimated. |
| `[clk]` | A timestamp from the collector clock. Hook payloads carry no timestamp, so AgentLens records when it received each hook. |

A value that cannot be known is shown as `unavailable` with the reason. It is never guessed. For example:

- **Token usage and cost** are not in hook payloads. They are planned for v0.3.
- **Session duration** is unavailable when AgentLens missed `SessionStart`, for example if it was installed mid-session.
- **Tool duration** is `[obs]` when Claude Code sends `duration_ms`. Otherwise it is `[calc]` from the Pre/Post receive times. That value includes hook process start-up jitter of tens of milliseconds. If the Post event happened to be stamped before the Pre event, the duration is shown as unavailable.

## How it works

```
Claude Code hook (async: true)
  -> python -m agentlens hook claude-code   # read stdin, normalize, redact (~30 ms)
  -> ~/.agentlens/spool/<ns>-<pid>-<rand>.json   # atomic write, 0600 file in 0700 dir
  -> ingest                                  # on every CLI read, and detached after SessionEnd
  -> ~/.agentlens/agentlens.db               # SQLite WAL, deduplicated, one transaction per batch
```

- **The hook never touches SQLite and always exits 0**, even on internal errors. Errors go to `~/.agentlens/logs/agentlens.log`.
- **SessionEnd hooks run under a 1.5-second budget**, so the hook only spools the event and starts a fully detached `agentlens ingest`. If that ingest is lost, the next `agentlens` command ingests the spool.
- **Event IDs are deterministic**, so a hook installed at both user and project level, or a re-ingested spool file, is stored once.
- **A spool file that cannot be parsed** moves to `spool/failed/` with a `.reason` file next to it.
- **No daemon and no network access.**

## Privacy

What is stored, per event:

- **Bash:** the command, redacted and capped at 500 characters.
- **Write/Edit/Read:** the file path and sizes. Never the contents.
- **Other tools:** short scalar arguments, redacted. Long text is reduced to its length, nested data to item counts.
- **Tool output:** booleans, numbers and text lengths only.
- **Prompts:** the length and a keyed HMAC (the salt is in `~/.agentlens/salt`). The text itself is not stored. To store redacted, truncated prompt text, opt in with `{"capture_prompts": true}` in `~/.agentlens/config.json`.
- **Assistant messages:** the length only.

Redaction runs before anything is written to disk, including the spool. It covers:

- `sk-…`, `sk-ant-…`, `ghp_…`/`gho_…`/…, `github_pat_…`, `AKIA…`/`ASIA…`, `xox?-…`, `AIza…`, JWTs.
- Bearer and `Authorization:` headers, and `user:pass@` in URLs.
- Secret-named `KEY=value` / `"key": "value"` pairs, and `--password`/`--token`/`--api-key` flags.
- Private key blocks.

Set `AGENTLENS_HOME` to keep all data somewhere other than `~/.agentlens`.

## Try it without installing hooks

```bash
./examples/demo_session.sh
```

This pipes a scripted session through the real hook command into a throwaway `AGENTLENS_HOME`, prints `sessions` and `inspect`, and then greps everything written to disk to confirm that the demo secrets are absent.

## Development

```bash
uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/pytest -q
```

Tests run with temporary `HOME`, `AGENTLENS_HOME` and `AGENTLENS_CLAUDE_SETTINGS`. A fixture fails any test whose paths resolve outside the sandbox. See `CLAUDE.md` for the full spec and the working rules.

## License

MIT

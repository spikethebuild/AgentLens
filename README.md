# AgentLens

**Find out where your coding agent's tokens and cache went, and how to waste less.**

AgentLens is a local-first observability tool for AI coding agents. It records what Claude Code does (sessions, prompts, tool calls, failures, approvals, compactions, model switches, instruction files loaded) through Claude Code's official hooks and stores it in a local SQLite database. It makes **zero LLM calls**, never blocks the agent, and never stores file contents, prompt text or secrets.

> Status: **0.1.1, first release (alpha)**. Recording (plugin or settings hooks), per-request token usage, the cache coach, plan usage in the status line, the `/agentlens` pane inside Claude Code, the CLI and a terminal dashboard work. Cost estimates and cross-session analytics are planned for v0.3, Codex support for v0.5.

## Quick start

1. In Claude Code: `/plugin marketplace add SiddharthDevare10/AgentLens`, then `/plugin install agentlens@agentlens`.
2. In a terminal: `pipx install 'agentlens-local[tui]'`, then `agentlens doctor` (every line should say `ok`).
3. Work as usual. Type `/agentlens` in Claude Code for the live pane, or run `agentlens` in a terminal for the dashboard. Optional: `agentlens statusline install` for plan usage in your status bar.

## Install

Requires Python 3.10+ on macOS or Linux. Recording needs only the standard library, so nothing is pip-installed for the plugin.

### As a Claude Code plugin (recommended)

In Claude Code:

```
/plugin marketplace add SiddharthDevare10/AgentLens
/plugin install agentlens@agentlens
```

Then run `/reload-plugins` or start a new session. Every session is recorded from then on.

- `/agentlens` opens a live pane beside the conversation (see "Inside Claude Code" below).
- `/agentlens:cache` prints a five-line cache summary for the current session (it runs locally; its few lines are the only tokens it adds).
- `/agentlens:status` prints a short health check.
- `/agentlens:coach` prints the top suggestions to waste fewer tokens in this project.

The plugin's hooks run `hooks/run.sh`, which finds Python 3.10+ (set `AGENTLENS_PYTHON` to choose one), runs the bundled collector and **always exits 0**. If no Python is found, it logs one line to `~/.agentlens/logs/agentlens.log` and does nothing else. Recorded data lives in `~/.agentlens` (or `AGENTLENS_HOME`), so it survives uninstalling or updating the plugin.

### The `agentlens` command (from PyPI)

For the CLI and dashboard in your own terminal. The package is published as **`agentlens-local`** (the name `agentlens` on PyPI belongs to an unrelated project) and installs the `agentlens` command:

```bash
pipx install 'agentlens-local[tui]'      # or: uv tool install 'agentlens-local[tui]'
agentlens status
```

Drop `[tui]` for the standard-library-only CLI. Prefer `pipx` or `uv tool` over a plain `pip install`: both packages provide a Python module called `agentlens`, so they must not share an environment.

### Fallback: settings hooks

If you don't use plugins, let the CLI add hooks to your settings instead:

```bash
agentlens install      # only if you are NOT using the plugin
```

From a source checkout instead of PyPI:

```bash
git clone https://github.com/SiddharthDevare10/AgentLens && cd AgentLens
python3 -m venv .venv && .venv/bin/pip install -e '.[tui]'   # or -e . for CLI only
.venv/bin/agentlens install
```

`install` adds hooks to `~/.claude/settings.json` (backup first, atomic write, idempotent); if you set `CLAUDE_CONFIG_DIR`, it uses `$CLAUDE_CONFIG_DIR/settings.json`. Claude Code picks the change up without a restart. It records the absolute path of the interpreter you run it with; if you move that virtualenv, `agentlens status` reports it as `MISSING` and `agentlens install --repair` fixes it.

- `--scope local` writes `.claude/settings.local.json` in the current project only (AgentLens warns if git does not ignore that file). `--scope project` writes `.claude/settings.json`, which is usually committed; since the hook names your machine's interpreter, prefer `local` or the plugin.
- `--dry-run` shows exactly what would change, as a diff.
- `agentlens doctor` checks the whole setup and prints a fix for anything wrong.

**Don't use both.** Claude Code runs plugin hooks and settings hooks side by side, so every event would fire twice. `agentlens install` refuses while the plugin is enabled (override with `--force`), and `agentlens status` warns if both are active. To switch to the plugin, run `agentlens uninstall`.

## Cost coach

`agentlens coach` looks across your recent sessions in the current project (last 14 days; `--days`, `--project`, `--all-projects`, `--session`) and lists what is costing you tokens, biggest first. Every suggestion names its sessions and request or event IDs and labels each number `[obs]`, `[calc]` or `[est]`. It makes no model calls.

| Rule | Fires when | Suggests |
|---|---|---|
| Long context | a session ran ≥10 requests above 200k tokens of context and they account for ≥30% of its cache reads | `/compact` at a task boundary, or a new session for an unrelated task |
| Starting context | the median first request across sessions is ≥50k tokens | `/context`, disabling unused plugins, trimming CLAUDE.md |
| Plugin overhead | an enabled plugin lists ≈2k+ tokens of skills, agents and commands `[est]` and none of its skills, agents or MCP tools were called in ≥3 sessions | disabling it for the project, or installing only the parts you use |
| Large output | Bash calls returned ≥20k characters | narrower commands (`head`, `grep`, quiet flags) |
| Cache | the cache report below found a rebuild (idle gap, model switch, effort change, large instruction files) | that rule's fix |

**Plugins that help.** When an enabled plugin ships a skill aimed at the same problem, the suggestion names it. For example, with [ECC](https://github.com/affaan-m/ECC) installed, long-context findings point to `/ecc:strategic-compact`. No plugin is required, and AgentLens never runs one. The report also lists the plugin skills you did use, with how much each call grew the context `[calc]`.

**Limits.** The plugin listing size is an upper bound read from the plugin's own files (Claude Code shortens long listings); `/context` shows the real figure. Where plugins are installed comes from `~/.claude/plugins/installed_plugins.json`, which Claude Code does not document. Slash commands you type are not recorded (AgentLens doesn't store prompt text), so a plugin used only through its commands can look unused. A plugin's hooks run whether or not you call its skills, and what they are worth to you is not measured.

`/agentlens:coach` prints the top three suggestions in five lines.

## Cache coach

`agentlens cache [session]` shows where a session's prompt cache was rebuilt, why, and how to avoid it. Without a session ID it uses the latest session in the current directory.

Example output (from a test scenario, shortened):

```
Cache report · session 3f2a9c1e-…
  cache hit ratio  82.4%            [calc]
  cache read       4.10M tokens     [obs]
  cache written    610k tokens      [obs]
  cache TTL        5m (from cache-write split)  [calc]
  cache misses     2, 103k tokens re-processed  [calc]

Findings
  ✗ 11-12 14:02:11 [clk]  Model switch claude-sonnet-5 → claude-opus-5
      impact    re-processed 51k tokens [calc] (cache written 52k [obs]) · Claude Code estimate $1.14 [obs]
      evidence  event 9c1f2e…(PostModelSwitch, command); request req_01… → req_01…
      fix       Pick the model before you start, or switch right after /clear; each model has its own cache.
```

How it works:

- **Usage** comes from the session transcript, read incrementally: AgentLens saves a byte offset per transcript and only reads what was appended, counting each API request once. It keeps token counts, model, request ID and timestamps, never message content. The transcript format is not a documented interface, so reports say so, and a missing field is shown as unavailable.
- **A miss** uses the same definition as Claude Code's own status line: a request that read from cache more than 5% and at least 2,000 tokens less than the previous request left there. Only the main conversation is checked.
- **Causes** come from recorded evidence between the two requests, in this order: compaction (`PreCompact`/`PostCompact`), model switch (`PostModelSwitch`, or the model changing in the transcript), idle gap beyond the cache TTL (TTL read from the 5-minute/1-hour write split), effort change (on models where it invalidates the cache). Anything else is reported as an **unexplained rebuild**. Hooks don't report tool or MCP changes, so AgentLens never claims that cause.
- **Other rules:** resuming after the cache expired (Claude Code's own `prompt_cache_likely_expired`), three or more compactions, a cache-hit ratio under 70% over at least 10 requests, and instruction files (CLAUDE.md, rules) over 200 lines, with their token size estimated as bytes ÷ 4 and labelled `[est]`.
- **Impact** is in tokens. Dollars appear only when Claude Code itself reported an estimate in a hook payload. Nothing is presented as "savings".

`--requests` adds a per-request table. `--brief` prints at most five lines (what the skill shows).

## Plan usage (opt-in status line)

Claude Code tells its **status line** command how much of your plan's 5-hour and weekly limits you have used (Pro, Max and Ultra plans, after the first response). AgentLens can be that command:

```bash
agentlens statusline install --dry-run   # see the change to ~/.claude/settings.json
agentlens statusline install
```

Your status bar then shows two rows:

```
session 24% · resets 12:23 · week 41%
Opus · ctx 41% · cache 97% hit, 2 misses (warm, 1h)
```

The second row (model, context use, prompt-cache hit ratio and misses) comes straight from Claude Code's status line input and is only displayed, never stored; a value Claude Code has not reported yet is left out. The dashboard's the dashboard's **Plan usage** panel shows the current session (5-hour) and current week `[obs]` (Anthropic calls the 5-hour limit the "session" limit), when they were reported `[clk]`, and the tokens Claude Code on this machine used in the current 5-hour window `[calc]` (window start = reset time − 5 h).

- **No plan name, no token limit.** Claude Code does not report which plan you are on, and Anthropic does not publish limits as token counts; they are shared with claude.ai and your other devices. AgentLens shows only the percentages Claude Code reports.
- **Only the windows are kept.** The status line input also carries paths, model and cost; AgentLens stores only `session_id`, `cwd` and `rate_limits`. The latest snapshot is `~/.agentlens/plan_usage.json`; a history event is recorded only when a percentage changes.
- **Never blanks your status bar.** It always prints a line and exits 0. If you already had a status line, it is saved, runs with the same input below the AgentLens line, and is restored by `agentlens statusline uninstall`.
- API-key, Team and Enterprise sessions get no `rate_limits`, so the panel says "not reported".

## Inside Claude Code: the `/agentlens` pane

The plugin also ships a pane for Claude Code itself (Claude Code v2.1.287+). It **opens by itself** when an interactive session starts: beside the conversation from 144 columns wide (110 once you have opened it yourself), above the prompt in a narrower terminal. **`/agentlens`** closes it and opens it again. Once you close it, it stays closed for the rest of that session. To stop it opening by itself, turn off **Open the AgentLens pane when a session starts** (`autoOpenPane`) for the plugin in `/config`. The pane shows:

- **Session:** model, prompts, tool calls and failures.
- **Tokens:** in (cache read, written, uncached), out, cache hit ratio and API requests, plus context use.
- **Plan usage:** the current session (5-hour) and week, live from Claude Code (`$.session.usage()`), or from the status line snapshot when that is not available.
- **Failures** and the most recent events.

It refreshes every 3 seconds and after each turn while open; nothing runs while it is closed. It is read-only: it runs `agentlens pane-data` through the plugin's launcher and never touches a tool call. Claude Code calls this plugin API (a "mod") **early access**, so it may change between releases; recording does not depend on it, and you can turn the plugin's mod off in `/plugin`. `agentlens pane-data [--session ID]` prints the same data as JSON.

**`/agentlens-dashboard`** opens the full dashboard in a new Terminal.app window (macOS) and brings it to the front. It never opens by itself. Like `/agentlens`, it runs no model turn and costs no tokens.

## What AgentLens costs in tokens

| Part | Model tokens |
|---|---|
| Hooks (recording) | 0: async, no output, nothing added to context |
| Status line | 0: Claude Code's status line "does not consume API tokens" |
| `/agentlens` pane, dashboard, CLI | 0: local reads, no model calls |
| `/agentlens:status`, `/agentlens:cache`, `/agentlens:coach` skills | 0 until you run one (`disable-model-invocation`); then their 4–5 output lines and Claude's repeat of them enter the conversation, a few hundred tokens per use (estimate) |

## Dashboard

Run `agentlens` with no arguments (or `agentlens dashboard`) to open the terminal dashboard.

- **Home:** headline tiles (sessions, active now, tool calls, failed, failure rate, average tool time) for the selected project, then **Active session** (the most recent active session's tokens in/out, cache read/written and hit ratio) and **Plan usage** (see above). Below them are your projects, every recorded session (active, idle or ended, newest first) and a live activity feed across all sessions. `/` opens a filter (ID, project, model or state), `f` shows only sessions with failed tool calls, `Enter` opens a session.
- **Session:** tiles for duration, tool calls, failures, prompts, total tool time and tokens in. It opens on **Errors** when the session has any. Each failed call shows its full sanitized command, exit code, status, duration and error headline and the size of the full output. The other tabs are **Tool calls** (select a call to see everything recorded about it), **Timeline** and **Overview**.
- **Live:** while the dashboard is open it drains the spool every second and appends new events as they happen. `p` pauses, `r` syncs now, `?` shows the provenance legend, `Esc` goes back, `q` quits.

Every value carries the same `[obs]`/`[calc]`/`[est]`/`[clk]` labels as the CLI. The dashboard never edits Claude Code settings and makes no network calls. Sessions stay in the database after Claude Code exits, so you can review them at any time.

### Open it in a window (macOS)

```
agentlens dashboard --window          # now, in a new Terminal window (what /agentlens-dashboard runs)
agentlens dashboard --auto-open off   # run once with the agentlens that has the `tui` extra: saves it for --window
agentlens dashboard --auto-open on    # opt-in: also open it with every new Claude Code session
```

`--window` uses the saved Python, or the current one if it has the `tui` extra, and does nothing if a dashboard is already running. When `--auto-open` is on, starting a new Claude Code session opens the dashboard in a new Terminal.app window, behind your editor. It does this only for new sessions (not on resume, `/clear` or compaction), only when no dashboard is already running, and at most once per 15 seconds. It also skips non-interactive runs (`claude -p`, the SDK), using `CLAUDE_CODE_ENTRYPOINT`, which Claude Code doesn't document. The window runs a script at `~/.agentlens/run/dashboard.command` handed to `open -g -a Terminal`, so macOS asks for no Automation permission. It works the same with the plugin and with settings hooks. Skipped launches are logged to `~/.agentlens/logs/agentlens.log`.

## Commands

| Command | What it does |
|---|---|
| `agentlens install [--scope user\|project\|local] [--dry-run] [--repair] [--force]` | Fallback to the plugin. Adds async hooks for 18 Claude Code events (the same set the plugin registers). Backs up the settings file first and writes it atomically. Running it again changes nothing. Refuses to touch a settings file that is not valid JSON. `--dry-run` prints the change as a diff and writes nothing. `--repair` also normalizes AgentLens's own entries (duplicates, edited fields, events it no longer uses) and never touches other hooks. |
| `agentlens uninstall [--scope ...] [--dry-run]` | Removes only handlers whose command contains `agentlens hook`. Keeps recorded data. |
| `agentlens status [--scope ...] [--brief]` | Plugin and settings-hook installation (user, project and local scopes), interpreter health, spool and database state, and warnings (double install, `disableAllHooks`). `--brief` prints at most four lines. |
| `agentlens doctor [--no-hook-test]` | Read-only health check, one line per check (`ok`/`warn`/`FAIL`) with the fix to run. Runs each configured hook command with a test event against a temporary data directory, so your real data is never touched. Exits 1 if any check fails. |
| `agentlens statusline install\|uninstall [--dry-run]` | Opt-in: makes AgentLens the Claude Code status line to show and record plan usage (5-hour and weekly %). Keeps and restores any status line you had. |
| `/agentlens` (in Claude Code) | Opens or closes the AgentLens pane beside the conversation (plugin, Claude Code v2.1.287+). |
| `agentlens pane-data [--session ID]` | The pane's data as one JSON object (tokens, plan usage, failures, recent events) with provenance. |
| `agentlens sessions [--limit N]` | Recent sessions: start, duration, state, prompts, tool calls, failures, tokens in/out. |
| `agentlens inspect <id-or-prefix> [--all]` | One session in detail: overview (including API requests, input/output tokens and cache hit ratio), tool calls, errors, timeline. |
| `agentlens coach [--days N] [--project DIR\|--all-projects] [--session ID] [--brief]` | Cost coach: suggestions to waste fewer tokens across recent sessions, with evidence. |
| `agentlens cache [session] [--brief] [--requests]` | Cache coach report: hit ratio, misses, causes with evidence, and fixes. |
| `agentlens` / `agentlens dashboard [--window] [--auto-open on\|off]` | Opens the terminal dashboard (needs the `tui` extra). Without a terminal, prints help. `--window` (macOS) opens it in a new Terminal window now; `--auto-open on` does that whenever a Claude Code session starts. |
| `/agentlens-dashboard` (in Claude Code) | Opens the dashboard in a new Terminal window (plugin, macOS). |
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

- **Token usage** comes from the session transcript's per-request `usage` fields (`[obs]`; one count per API request, subagents included). That format is undocumented, so a session without a readable transcript shows tokens as unavailable with the reason. **Cost** needs pricing and is planned for v0.3.
- **Session duration** is unavailable when AgentLens missed `SessionStart`, for example if it was installed mid-session.
- **Session state** is `ended (<reason>)` once `SessionEnd` arrives. Closing the terminal or a crash sends no `SessionEnd`, so until then a session is **active** while hook events arrive and **idle** after an hour without one (status-line refreshes don't count). Neither label claims the session ended; its duration is shown as `≥ …`.
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
- **Tool failures:** the exit code, one headline line of the error (redacted, at most 200 characters, picked by fixed rules: the last line that reads like an error, else the last line), and the size of the full output. The rest of the output, which can include file contents, is not stored.
- **Prompts:** the length and a keyed HMAC (the salt is in `~/.agentlens/salt`). The text itself is not stored. To store redacted, truncated prompt text, opt in with `{"capture_prompts": true}` in `~/.agentlens/config.json`.
- **Assistant messages:** the length only.

Redaction runs before anything is written to disk, including the spool. It covers:

- `sk-…`, `sk-ant-…`, `ghp_…`/`gho_…`/…, `github_pat_…`, `AKIA…`/`ASIA…`, `xox?-…`, `AIza…`, JWTs.
- Bearer and `Authorization:` headers, and `user:pass@` in URLs.
- Secret-named `KEY=value` / `"key": "value"` pairs, and `--password`/`--token`/`--api-key` flags.
- Private key blocks.

Files AgentLens keeps, all under `~/.agentlens` (directory mode 0700, files 0600):

| Path | What |
|---|---|
| `agentlens.db` | The SQLite database: normalized, redacted events and per-request token counts. |
| `spool/` | Events waiting to be ingested (normally empty); `spool/failed/` holds any that could not be read. |
| `plan_usage.json` | The latest plan-usage windows (only with the status line installed). |
| `statusline-previous.json` | Your previous status line, kept so `statusline uninstall` can restore it. |
| `salt` | The key for prompt hashes. |
| `config.json` | Optional settings (e.g. `capture_prompts`). |
| `logs/agentlens.log` | Errors from hooks and ingest (redacted). |

AgentLens makes no network calls and sends nothing anywhere. Set `AGENTLENS_HOME` to keep all data somewhere other than `~/.agentlens`.

## Troubleshooting

Start with `agentlens doctor`: it checks everything below and prints the command that fixes each problem. It changes nothing itself.

| Symptom | Cause and fix |
|---|---|
| No sessions appear | Neither the plugin nor settings hooks are installed, or `disableAllHooks` is set. `doctor` says which. Install the plugin, or run `agentlens install`. |
| Every event shows up twice in `status` | The plugin and settings hooks are both installed. Keep one: `agentlens uninstall`. |
| `interpreter … MISSING` | The virtualenv AgentLens was installed from moved or was deleted. Run `agentlens install --repair` from the current environment. |
| Some events never appear (e.g. compactions) | Hooks from an older AgentLens lack newer events. `agentlens install --dry-run`, then `agentlens install`. |
| Tokens show `unavailable (…)` | The reason is shown: usually the session's transcript was not found or has no usage entries. The transcript format is undocumented, so a Claude Code update can change it. |
| Plan usage shows `unavailable` | Install the status line (`agentlens statusline install`), or you are on an API key, Team or Enterprise plan, which get no plan windows. |
| `/agentlens` does nothing | It needs the plugin and Claude Code v2.1.287+. A pane opened by a mod on its own waits for a wide terminal; one you open yourself shows at any width. |
| Status bar went blank | AgentLens never exits non-zero, so check `~/.agentlens/logs/agentlens.log` and that the workspace trust dialog was accepted. `agentlens statusline uninstall` restores your previous status line. |

## Uninstall

```bash
agentlens statusline uninstall     # if you installed it; restores your previous status line
agentlens uninstall                # settings hooks (add --scope project|local if you used them)
pipx uninstall agentlens-local     # or: uv tool uninstall agentlens-local
rm -rf ~/.agentlens                # recorded data (optional; nothing else is stored)
```

In Claude Code, remove the plugin with `/plugin` (Installed tab) or `claude plugin uninstall agentlens@agentlens`. Uninstalling never deletes `~/.agentlens`; that is the last step above.

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

Tests run with temporary `HOME`, `AGENTLENS_HOME` and `AGENTLENS_CLAUDE_SETTINGS`. A fixture fails any test whose paths resolve outside the sandbox. See `CONTRIBUTING.md` for the working rules and `RELEASING.md` for how releases are cut.

CI (`.github/workflows/ci.yml`) runs the suite on Ubuntu and macOS for Python 3.10–3.14, plus a job without the `tui` extra to keep the core standard-library only.

## License

MIT

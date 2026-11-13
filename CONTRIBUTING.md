# Contributing to AgentLens

Thanks for helping. AgentLens is small on purpose: deterministic, local, and
careful with other people's data. These notes keep it that way.

## Set up

```bash
git clone https://github.com/spikethebuild/AgentLens && cd AgentLens
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
```

The `/agentlens` pane is TypeScript (`hooks/agentlens-pane.tsx`). With Claude
Code installed: `claude plugin validate .` and `claude plugin test .`.

Tests never touch your real `~/.claude` or `~/.agentlens`: a fixture sets a
temporary `HOME`, `AGENTLENS_HOME` and `AGENTLENS_CLAUDE_SETTINGS` and fails any
test whose paths escape it. Keep it that way for new tests.

## Rules every change follows

1. **Zero LLM calls** in collection and analysis.
2. **Never block the agent.** Hooks and the status line always exit 0 and stay fast.
3. **Privacy first.** No file contents, secrets or message text on disk;
   prompt text only when the user opts in. Redact before anything is written.
4. **Never invent data.** Missing values say `unavailable` and why. Every
   number is labelled `[obs]` (reported by the provider), `[calc]` (derived by
   AgentLens) or `[est]` (estimated).
5. **Do not break the user's settings.** Back up, write atomically, stay
   idempotent, touch only AgentLens's own entries, refuse invalid JSON.
6. **Standard library only** for the hook path, ingest and the plain CLI.
   Textual and Rich live in the optional `tui` extra.
7. **Provider-agnostic core.** Provider details belong in `adapters/`.
8. **Suggestions cite evidence**: sessions, event or request IDs, times, counts.

Provider facts (hook fields, status line fields, cache behaviour) must come
from the official docs. Check them, and note the date you checked, before
relying on a field or behaviour.

## Pull requests

- One focused change per PR, with tests. Run `pytest -q` before pushing.
- Update `README.md` when behaviour changes, and add a line to `CHANGELOG.md`.
- Commit messages are imperative and scoped: `adapter: normalize PostToolUseFailure`.
- Never commit real transcripts, settings files or secrets. Test fixtures use
  made-up values.

AI coding agents working on this repo should follow the rules above.

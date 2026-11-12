---
name: status
description: Show whether AgentLens is recording this machine's Claude Code sessions (short health check).
disable-model-invocation: true
---

!`sh "${CLAUDE_PLUGIN_ROOT}/hooks/run.sh" status --brief`

Repeat the AgentLens status above to the user verbatim. Do not add analysis.

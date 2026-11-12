---
name: cache
description: Show where this session's prompt cache was rebuilt, why, and the fix (AgentLens, local analysis, no extra model calls).
disable-model-invocation: true
---

!`sh "${CLAUDE_PLUGIN_ROOT}/hooks/run.sh" cache --brief`

Repeat the AgentLens cache summary above to the user verbatim. Do not add analysis. Full report in a terminal: `agentlens cache <session>`.

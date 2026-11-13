// Type contract for the AgentLens pane's session state ($.state).

/** One rate-limit window as shown in the pane. `source` says who reported it. */
export type PlanWindow = {
  name: string
  usedPercentage: number
  /** Epoch seconds, or null when not reported. */
  resetsAt: number | null
}

/** What `agentlens pane-data` printed, as the pane uses it. */
export type PaneData = {
  schema: number
  error?: string
  reason?: string
  session: null | {
    id: string
    model: string | null
    state: string
    last_event: number
    prompts: number
    tool_calls: number
    tool_failures: number
    tokens: null | {
      requests: number
      subagent_requests: number
      input: number
      cache_read: number
      cache_write: number
      uncached: number
      output: number
      hit_ratio: number | null
    }
    tokens_reason: string | null
  }
  plan: null | {
    as_of: number
    windows: { name: string; used_percentage: number; resets_at: number | null }[]
  }
  failures: { ts: number; source: string; message: string }[]
  events: { ts: number; type: string; detail: string }[]
}

/** Everything the pane draws, refreshed while it is open. */
export type PaneView = {
  data: PaneData | null
  /** Why the last refresh failed, shown in the pane; null when it worked. */
  error: string | null
  /** Live plan windows from Claude Code ($.session.usage), preferred over AgentLens's snapshot. */
  live: PlanWindow[]
  /** Context window use from Claude Code, 0-100, or null when not reported. */
  contextPercent: number | null
  /** When the pane last refreshed, epoch milliseconds. */
  refreshedAt: number | null
}

declare module 'claude-code' {
  interface PluginState {
    agentlens: {
      view: Shaped<PaneView>
      /** True once the person closed the pane this session: a reload must not reopen it. */
      dismissed: Shaped<boolean>
    }
  }
}

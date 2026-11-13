// AgentLens pane for Claude Code: a live panel beside the conversation with this
// session's tokens, cache, plan usage, failures and recent events. It opens by
// itself in interactive sessions (plugin option `autoOpenPane`, on by default);
// `/agentlens` opens or closes it. `/agentlens-dashboard` opens the full
// dashboard in a Terminal window, only when asked.
//
// Read-only and local: it runs `agentlens pane-data` (the plugin's own CLI,
// through hooks/run.sh) and reads Claude Code's own usage figures. It makes no
// model calls, so it costs no tokens, and it never touches a tool call. Nothing
// runs while the pane is closed.

import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { PaneData, PaneView, PlanWindow } from '../types'

const PANE = 'agentlens'
const COMMAND = 'agentlens'
const DASHBOARD_COMMAND = 'agentlens-dashboard'
const REFRESH_MS = 3000
const RUN_TIMEOUT_MS = 15000

const VIOLET = '#c3b1ff'
const TEXT = '#e2ddf3'
const MUTED = '#9a93b8'
const DIM = '#655f80'
const MINT = '#9fe0c0'
const PEACH = '#f3c48f'
const ROSE = '#f5a3b5'

const EMPTY: PaneView = { data: null, error: null, live: [], contextPercent: null, refreshedAt: null }
// Change the tag when PaneView's fields change: it declines what an earlier version stored.
const view = atom({ plugin: 'agentlens', key: 'view' } as const, EMPTY, { shape: 'view-1' })
const dismissed = atom({ plugin: 'agentlens', key: 'dismissed' } as const, false, { shape: 'dismissed-1' })
const dashboardAsked = atom({ plugin: 'agentlens', key: 'dashboardAsked' } as const, false, { shape: 'asked-1' })

// The start-up question. "Never ask" is kept in $.store (across sessions).
const NEVER_ASK_KEY = 'dashboardNeverAsk'
const ASK_YES = 'Open dashboard'
const ASK_NO = 'Not now'
const ASK_NEVER = "Don't ask again"

type Options = { autoOpenPane?: boolean; askDashboard?: boolean }

// ---------------------------------------------------------------- formatting

export function tokens(n: number | null | undefined): string {
  if (n === null || n === undefined) return 'unavailable'
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`
  if (n >= 10_000) return `${Math.round(n / 1000)}k`
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`
  return String(n)
}

function pad2(n: number): string {
  return n < 10 ? `0${n}` : String(n)
}

/** HH:MM from epoch seconds; with the weekday when it is more than a day away. */
export function clock(epochSeconds: number | null, nowMs: number): string {
  if (epochSeconds === null) return '?'
  const d = new Date(epochSeconds * 1000)
  const hm = `${pad2(d.getHours())}:${pad2(d.getMinutes())}`
  if (epochSeconds * 1000 - nowMs < 86_400_000) return hm
  return `${['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][d.getDay()]} ${hm}`
}

const WINDOW_LABEL: Record<string, string> = { five_hour: 'session (5h)', seven_day: 'week' }

/** Claude Code's live windows first; AgentLens's status line snapshot when there are none. */
export function planWindows(v: PaneView, nowMs: number): { windows: PlanWindow[]; source: string } {
  const live = v.live.filter(w => w.name in WINDOW_LABEL)
  if (live.length) return { windows: live, source: 'Claude Code, live' }
  const snap = v.data?.plan
  if (!snap) return { windows: [], source: '' }
  const windows = snap.windows
    .filter(w => w.name in WINDOW_LABEL && (w.resets_at === null || w.resets_at * 1000 > nowMs))
    .map(w => ({ name: w.name, usedPercentage: w.used_percentage, resetsAt: w.resets_at }))
  return { windows, source: 'AgentLens status line' }
}

// ---------------------------------------------------------------- data

async function refresh($: EngineInterface): Promise<void> {
  let data: PaneData | null = null
  let error: string | null = null
  try {
    const id: string = await $.session.id()
    const ran = await $.process.run(
      ['sh', `${$.plugin.root}/hooks/run.sh`, 'pane-data', '--session', id],
      { timeoutMs: RUN_TIMEOUT_MS },
    )
    const text = String(ran.stdout ?? '').trim()
    if (ran.exitCode !== 0 || !text.startsWith('{')) {
      error = text.split('\n')[0] || `agentlens exited ${ran.exitCode}`
    } else {
      data = JSON.parse(text) as PaneData
      if (data.error) error = data.error
    }
  } catch (err) {
    error = err instanceof Error ? err.message : String(err)
  }
  let live: PlanWindow[] = []
  let contextPercent: number | null = null
  try {
    const usage = await $.session.usage()
    live = (usage.rateLimits ?? []).map((w: { kind: string; percentUsed: number; resetsAt?: string }) => ({
      name: w.kind,
      usedPercentage: w.percentUsed,
      resetsAt: w.resetsAt ? Date.parse(w.resetsAt) / 1000 : null,
    }))
    contextPercent = typeof usage.context?.percent === 'number' ? usage.context.percent : null
  } catch {
    // Claude Code's figures are optional: the pane still shows AgentLens's own data.
  }
  await update($, view, prev => ({
    data: data ?? prev.data,
    error,
    live,
    contextPercent,
    refreshedAt: Date.now(),
  }))
}

let timer: { cancel: () => void } | null = null

function stopTimer(): void {
  timer?.cancel()
  timer = null
}

async function isOpen($: EngineInterface): Promise<boolean> {
  return (await $.ui.panes()).some((pane: { id: string }) => pane.id === PANE)
}

function startTimer($: EngineInterface): void {
  stopTimer()
  timer = $.clock.every(REFRESH_MS, () => void refresh($))
}

async function openPane($: EngineInterface): Promise<boolean> {
  const opened = await $.ui.open({ id: PANE, title: 'AgentLens' })
  await refresh($)
  startTimer($)
  return opened.isPlaced
}

/** Session start (and every reload): reopen or keep the pane, unless the person closed it. */
async function onStart($: EngineInterface, autoOpen: boolean): Promise<void> {
  if (await isOpen($)) {
    await refresh($)
    startTimer($) // a reload drops the module's timer; the pane itself stays
    return
  }
  if (autoOpen && !(await read($, dismissed))) await openPane($)
}

async function openDashboard($: EngineInterface): Promise<string> {
  const ran = await $.process.run(
    ['sh', `${$.plugin.root}/hooks/run.sh`, 'dashboard', '--window'],
    { timeoutMs: RUN_TIMEOUT_MS },
  )
  const text = String(ran.stdout ?? '').trim().split('\n')[0]
  return text || `agentlens exited ${ran.exitCode}`
}

/**
 * Once per session (not on reloads): ask whether to open the Terminal dashboard.
 * Not awaited by session.start, so the session never waits on the answer.
 */
async function askDashboard($: EngineInterface): Promise<void> {
  if (await read($, dashboardAsked)) return
  await update($, dashboardAsked, () => true)
  if ((await $.store.get(NEVER_ASK_KEY)) === true) return
  let answer: string
  try {
    answer = await $.ui.ask('Open the AgentLens dashboard in a Terminal window?', {
      options: [ASK_YES, ASK_NO, ASK_NEVER],
      header: 'AgentLens',
    })
  } catch {
    return // dismissed: same as "Not now"
  }
  if (answer === ASK_YES) $.ui.toast(await openDashboard($))
  else if (answer === ASK_NEVER) {
    await $.store.set(NEVER_ASK_KEY, true)
    $.ui.toast('AgentLens will not ask again. /agentlens-dashboard opens it any time.')
  }
}

// ---------------------------------------------------------------- hooks

export const register: Register = (on, options) => {
  const autoOpen = (options as Options | undefined)?.autoOpenPane !== false
  const ask = (options as Options | undefined)?.askDashboard !== false

  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: COMMAND,
      description: 'Open or close the AgentLens pane: tokens, cache, plan usage and failures (local, no tokens)',
      immediate: true,
    })
    await $.command.register({
      name: DASHBOARD_COMMAND,
      description: 'Open the full AgentLens dashboard in a Terminal window (macOS, local, no tokens)',
      immediate: true,
    })
    if (e.isInteractive) {
      await onStart($, autoOpen)
      if (ask) void askDashboard($).catch(() => undefined)
    }
    return next(e)
  }).catch(($, e, next) => (next.called ? undefined : next(e))) // never hold up a session start

  on('command.run', { command: COMMAND }, async $ => {
    if (await isOpen($)) {
      await $.ui.close({ id: PANE })
      return { text: 'AgentLens pane closed. Run /agentlens to open it again.' }
    }
    await update($, dismissed, () => false)
    const placed = await openPane($)
    return {
      text: placed
        ? 'AgentLens pane opened. Run /agentlens again to close it.'
        : 'AgentLens pane opened; it shows when the terminal has room.',
    }
  }).catch(() => {
    stopTimer()
    return { text: 'The AgentLens pane hit an error. Run `agentlens doctor` in a terminal.' }
  })

  on('command.run', { command: DASHBOARD_COMMAND }, async $ => ({ text: await openDashboard($) }))
    .catch(() => ({ text: 'Could not open the dashboard. Run `agentlens dashboard` in a terminal.' }))

  on('ui.close', { id: PANE }, async ($, e, next) => {
    stopTimer()
    try {
      await update($, dismissed, () => true) // closed: stay closed for this session, even across a reload
    } catch {
      // Closing must never fail because of AgentLens (e.g. the session is already ending).
    }
    return next(e)
  }).catch(($, e, next) => {
    stopTimer()
    return next.called ? undefined : next(e) // a close must never be held up by AgentLens
  })

  on('turn.complete', async ($, e, next) => {
    const done = await next(e)
    if (timer) void refresh($)
    return done
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const v = await read($, view)
    const now = Date.now()
    const s = v.data?.session ?? null
    const t = s?.tokens ?? null
    const plan = planWindows(v, now)
    const width = e.props?.bodyColumns ?? 60

    const row = (label: string, value: string, tag = '', color = TEXT) => (
      <Box flexDirection="row">
        <Text color={MUTED}>{label.padEnd(13)}</Text>
        <Text color={color}>{value}</Text>
        {tag ? <Text color={DIM}>{` [${tag}]`}</Text> : null}
      </Box>
    )
    const heading = (text: string) => <Text color={VIOLET} bold>{text}</Text>

    return (
      <Box flexDirection="column">
        {v.error ? <Text color={ROSE}>{`! ${v.error}`.slice(0, width * 2)}</Text> : null}
        {v.data === null && !v.error ? <Text color={DIM}>Loading AgentLens data…</Text> : null}

        {heading('Session')}
        {s === null
          ? <Text color={PEACH}>{v.data?.reason ?? 'unavailable'}</Text>
          : (
            <Box flexDirection="column">
              {row('model', s.model ?? 'unavailable', s.model ? 'obs' : '')}
              {row('prompts', String(s.prompts), 'calc')}
              {row('tool calls', `${s.tool_calls}${s.tool_failures ? `, ${s.tool_failures} failed` : ''}`, 'calc',
                s.tool_failures ? ROSE : TEXT)}
            </Box>
          )}

        {heading('Tokens')}
        {t === null
          ? <Text color={PEACH}>{`unavailable (${s?.tokens_reason ?? 'no usage recorded yet'})`}</Text>
          : (
            <Box flexDirection="column">
              {row('in', tokens(t.input), 'obs')}
              {row('  cache read', tokens(t.cache_read), 'obs')}
              {row('  written', tokens(t.cache_write), 'obs')}
              {row('  uncached', tokens(t.uncached), 'obs')}
              {row('out', tokens(t.output), 'obs')}
              {row('cache hit', t.hit_ratio === null ? 'unavailable' : `${(t.hit_ratio * 100).toFixed(1)}%`,
                t.hit_ratio === null ? '' : 'calc', MINT)}
              {row('requests', `${t.requests}${t.subagent_requests ? ` (${t.subagent_requests} subagent)` : ''}`, 'calc')}
            </Box>
          )}
        {v.contextPercent !== null ? row('context', `${Math.round(v.contextPercent)}% used`, 'obs') : null}

        {heading('Plan usage')}
        {plan.windows.length === 0
          ? <Text color={PEACH}>unavailable (Claude Code reports it for Pro/Max/Ultra after the first response)</Text>
          : (
            <Box flexDirection="column">
              {plan.windows.map(w =>
                row(WINDOW_LABEL[w.name] ?? w.name,
                  `${Math.round(w.usedPercentage)}% used · resets ${clock(w.resetsAt, now)}`, 'obs',
                  w.usedPercentage >= 80 ? ROSE : TEXT))}
              <Text color={DIM}>{`from ${plan.source}`}</Text>
            </Box>
          )}

        {heading('Failures')}
        {(v.data?.failures ?? []).length === 0
          ? <Text color={DIM}>none recorded</Text>
          : (v.data?.failures ?? []).map(f => (
            <Text color={ROSE}>{`✗ ${clock(f.ts, now)} ${f.source}: ${f.message}`.slice(0, width * 2)}</Text>
          ))}

        {heading('Recent')}
        {(v.data?.events ?? []).slice(-6).map(ev => (
          <Text color={DIM}>{`${clock(ev.ts, now)} ${ev.type} ${ev.detail}`.slice(0, width)}</Text>
        ))}
        <Text color={DIM}>{v.refreshedAt ? `updated ${new Date(v.refreshedAt).toLocaleTimeString()} · /agentlens-dashboard full view · /agentlens closes` : ''}</Text>
      </Box>
    )
  })
}

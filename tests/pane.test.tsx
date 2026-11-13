// AgentLens pane (hooks/agentlens-pane.tsx), run by `claude plugin test .`.
// Each `on(...)` here stands for the engine beneath the plugin.

import { expect, test } from 'claude-code/testing'

const NOW_S = Date.now() / 1000

const DATA = {
  schema: 1,
  session: {
    id: 'sess-1', model: 'claude-opus-5-5', state: 'open', last_event: NOW_S,
    prompts: 4, tool_calls: 12, tool_failures: 1,
    tokens: {
      requests: 9, subagent_requests: 0, input: 4_440_000, cache_read: 4_300_000,
      cache_write: 135_000, uncached: 80, output: 32_000, hit_ratio: 0.97,
    },
    tokens_reason: null,
  },
  plan: { as_of: NOW_S, windows: [{ name: 'five_hour', used_percentage: 23, resets_at: NOW_S + 3600 }] },
  failures: [{ ts: NOW_S - 60, source: 'Bash', message: 'Exit code 2' }],
  events: [{ ts: NOW_S - 5, type: 'tool.post', detail: 'Bash ok' }],
}

const PANE = {
  plugin: 'agentlens',
  component: 'Pane',
  requestId: 'agentlens',
  props: {
    title: 'AgentLens', isFocused: false, bodyColumns: 60, placement: 'dock',
    scroll: { offset: 0, bodyRows: 40 }, view: {},
  },
} as const

function engine(on: any, opts: { stdout?: string; live?: boolean; panes?: string[] } = {}) {
  const ran: string[][] = []
  let panes = opts.panes ?? []
  on('command.register', () => ({ value: { command: 'agentlens' } }))
  on('session.start', (_: unknown, e: { cwd: string }) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: 'sess-1' }))
  on('session.usage', () => ({
    value: {
      startedAt: 0,
      context: { window: 200000, percent: 41 },
      rateLimits: opts.live ? [{ kind: 'five_hour', percentUsed: 30, resetsAt: new Date(Date.now() + 7200_000).toISOString() }] : [],
    },
  }))
  on('process.run', (_: unknown, e: { argv: string[] }) => {
    ran.push([...e.argv])
    return { value: { exitCode: 0, stdout: opts.stdout ?? JSON.stringify(DATA), stderr: '' } }
  })
  on('ui.open', (_: unknown, e: { id: string }) => {
    panes = [...panes, e.id]
    return { value: { isPlaced: true } }
  })
  on('ui.close', (_: unknown, e: { id: string }) => {
    panes = panes.filter(p => p !== e.id)
    return { value: undefined }
  })
  on('ui.panes', () => ({
    value: panes.map(id => ({ id, title: 'AgentLens', isShown: true, isFocused: false, isPlaced: true })),
  }))
  on('clock.every', () => ({ value: undefined }))
  return { ran, panes: () => panes }
}

const MANUAL = { options: { autoOpenPane: false } }

const run = ($: any) => $.command.run({ command: 'agentlens', args: '', origin: { kind: 'composer' } })

test('/agentlens opens the pane and shows tokens, plan usage and failures', MANUAL, async ($, on) => {
  const eng = engine(on)
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  const result = await run($)
  expect(result.text).toContain('AgentLens pane opened')
  expect(eng.ran[0]?.slice(-3)).toEqual(['pane-data', '--session', 'sess-1'])
  expect(eng.ran[0]?.[1]).toMatch(/\/hooks\/run\.sh$/)

  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect((await ui.find({ type: 'Text', text: /^4\.44M$/ }))?.text).toBe('4.44M')
    expect((await ui.find({ type: 'Text', text: /97\.0%/ }))?.text).toBe('97.0%')
    expect((await ui.find({ type: 'Text', text: /23% used/ }))?.text).toContain('23% used · resets')
    expect((await ui.find({ type: 'Text', text: /AgentLens status line/ }))?.text).toContain('from AgentLens status line')
    expect((await ui.find({ type: 'Text', text: /Exit code 2/ }))?.text).toContain('✗')
    expect((await ui.find({ type: 'Text', text: /41% used/ }))?.text).toBe('41% used')
    await ui.unmount()
  }
})

test('live plan usage from Claude Code is preferred over the snapshot', MANUAL, async ($, on) => {
  engine(on, { live: true })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await run($)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Text', text: /30% used/ }))?.text).toContain('30% used')
  expect((await ui.find({ type: 'Text', text: /Claude Code, live/ }))?.text).toContain('Claude Code, live')
  await ui.unmount()
})

test('a second /agentlens closes the pane', MANUAL, async ($, on) => {
  const eng = engine(on)
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await run($)
  expect(eng.panes()).toEqual(['agentlens'])
  const result = await run($)
  expect(result.text).toContain('AgentLens pane closed.')
  expect(eng.panes()).toEqual([])
})

test('a failing agentlens command shows its reason, not a blank pane', MANUAL, async ($, on) => {
  engine(on, { stdout: 'AgentLens: Python 3.10+ not found. Install it or set AGENTLENS_PYTHON.' })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await run($)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Text', text: /Python 3\.10\+ not found/ }))?.text).toContain('!')
  expect((await ui.find({ type: 'Text', text: /no usage recorded yet|unavailable/ }))).toBeTruthy()
  await ui.unmount()
})

const START = { cwd: '/', surface: 'terminal', isInteractive: true } as const

// A pane left open is drawn after the test ends; close it so nothing outlives the test.
const closePane = ($: any) => run($) // /agentlens toggles the open pane shut

test('the pane opens by itself in an interactive session', async ($, on) => {
  const eng = engine(on)
  await $.session.start(START)
  expect(eng.panes()).toEqual(['agentlens'])
  expect(eng.ran.some(argv => argv.includes('pane-data'))).toBe(true)
  await closePane($)
})

test('no pane for a -p run or the SDK', async ($, on) => {
  const eng = engine(on)
  await $.session.start({ cwd: '/', surface: null, isInteractive: false })
  expect(eng.panes()).toEqual([])
})

test('autoOpenPane off: only /agentlens opens it', MANUAL, async ($, on) => {
  const eng = engine(on)
  await $.session.start(START)
  expect(eng.panes()).toEqual([])
  await run($)
  expect(eng.panes()).toEqual(['agentlens'])
  await closePane($)
})

test('a pane the person closed stays closed when session.start runs again (reload)', async ($, on) => {
  const eng = engine(on)
  await $.session.start(START)
  await run($) // closes it
  expect(eng.panes()).toEqual([])
  await $.session.start(START)
  expect(eng.panes()).toEqual([])
  await run($) // reopening by hand clears that
  expect(eng.panes()).toEqual(['agentlens'])
  await closePane($)
})

test('/agentlens-dashboard opens the Terminal dashboard through the launcher', async ($, on) => {
  const eng = engine(on, { stdout: 'Opened the AgentLens dashboard in a new Terminal window.' })
  await $.session.start(START)
  const result = await $.command.run({ command: 'agentlens-dashboard', args: '', origin: { kind: 'composer' } } as any)
  expect(result.text).toBe('Opened the AgentLens dashboard in a new Terminal window.')
  const argv = eng.ran.find(a => a.includes('--window'))
  expect(argv?.slice(-2)).toEqual(['dashboard', '--window'])
  expect(argv?.[1]).toMatch(/\/hooks\/run\.sh$/)
  await closePane($)
})

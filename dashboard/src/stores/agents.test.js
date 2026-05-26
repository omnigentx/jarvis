/**
 * Agents store unit tests.
 *
 * Pinia vanilla-JS surface (no Vue runtime needed for state mutations).
 * Stubs globalThis.fetch + apiFetch so the store doesn't try to hit a
 * live backend during unit runs.
 */
import { test, beforeEach } from 'node:test'
import assert from 'node:assert/strict'
import { createPinia, setActivePinia } from 'pinia'

// Stub apiFetch BEFORE importing the store (the store closures it in).
import { register } from 'node:module'

// Inline-mock via dynamic import + global replacement isn't possible
// without a module loader hook; fall back to manually stubbing the
// fetch primitive used by ../api. The store only hits apiFetch in
// fetchAgents/pauseAgent/resumeAgent, none of which the race test
// exercises — we drive processEvent() directly.
globalThis.fetch = async () => new Response('[]', { status: 200 })

const { useAgentsStore } = await import('./agents.js')

beforeEach(() => {
  setActivePinia(createPinia())
})

// HP-1 regression: a final-turn ``message_turn`` event that races AFTER
// the pause cycle must NOT clobber the agent's status back to idle.
// Pin for the 2026-05-24 bug where Jarvis stayed running, user clicked
// Pause, and the badge flipped to "Idle" because the trailing
// message_turn from the just-finished response forced status='idle'
// over the freshly-set 'paused'.
test('message_turn final-turn does not clobber paused status', () => {
  const store = useAgentsStore()

  // Bootstrap an agent record.
  store.processEvent({
    agent_name: 'Jarvis',
    event_type: 'agent_added',
    timestamp: 1,
    data: {},
  })

  // Pause arrives first → status=pausing.
  store.processEvent({
    agent_name: 'Jarvis',
    event_type: 'agent_pausing',
    timestamp: 2,
    data: { status: 'pausing' },
  })
  // Terminal paused emitted shortly after (idle-agent path).
  store.processEvent({
    agent_name: 'Jarvis',
    event_type: 'agent_paused',
    timestamp: 3,
    data: { status: 'paused' },
  })
  assert.equal(store.agents.get('Jarvis').status, 'paused')

  // Trailing message_turn from the just-finished assistant turn —
  // pre-fix this overwrote status to 'idle'.
  store.processEvent({
    agent_name: 'Jarvis',
    event_type: 'message_turn',
    timestamp: 4,
    data: {
      message: { role: 'assistant', stop_reason: 'endTurn' },
    },
  })

  assert.equal(
    store.agents.get('Jarvis').status,
    'paused',
    'message_turn must respect the pause-cycle status — pre-fix this flipped to idle',
  )
})

// Counterpoint: when the agent is NOT in a pause cycle, message_turn
// must still set idle on a final assistant turn (otherwise the badge
// would stick on "Running" forever).
test('message_turn final-turn sets idle when not in pause cycle', () => {
  const store = useAgentsStore()
  store.processEvent({
    agent_name: 'Jarvis',
    event_type: 'agent_added',
    timestamp: 1,
    data: {},
  })
  // Simulate normal running state via a tool_calls message_turn.
  store.processEvent({
    agent_name: 'Jarvis',
    event_type: 'message_turn',
    timestamp: 2,
    data: {
      message: { role: 'assistant', tool_calls: { x: {} }, stop_reason: 'toolUse' },
    },
  })
  assert.equal(store.agents.get('Jarvis').status, 'running')

  // Final turn arrives. Not paused → flip to idle.
  store.processEvent({
    agent_name: 'Jarvis',
    event_type: 'message_turn',
    timestamp: 3,
    data: {
      message: { role: 'assistant', stop_reason: 'endTurn' },
    },
  })
  assert.equal(store.agents.get('Jarvis').status, 'idle')
})

// Pausing and resuming states must also be protected — same race
// window applies if message_turn arrives during the transitional
// states (less likely but the guard is whitelist-based so we pin it).
test('message_turn does not clobber pausing or resuming states', () => {
  const store = useAgentsStore()
  store.processEvent({
    agent_name: 'X', event_type: 'agent_added', timestamp: 1, data: {},
  })

  for (const transitional of ['pausing', 'resuming']) {
    store.processEvent({
      agent_name: 'X',
      event_type: transitional === 'pausing' ? 'agent_pausing' : 'agent_resuming',
      timestamp: 2,
      data: { status: transitional },
    })
    assert.equal(store.agents.get('X').status, transitional)

    store.processEvent({
      agent_name: 'X',
      event_type: 'message_turn',
      timestamp: 3,
      data: { message: { role: 'assistant', stop_reason: 'endTurn' } },
    })
    assert.equal(
      store.agents.get('X').status, transitional,
      `message_turn must not clobber ${transitional}`,
    )
  }
})


// Race triplet: `result` / `idle` / `response` events fire from the
// chat lifecycle whenever a turn completes. If they arrive AFTER an
// `agent_paused` SSE event, they used to clobber status back to
// 'idle'. The pause-cycle guard now blocks the status field on all
// three. This pins the same invariant the message_turn test covers,
// across every clobber path.
for (const ev of ['result', 'idle', 'response']) {
  test(`${ev} event does not clobber paused status`, () => {
    const store = useAgentsStore()
    store.processEvent({
      agent_name: 'Jarvis', event_type: 'agent_added', timestamp: 1, data: {},
    })
    store.processEvent({
      agent_name: 'Jarvis', event_type: 'agent_paused', timestamp: 2,
      data: { status: 'paused' },
    })
    assert.equal(store.agents.get('Jarvis').status, 'paused')

    store.processEvent({
      agent_name: 'Jarvis', event_type: ev, timestamp: 3, message: 'Done', data: {},
    })
    assert.equal(
      store.agents.get('Jarvis').status, 'paused',
      `${ev} must respect pause-cycle status`,
    )
  })
}

// Counterpoint: same events MUST set idle when not in pause cycle.
test('result/idle/response set idle when not in pause cycle', () => {
  for (const ev of ['result', 'idle', 'response']) {
    const store = useAgentsStore()
    store.processEvent({
      agent_name: 'X', event_type: 'agent_added', timestamp: 1, data: {},
    })
    // Bootstrap running.
    store.processEvent({
      agent_name: 'X', event_type: 'message_turn', timestamp: 2,
      data: { message: { role: 'assistant', tool_calls: { x: {} } } },
    })
    assert.equal(store.agents.get('X').status, 'running')

    store.processEvent({
      agent_name: 'X', event_type: ev, timestamp: 3, message: 'Done', data: {},
    })
    assert.equal(
      store.agents.get('X').status, 'idle',
      `${ev} must still mark idle in the non-paused happy path`,
    )
  }
})


// Subprocess event tail: after pause, subprocess hook may emit a
// trailing batch of in-flight events (thinking, tool_call, tool_result,
// started, resumed) before its next checkpoint actually blocks. Each
// must respect the pause-cycle status — pre-fix, these unconditionally
// flipped status to 'running' a few seconds after pause, bouncing the
// UI back from Paused → Running and confusing the user (observed
// 2026-05-24 with team members like QE / PM).
for (const ev of ['thinking', 'tool_call', 'tool_result', 'started', 'resumed']) {
  test(`${ev} event does not clobber paused status`, () => {
    const store = useAgentsStore()
    store.processEvent({
      agent_name: 'QE', event_type: 'agent_added', timestamp: 1, data: {},
    })
    store.processEvent({
      agent_name: 'QE', event_type: 'agent_paused', timestamp: 2,
      data: { status: 'paused' },
    })
    assert.equal(store.agents.get('QE').status, 'paused')

    store.processEvent({
      agent_name: 'QE', event_type: ev, timestamp: 3, message: 'x', data: {},
    })
    assert.equal(
      store.agents.get('QE').status, 'paused',
      `${ev} must respect pause-cycle status`,
    )
  })
}

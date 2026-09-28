import { test } from 'node:test'
import assert from 'node:assert/strict'
import { effectScope, nextTick, watch } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { useAgentsStore } from '../stores/agents.js'
import { useAgentTurns, ingestMessageTurnEvent } from './useAgentTurns.js'
import { agentIdentity } from './agentIdentity.js'

test('ingestMessageTurnEvent rejects missing/wrong sessions and inserts only into valid session bucket', () => {
  const roster = [
    { name: 'Worker', session_id: 'session-a' },
    { name: 'Worker', session_id: 'session-b' },
  ]
  const turns = new Map()
  assert.equal(ingestMessageTurnEvent(turns, roster, { event_type: 'message_turn', agent_name: 'Worker', data: { turn_idx: 1 } }), false)
  assert.equal(ingestMessageTurnEvent(turns, roster, { event_type: 'message_turn', agent_name: 'Worker', session_id: 'invalid', data: { turn_idx: 2 } }), false)
  assert.equal(turns.size, 0)
  assert.equal(ingestMessageTurnEvent(turns, roster, { event_type: 'message_turn', agent_name: 'Worker', session_id: 'session-a', data: { turn_idx: 3 } }), true)
  assert.deepEqual(turns.get(agentIdentity(roster[0])).map(turn => turn.turn_idx), [3])
  assert.equal(turns.has(agentIdentity(roster[1])), false)
})

test('useAgentTurns watcher delegates incoming SSE events to production ingestion function', async () => {
  setActivePinia(createPinia())
  const store = useAgentsStore()
  const teamA = { name: 'Worker', session_id: 'session-a' }
  const teamB = { name: 'Worker', session_id: 'session-b' }
  store.agents.set(agentIdentity(teamA), teamA)
  store.agents.set(agentIdentity(teamB), teamB)

  const scope = effectScope()
  const agentTurns = scope.run(() => useAgentTurns())
  try {
    store.recentEvents = [{ event_type: 'message_turn', agent_name: 'Worker', data: { turn_idx: 1 } }]
    await nextTick()
    assert.equal(agentTurns.turns.value.size, 0)
    store.recentEvents = [{ event_type: 'message_turn', agent_name: 'Worker', session_id: 'session-b', data: { turn_idx: 2 } }]
    await nextTick()
    assert.deepEqual(agentTurns.getTurns(teamA), [])
    assert.deepEqual(agentTurns.getTurns(teamB).map(turn => turn.turn_idx), [2])
  } finally {
    scope.stop()
  }
})

test('live SSE stays visible when a slower initial history response arrives', async () => {
  setActivePinia(createPinia())
  const store = useAgentsStore()
  const agent = { name: 'Jarvis' }
  store.agents.set(agentIdentity(agent), agent)
  const originalFetch = globalThis.fetch
  let releaseHistory
  globalThis.fetch = () => new Promise(resolve => { releaseHistory = resolve })
  const scope = effectScope()
  const agentTurns = scope.run(() => useAgentTurns())
  const observed = []
  scope.run(() => watch(agentTurns.turns, map => {
    observed.push(map.get(agentIdentity(agent))?.length || 0)
  }))
  try {
    const pending = agentTurns.fetchInitial(agent)
    store.recentEvents = [{
      event_type: 'message_turn', agent_name: 'Jarvis', run_id: 'run-1',
      data: { turn_idx: 2, role: 'assistant', message: { content: [{ text: 'live' }] } },
    }]
    await nextTick()
    assert.deepEqual(observed, [1], 'the SSE delta must trigger a reactive render')

    releaseHistory(new Response(JSON.stringify({ turns: [
      { turn_idx: 0, role: 'user', message: { content: [{ text: 'request' }] } },
      { turn_idx: 1, role: 'assistant', message: { content: [{ text: 'history' }] } },
    ] }), { headers: { 'content-type': 'application/json' } }))
    await pending
    assert.deepEqual(agentTurns.getTurns(agent).map(turn => turn.turn_idx), [0, 1, 2])
  } finally {
    scope.stop()
    globalThis.fetch = originalFetch
  }
})

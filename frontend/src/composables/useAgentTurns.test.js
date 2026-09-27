import { test } from 'node:test'
import assert from 'node:assert/strict'
import { effectScope, nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { useAgentsStore } from '../stores/agents.js'
import { useAgentTurns } from './useAgentTurns.js'
import { agentIdentity } from './agentIdentity.js'

test('useAgentTurns watcher drops missing/wrong session SSE and buckets valid turn by session', async () => {
  setActivePinia(createPinia())
  const store = useAgentsStore()
  const teamA = { name: 'Worker', session_id: 'session-a' }
  const teamB = { name: 'Worker', session_id: 'session-b' }
  store.agents.set(agentIdentity(teamA), teamA)
  store.agents.set(agentIdentity(teamB), teamB)

  const scope = effectScope()
  const agentTurns = scope.run(() => useAgentTurns())
  try {
    for (const event of [
      { agent_name: 'Worker', data: { turn_idx: 1 } },
      { agent_name: 'Worker', session_id: 'not-a-session', data: { turn_idx: 2 } },
    ]) {
      store.recentEvents = [{ event_type: 'message_turn', ...event }]
      await nextTick()
    }
    assert.equal(agentTurns.turns.value.size, 0, 'missing/wrong session must create no bucket')

    store.recentEvents = [{
      event_type: 'message_turn', agent_name: 'Worker', session_id: 'session-a',
      data: { turn_idx: 3, role: 'assistant', message: { role: 'assistant' } },
    }]
    await nextTick()
    assert.deepEqual(agentTurns.getTurns(teamA).map(turn => turn.turn_idx), [3])
    assert.deepEqual(agentTurns.getTurns(teamB), [], 'turn must not leak to the other team')
  } finally {
    scope.stop()
  }
})

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createPinia, setActivePinia } from 'pinia'
import { useAgentsStore } from '../stores/agents.js'
import { agentIdentity, findAgentByIdentity } from './agentIdentity.js'
import { insertTurn } from './agentTurnsUtils.js'

/** Exercise the exact SSE bridge identity decision and turn bucket behavior. */
test('useAgentTurns bridge rejects missing/wrong session message_turn for duplicate names', () => {
  setActivePinia(createPinia())
  const store = useAgentsStore()
  const teamA = { name: 'Worker', session_id: 'session-a' }
  const teamB = { name: 'Worker', session_id: 'session-b' }
  store.agents.set(agentIdentity(teamA), teamA)
  store.agents.set(agentIdentity(teamB), teamB)

  const turns = new Map()
  const onSseEvent = event => {
    if (event?.event_type !== 'message_turn') return
    const agent = findAgentByIdentity(store.agentsList, event.agent_name, event.session_id)
    if (!agent) return
    const key = agentIdentity(agent)
    turns.set(key, insertTurn(turns.get(key) || [], event.data, 50))
  }

  onSseEvent({ event_type: 'message_turn', agent_name: 'Worker', data: { turn_idx: 1 } })
  onSseEvent({ event_type: 'message_turn', agent_name: 'Worker', session_id: 'not-a-session', data: { turn_idx: 2 } })
  assert.equal(turns.size, 0, 'missing/wrong session events must not create any history bucket')

  onSseEvent({ event_type: 'message_turn', agent_name: 'Worker', session_id: 'session-a', data: { turn_idx: 3 } })
  assert.deepEqual(turns.get(agentIdentity(teamA)).map(turn => turn.turn_idx), [3])
  assert.equal(turns.has(agentIdentity(teamB)), false, 'session A turn must not leak into session B')
})

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { agentIdentity, findAgentByIdentity } from './agentIdentity.js'
import { insertTurn } from './agentTurnsUtils.js'

test('message_turn SSE with missing or wrong session cannot assign turns across duplicate-name teams', () => {
  const roster = [
    { name: 'Worker', session_id: 'session-a' },
    { name: 'Worker', session_id: 'session-b' },
  ]
  const turns = new Map()
  const ingestMessageTurn = event => {
    if (event.event_type !== 'message_turn') return
    const agent = findAgentByIdentity(roster, event.agent_name, event.session_id)
    if (!agent) return
    const key = agentIdentity(agent)
    turns.set(key, insertTurn(turns.get(key) || [], event.data, 50))
  }

  ingestMessageTurn({ event_type: 'message_turn', agent_name: 'Worker', data: { turn_idx: 1 } })
  ingestMessageTurn({ event_type: 'message_turn', agent_name: 'Worker', session_id: 'unknown', data: { turn_idx: 2 } })
  assert.equal(turns.size, 0)

  ingestMessageTurn({ event_type: 'message_turn', agent_name: 'Worker', session_id: 'session-a', data: { turn_idx: 3 } })
  assert.deepEqual(turns.get(agentIdentity(roster[0])).map(turn => turn.turn_idx), [3])
  assert.equal(turns.has(agentIdentity(roster[1])), false)
})

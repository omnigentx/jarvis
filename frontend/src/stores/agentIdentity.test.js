import { test } from 'node:test'
import assert from 'node:assert/strict'
import { agentKey, findAgent } from './agentIdentity.js'

test('roster key and lookup keep same-named agents from separate sessions distinct', () => {
  const a = { name: 'Worker', session_id: 'session-a' }
  const b = { name: 'Worker', session_id: 'session-b' }
  const roster = new Map([[agentKey(a), a], [agentKey(b), b]])
  assert.equal(roster.size, 2)
  assert.equal(findAgent(roster, 'Worker', 'session-a'), a)
  assert.equal(findAgent(roster, 'Worker', 'session-b'), b)
})

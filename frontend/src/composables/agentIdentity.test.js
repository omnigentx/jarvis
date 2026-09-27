import { test } from 'node:test'
import assert from 'node:assert/strict'
import { agentIdentity } from './agentIdentity.js'

test('agent identity distinguishes same-named agents from separate sessions', () => {
  assert.notEqual(
    agentIdentity({ name: 'Worker', session_id: 'team-a' }),
    agentIdentity({ name: 'Worker', session_id: 'team-b' }),
  )
})

test('agent identity is stable for same session and name', () => {
  assert.equal(
    agentIdentity({ name: 'Worker', session_id: 'team-a' }),
    agentIdentity({ name: 'Worker', session_id: 'team-a' }),
  )
})

test('roster event lookup fails closed for missing or wrong session on duplicate names', async () => {
  const { findAgentByIdentity } = await import('./agentIdentity.js')
  const roster = [
    { name: 'Worker', session_id: 'session-a' },
    { name: 'Worker', session_id: 'session-b' },
  ]
  assert.equal(findAgentByIdentity(roster, 'Worker', undefined), undefined)
  assert.equal(findAgentByIdentity(roster, 'Worker', 'session-c'), undefined)
  assert.equal(findAgentByIdentity(roster, 'Worker', 'session-a'), roster[0])
})

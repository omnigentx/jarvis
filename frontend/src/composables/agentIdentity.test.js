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

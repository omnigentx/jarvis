import { test } from 'node:test'
import assert from 'node:assert/strict'
import { decodeTeamFilter, groupAgentsByTeam, makeTeamFilter, teamIdentity, teamLabel } from './teamIdentity.js'

test('same team names across sessions stay distinct with readable labels', () => {
  const agents = [
    { name: 'Alpha', team_name: 'Review', session_id: 'session-a' },
    { name: 'Beta', team_name: 'Review', session_id: 'session-b' },
  ]
  const groups = groupAgentsByTeam(agents)
  assert.equal(groups.length, 2)
  assert.notEqual(teamIdentity(groups[0]), teamIdentity(groups[1]))
  assert.deepEqual(groups.map(group => group.agents.map(agent => agent.name)), [['Alpha'], ['Beta']])
  assert.deepEqual(groups.map(group => teamLabel(group, groups)), ['Review (session-)', 'Review (session-)'])
  assert.deepEqual(groups.map(group => decodeTeamFilter(makeTeamFilter(group))), [
    { sessionId: 'session-a', teamName: 'Review' },
    { sessionId: 'session-b', teamName: 'Review' },
  ])
})

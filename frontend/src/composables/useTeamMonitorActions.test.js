import { test } from 'node:test'
import assert from 'node:assert/strict'
import { deleteAgentByName, deleteAgentsByName, injectToAgent, injectToAgents } from './useTeamMonitorActions.js'
import { summarizeBulkInjectResults } from '../components/monitor/bulkInjectFeedback.js'

test('single name-only delete rechecks live roster and aborts after modal-open collision', async () => {
  let roster = [{ name: 'Worker', session_id: 'a' }]
  let calls = 0
  const result = await deleteAgentByName(async () => { calls++ }, () => roster, 'Worker')
  assert.equal(result.blocked, false)
  roster = [...roster, { name: 'Worker', session_id: 'b' }]
  const raced = await deleteAgentByName(async () => { calls++ }, () => roster, 'Worker')
  assert.equal(raced.blocked, true)
  assert.equal(calls, 1)
})

test('bulk name-only delete makes zero requests if any selected name collides', async () => {
  let calls = 0
  const targets = [{ name: 'Solo' }, { name: 'Worker' }]
  const roster = [...targets, { name: 'Worker', session_id: 'other' }]
  const result = await deleteAgentsByName(async () => { calls++ }, () => roster, targets)
  assert.equal(result.blocked, true)
  assert.equal(calls, 0)
})

test('inject always scopes request by encoded session and preserves 409', async () => {
  let request
  const api = async (url, options) => { request = { url, options }; const error = new Error('ambiguous'); error.status = 409; throw error }
  await assert.rejects(injectToAgent(api, { name: 'Worker', session_id: 'team/a' }, { text: 'hi' }), error => error.status === 409)
  assert.equal(request.url, '/api/agents/Worker/inject?session_id=team%2Fa')
  assert.equal(request.options.method, 'POST')
})

test('builtin agent uses explicit static target instead of team namesake', async () => {
  let request
  await injectToAgent(async url => { request = url }, { name: 'Jarvis', type: 'builtin' }, { text: 'hi' })
  assert.equal(request, '/api/agents/Jarvis/inject?target=static')
})

test('bulk inject retains each rejected agent result', async () => {
  const agents = [{ name: 'A' }, { name: 'B' }]
  const results = await injectToAgents(async agent => { if (agent.name === 'B') { const e = new Error('ambiguous'); e.status = 409; throw e } }, agents, { text: 'hi' })
  assert.deepEqual(results.map(r => r.status), ['fulfilled', 'rejected'])
  assert.equal(results[1].reason.status, 409)
})

test('bulk inject feedback identifies each rejected target and 409 ambiguity', () => {
  const result = summarizeBulkInjectResults([{ status: 'fulfilled' }, { status: 'rejected', reason: { status: 409 } }], [{ name: 'A' }, { name: 'Worker' }], (key) => key)
  assert.deepEqual(result, { ok: 1, failures: ['Worker: ambiguousActionBlocked'] })
})

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { injectToAgent, deleteAgentByName, deleteAgentsByName } from '../composables/useTeamMonitorActions.js'

test('built-in inject preserves name-only route without a session', async () => {
  let request
  await injectToAgent(async (url, options) => { request = { url, options } }, { name: 'Jarvis', type: 'builtin' }, { text: 'hi' })
  assert.equal(request.url, '/api/agents/Jarvis/inject')
  assert.equal(request.options.method, 'POST')
})

test('dynamic agent injects require an explicit session identity', async () => {
  await assert.rejects(injectToAgent(async () => {}, { name: 'Worker', type: 'team' }, { text: 'hi' }), /Session identity required/)
})

test('inject request includes session_id and propagates 409', async () => {
  let call
  const api = async (url, options) => { call = { url, options }; const error = new Error('ambiguous'); error.status = 409; throw error }
  await assert.rejects(injectToAgent(api, { name: 'Worker', session_id: 'session-a' }, { text: 'hi' }), error => error.status === 409)
  assert.equal(call.url, '/api/agents/Worker/inject?session_id=session-a')
  assert.equal(call.options.method, 'POST')
})

test('single-delete live guard blocks name collision before fetch', async () => {
  const roster = [{ name: 'Worker', session_id: 'a' }, { name: 'Worker', session_id: 'b' }]
  let calls = 0
  const result = await deleteAgentByName(async () => calls++, () => roster, 'Worker')
  assert.equal(result.blocked, true)
  assert.equal(calls, 0)
})

test('bulk-delete preflight aborts all requests on any collision', async () => {
  const targets = [{ name: 'Solo' }, { name: 'Worker' }]
  const roster = [...targets, { name: 'Worker', session_id: 'b' }]
  let calls = 0
  const result = await deleteAgentsByName(async () => calls++, () => roster, targets)
  assert.equal(result.blocked, true)
  assert.equal(calls, 0)
})

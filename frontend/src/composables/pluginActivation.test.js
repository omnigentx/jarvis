import test from 'node:test'
import assert from 'node:assert/strict'
import { blockingCapabilities, activationTargets, activateReviewedTargets } from './pluginActivation.js'

test('policy resolves MCP review only; unsupported adapters remain blocked', () => {
  assert.deepEqual(blockingCapabilities({skills:[],blockers:['agents','host_connectors','mcp_requires_policy_review'],policy_configured:true}), ['agents','host_connectors'])
  assert.deepEqual(blockingCapabilities({skills:[],blockers:['executable_content','mcp_requires_policy_review'],policy_configured:true}), [])
  assert.deepEqual(blockingCapabilities({skills:[{}],blockers:['executable_content'],policy_configured:true}), ['executable_content'])
})
test('all targets is an explicit snapshot, deduplicated by runtime and excludes future agents', () => {
  const available = [{agent:'Jarvis',label:'Jarvis'}, {agent:'Dev',run_id:'run1',label:'Dev'}, {agent:'Dev',run_id:'run1',label:'Dev'}]
  const chosen = activationTargets('all:current', available)
  available.push({agent:'New',label:'New'})
  assert.equal(chosen.length, 2)
  assert.deepEqual(activationTargets('missing', available), [])
})
test('partial failure continues, target ACK overrides aggregate Ready, and no automatic retry', async () => {
  const reviews = ['Jarvis','Dev','QA'].map(target => ({target,plugin:{id:'one'},review_token:target}))
  const calls = []
  const results = await activateReviewedTargets(reviews, async review => {
    calls.push(review.target)
    if (review.target === 'Dev') throw new Error('runtime stopped')
    return {status:'ready',bindings:[{agent:review.target,status:review.target==='QA'?'activation_failed':'ready'}]}
  })
  assert.deepEqual(calls, ['Jarvis','Dev','QA'])
  assert.deepEqual(results.map(x=>x.status), ['ready','activation_failed','activation_failed'])
  assert.equal(results[1].error, 'runtime stopped')
})

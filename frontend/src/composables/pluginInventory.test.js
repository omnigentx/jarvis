import test from 'node:test'
import assert from 'node:assert/strict'
import { createPluginInventory } from './pluginInventory.js'

test('an older snapshot cannot overwrite a pushed runtime ACK', async () => {
  let resolve
  const inventory = createPluginInventory(() => new Promise(r => { resolve = r }))
  const pending = inventory.reload()
  inventory.onEvent({ event_type: 'plugin_status', data: { id: 'one', status: 'ready', agent: 'Jarvis' } })
  resolve({ plugins: [{ id: 'one', name: 'review', status: 'activating', bindings: [] }] })
  await pending
  assert.equal(inventory.plugins.value[0].status, 'ready')
  assert.deepEqual(inventory.plugins.value[0].bindings, [{ agent: 'Jarvis', status: 'ready' }])
})

test('unrelated events make no request', () => {
  let calls = 0
  const inventory = createPluginInventory(() => { calls++ })
  inventory.onEvent({ event_type: 'thinking', data: {} })
  assert.equal(calls, 0)
})

test('a subsequent fresh snapshot replaces an older event', async () => {
  const inventory = createPluginInventory(async () => ({ plugins: [{ id: 'one', status: 'disabled', bindings: [] }] }))
  inventory.onEvent({ event_type: 'plugin_status', data: { id: 'one', status: 'ready', agent: 'Jarvis' } })
  await inventory.reload()
  assert.equal(inventory.plugins.value[0].status, 'disabled')
})

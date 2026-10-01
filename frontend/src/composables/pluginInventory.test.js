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

test('concurrent target ACKs both survive an older in-flight snapshot', async () => {
  let resolve
  const inventory = createPluginInventory(() => new Promise(r => { resolve = r }))
  const pending = inventory.reload()
  inventory.onEvent({ event_type: 'plugin_status', data: { id: 'one', status: 'ready', agent: 'Jarvis' } })
  inventory.onEvent({ event_type: 'plugin_status', data: { id: 'one', status: 'ready', agent: 'team:second' } })
  resolve({ plugins: [{ id: 'one', status: 'activating', bindings: [] }] })
  await pending
  assert.deepEqual(inventory.plugins.value[0].bindings, [
    { agent: 'Jarvis', status: 'ready' }, { agent: 'team:second', status: 'ready' },
  ])
})


test('sharing delta updates flag without adding an agent binding', async () => {
  const inventory = createPluginInventory(async () => ({ plugins: [{ id: 'one', status: 'ready', global_enabled: false, bindings: [] }] }))
  await inventory.reload()
  inventory.onEvent({ event_type: 'plugin_status', data: { id: 'one', status: 'ready', global_enabled: true } })
  assert.equal(inventory.plugins.value[0].global_enabled, true)
  assert.deepEqual(inventory.plugins.value[0].bindings, [])
})

test('terminal runtime event invalidates only matching ready bindings without REST polling', async () => {
  let calls = 0
  const inventory = createPluginInventory(async () => { calls++; return { plugins: [{ id: 'one', status: 'ready', bindings: [
    { agent: 'team:a', run_id: 'run-a', status: 'ready' }, { agent: 'Jarvis', status: 'ready' },
  ] }] } })
  await inventory.reload()
  inventory.onEvent({ event_type: 'completed', run_id: 'run-a', data: {} })
  assert.equal(inventory.plugins.value[0].bindings[0].status, 'needs_reactivation')
  assert.equal(inventory.plugins.value[0].bindings[1].status, 'ready')
  assert.equal(inventory.plugins.value[0].status, 'ready')
  assert.equal(calls, 1)
})

test('runtime death during initial snapshot cannot restore a stale Ready', async () => {
  let resolve
  const inventory = createPluginInventory(() => new Promise(r => { resolve = r }))
  const pending = inventory.reload()
  inventory.onEvent({ event_type: 'completed', run_id: 'run-a', data: {} })
  resolve({ plugins: [{ id: 'one', status: 'ready', bindings: [
    { agent: 'team:a', run_id: 'run-a', status: 'ready' },
  ] }] })
  await pending
  assert.equal(inventory.plugins.value[0].status, 'needs_reactivation')
  assert.equal(inventory.plugins.value[0].bindings[0].status, 'needs_reactivation')
})

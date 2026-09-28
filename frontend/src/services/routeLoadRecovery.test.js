import test from 'node:test'
import assert from 'node:assert/strict'
import { createRouter, createMemoryHistory } from 'vue-router'
import { createRouteLoadRecovery, isRouteAssetError } from './routeLoadRecovery.js'

for (const message of [
  'Failed to fetch dynamically imported module: /assets/MeetingsView-old.js',
  'error loading dynamically imported module: /assets/x.js',
  'Importing a module script failed.',
  'Unable to preload CSS for /assets/x.css',
  'Loading chunk 42 failed.',
]) {
  test(`recognizes asset error: ${message}`, () => assert.equal(isRouteAssetError(new TypeError(message)), true))
}

test('does not treat API/network/runtime errors as obsolete routes', () => {
  for (const error of [null, {}, new Error('Failed to fetch'), new Error('HTTP 502'), new Error('Cannot read properties of undefined')]) {
    assert.equal(isRouteAssetError(error), false)
  }
})

function setup() {
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/chat', component: {} },
    { path: '/meetings', component: () => Promise.reject(new TypeError('Failed to fetch dynamically imported module')) },
    { path: '/other', component: {} },
    { path: '/bug', component: () => Promise.reject(new Error('render bug')) },
  ] })
  const navigations = []
  const recovery = createRouteLoadRecovery(router, { origin: 'https://jarvis.test', assign: href => navigations.push(href) })
  return { router, recovery, navigations }
}

test('real router retains current route, preserves requested query/hash, reloads only on user action', async () => {
  const { router, recovery, navigations } = setup()
  await router.push('/chat')
  await assert.rejects(router.push('/meetings?team=one#latest'))
  assert.equal(router.currentRoute.value.path, '/chat')
  assert.deepEqual(navigations, [])
  assert.equal(recovery.failure.value.href, '/meetings?team=one#latest')
  recovery.reload()
  assert.deepEqual(navigations, ['/meetings?team=one#latest'])
})

test('dismiss/repeated failure and unrelated successful navigation', async () => {
  const { router, recovery, navigations } = setup()
  await router.push('/chat')
  await assert.rejects(router.push('/meetings'))
  recovery.dismiss()
  recovery.reload()
  assert.equal(recovery.failure.value, null)
  assert.deepEqual(navigations, [])
  await assert.rejects(router.push('/meetings'))
  assert.ok(recovery.failure.value)
  await router.push('/other')
  assert.equal(recovery.failure.value, null)
  await assert.rejects(router.push('/bug'))
  assert.equal(recovery.failure.value, null)
})

test('initial route failure surfaces recovery without automatic retry', async () => {
  const { router, recovery, navigations } = setup()
  await assert.rejects(router.push('/meetings'))
  assert.ok(recovery.failure.value)
  assert.deepEqual(navigations, [])
})

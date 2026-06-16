/**
 * webrtcRecovery — ICE-failure recovery policy.
 *
 * The prod bug (iPhone/5G): a mid-session ICE failure showed the fatal
 * "WebRTC could not connect" banner on the FIRST blip, even though the WS
 * control channel was still up and a re-offer would have recovered in ~1 s.
 * These tests pin the policy: failed → restart, disconnected → grace then
 * restart (cancelled on self-heal), consecutive-failure budget, reset on
 * connected, and silence after stop().
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'

import { createWebRtcRecovery } from './webrtcRecovery.js'

function make(opts = {}) {
  const calls = { restarts: [], fails: [] }
  const r = createWebRtcRecovery({
    restart: (n) => calls.restarts.push(n),
    fail: (reason) => calls.fails.push(reason),
    graceMs: 10, // real timers, kept tiny so tests stay fast
    ...opts,
  })
  return { r, calls }
}

const tick = (ms) => new Promise((res) => setTimeout(res, ms))

test('failed → restarts immediately', () => {
  const { r, calls } = make()
  r.onState('failed')
  assert.deepEqual(calls.restarts, [1])
  assert.equal(calls.fails.length, 0)
})

test('disconnected → waits grace, then restarts if not recovered', async () => {
  const { r, calls } = make()
  r.onState('disconnected')
  assert.equal(calls.restarts.length, 0) // inside grace window — no action yet
  await tick(30)
  assert.deepEqual(calls.restarts, [1])
})

test('disconnected → connected within grace cancels the restart', async () => {
  const { r, calls } = make()
  r.onState('disconnected')
  r.onState('connected')
  await tick(30)
  assert.equal(calls.restarts.length, 0)
})

test('gives up via fail() after maxRetries consecutive failures', () => {
  const { r, calls } = make()
  r.onState('failed') // attempt 1
  r.onState('failed') // attempt 2 (new PC failed too)
  r.onState('failed') // budget exhausted → fail
  assert.deepEqual(calls.restarts, [1, 2])
  assert.equal(calls.fails.length, 1)
})

test('connected resets the retry budget — only consecutive failures count', () => {
  const { r, calls } = make()
  r.onState('failed')    // attempt 1
  r.onState('connected') // recovered — budget back to full
  r.onState('failed')    // attempt 1 again, not attempt 2
  r.onState('failed')    // attempt 2
  assert.deepEqual(calls.restarts, [1, 1, 2])
  assert.equal(calls.fails.length, 0)
})

test('onRestartError consumes budget and eventually fails', () => {
  const { r, calls } = make()
  r.onState('failed')   // attempt 1
  r.onRestartError()    // attempt 1's setup blew up → attempt 2
  r.onRestartError()    // attempt 2's setup blew up → budget exhausted
  assert.deepEqual(calls.restarts, [1, 2])
  assert.equal(calls.fails.length, 1)
})

test('stop() silences everything, including a pending grace timer', async () => {
  const { r, calls } = make()
  r.onState('disconnected')
  r.stop()
  await tick(30)
  r.onState('failed')
  assert.equal(calls.restarts.length, 0)
  assert.equal(calls.fails.length, 0)
})

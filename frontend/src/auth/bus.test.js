import { test, beforeEach } from 'node:test'
import assert from 'node:assert/strict'

import { on, emit, EVENTS, _resetForTests } from './bus.js'

beforeEach(() => _resetForTests())

test('subscribe + emit delivers payload', () => {
  let received = null
  on('foo', (p) => { received = p })
  emit('foo', { a: 1 })
  assert.deepEqual(received, { a: 1 })
})

test('multiple subscribers all fire in order', () => {
  const calls = []
  on('foo', () => calls.push('a'))
  on('foo', () => calls.push('b'))
  on('foo', () => calls.push('c'))
  emit('foo')
  assert.deepEqual(calls, ['a', 'b', 'c'])
})

test('unsubscribe stops further deliveries', () => {
  let count = 0
  const off = on('foo', () => { count++ })
  emit('foo')
  off()
  emit('foo')
  assert.equal(count, 1)
})

test('emitter snapshots the listener set so unsubscribe-during-emit is safe', () => {
  let aFired = 0
  let bFired = 0
  const offA = on('foo', () => {
    aFired++
    offA()  // remove self mid-emit
  })
  on('foo', () => { bFired++ })
  emit('foo')
  // Both should fire on this emit despite the mid-iteration unsubscribe.
  assert.equal(aFired, 1)
  assert.equal(bFired, 1)
  // Subsequent emit should not re-call A.
  emit('foo')
  assert.equal(aFired, 1)
  assert.equal(bFired, 2)
})

test('handler that throws does not break other handlers', () => {
  const errs = []
  const original = console.error
  console.error = (...args) => errs.push(args)
  try {
    let bFired = 0
    on('foo', () => { throw new Error('boom') })
    on('foo', () => { bFired++ })
    emit('foo')
    assert.equal(bFired, 1, 'second handler must still run')
    assert.equal(errs.length, 1, 'console.error called for the throwing handler')
  } finally {
    console.error = original
  }
})

test('emit on event with no subscribers is a no-op', () => {
  // Just ensure it doesn't throw.
  emit('nobody-listens', { x: 1 })
})

test('event-name constants exist for the three transitions', () => {
  assert.equal(EVENTS.EXPIRED, 'auth:expired')
  assert.equal(EVENTS.RESTORED, 'auth:restored')
  assert.equal(EVENTS.CHALLENGED, 'auth:challenged')
})

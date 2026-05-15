/**
 * Unit tests for agentTurnsUtils — pure helpers feeding useAgentTurns.
 * Run via ``npm run test:unit`` (Node's built-in test runner).
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'

import { insertTurn, isResetSignal, lastAssistantText } from './agentTurnsUtils.js'


// ── insertTurn ──────────────────────────────────────────────────────


test('insertTurn appends in sorted order', () => {
  let arr = []
  arr = insertTurn(arr, { turn_idx: 0, role: 'user' }, 100)
  arr = insertTurn(arr, { turn_idx: 1, role: 'assistant' }, 100)
  arr = insertTurn(arr, { turn_idx: 2, role: 'user' }, 100)
  assert.deepEqual(arr.map(t => t.turn_idx), [0, 1, 2])
})

test('insertTurn places out-of-order deltas in correct slot', () => {
  let arr = [
    { turn_idx: 0, role: 'user' },
    { turn_idx: 2, role: 'user' },
  ]
  arr = insertTurn(arr, { turn_idx: 1, role: 'assistant' }, 100)
  assert.deepEqual(arr.map(t => t.turn_idx), [0, 1, 2])
})

test('insertTurn replaces a duplicate turn_idx (idempotent SSE replay)', () => {
  let arr = [
    { turn_idx: 0, role: 'user', message: { content: [{ text: 'old' }] } },
  ]
  arr = insertTurn(arr, { turn_idx: 0, role: 'user', message: { content: [{ text: 'new' }] } }, 100)
  assert.equal(arr.length, 1)
  assert.equal(arr[0].message.content[0].text, 'new')
})

test('insertTurn drops oldest entries when above maxPerAgent', () => {
  let arr = []
  for (let i = 0; i < 7; i++) {
    arr = insertTurn(arr, { turn_idx: i, role: 'user' }, 5)
  }
  assert.equal(arr.length, 5)
  assert.deepEqual(arr.map(t => t.turn_idx), [2, 3, 4, 5, 6])
})

test('insertTurn does not mutate the input array', () => {
  const arr = [{ turn_idx: 0, role: 'user' }]
  const next = insertTurn(arr, { turn_idx: 1, role: 'assistant' }, 100)
  assert.equal(arr.length, 1, 'input should remain length 1')
  assert.equal(next.length, 2, 'output should be length 2')
})


// ── isResetSignal ───────────────────────────────────────────────────


test('isResetSignal true: turn_idx=0 arriving while bucket has higher idx', () => {
  const arr = [
    { turn_idx: 0 },
    { turn_idx: 1 },
    { turn_idx: 2 },
  ]
  assert.equal(isResetSignal(arr, { turn_idx: 0 }), true)
})

test('isResetSignal false: turn_idx=0 on empty bucket (initial load)', () => {
  assert.equal(isResetSignal([], { turn_idx: 0 }), false)
})

test('isResetSignal false: bucket only has turn_idx=0', () => {
  const arr = [{ turn_idx: 0 }]
  assert.equal(isResetSignal(arr, { turn_idx: 0 }), false)
})

test('isResetSignal false: non-zero turn_idx delta', () => {
  const arr = [{ turn_idx: 0 }, { turn_idx: 1 }]
  assert.equal(isResetSignal(arr, { turn_idx: 2 }), false)
})


// ── lastAssistantText ───────────────────────────────────────────────


test('lastAssistantText returns text of latest assistant turn', () => {
  const arr = [
    { turn_idx: 0, role: 'user', message: { content: [{ type: 'text', text: 'hi' }] } },
    { turn_idx: 1, role: 'assistant', message: { content: [{ type: 'text', text: 'old reply' }] } },
    { turn_idx: 2, role: 'user', message: { content: [{ type: 'text', text: 'follow up' }] } },
    { turn_idx: 3, role: 'assistant', message: { content: [{ type: 'text', text: 'new reply' }] } },
  ]
  assert.equal(lastAssistantText(arr), 'new reply')
})

test('lastAssistantText returns empty when no assistant turns', () => {
  const arr = [
    { turn_idx: 0, role: 'user', message: { content: [{ text: 'hi' }] } },
  ]
  assert.equal(lastAssistantText(arr), '')
})

test('lastAssistantText handles missing content gracefully', () => {
  const arr = [
    { turn_idx: 0, role: 'assistant', message: {} },
  ]
  assert.equal(lastAssistantText(arr), '')
})

test('lastAssistantText handles empty array', () => {
  assert.equal(lastAssistantText([]), '')
})

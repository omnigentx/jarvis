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

test('isResetSignal false: turn_idx=0 with new run_id (resume_spawn case)', () => {
  // The previous run finished at turn_idx>=1. resume_spawn starts a new
  // subprocess with run_id="r2" whose first turn is turn_idx=0. This is
  // a fresh run of the same agent — old turns must be kept so the user
  // sees the conversation stacked, not reset.
  const arr = [
    { turn_idx: 0, run_id: 'r1' },
    { turn_idx: 1, run_id: 'r1' },
  ]
  assert.equal(isResetSignal(arr, { turn_idx: 0, run_id: 'r2' }), false)
})

test('isResetSignal true: turn_idx=0 with same run_id (real history reset)', () => {
  // Same run_id restarting at turn_idx=0 is a real history clear — this
  // is the legacy ``compact`` / ``new conversation`` signal.
  const arr = [
    { turn_idx: 0, run_id: 'r1' },
    { turn_idx: 1, run_id: 'r1' },
  ]
  assert.equal(isResetSignal(arr, { turn_idx: 0, run_id: 'r1' }), true)
})

test('insertTurn dedups within same run, stacks across runs (resume case)', () => {
  // r1 contributed turn_idx 0 and 1. r2 (resumed) starts a new turn_idx=0
  // — must NOT replace r1's turn 0. Bucket should hold all 3 turns.
  let arr = []
  arr = insertTurn(arr, { turn_idx: 0, run_id: 'r1', ts: 100, role: 'user' }, 100)
  arr = insertTurn(arr, { turn_idx: 1, run_id: 'r1', ts: 101, role: 'assistant' }, 100)
  arr = insertTurn(arr, { turn_idx: 0, run_id: 'r2', ts: 200, role: 'user' }, 100)
  assert.equal(arr.length, 3)
  assert.deepEqual(arr.map(t => [t.run_id, t.turn_idx]), [
    ['r1', 0], ['r1', 1], ['r2', 0],
  ])
})

test('insertTurn still replaces an exact (run_id, turn_idx) duplicate', () => {
  // SSE replay scenario: same run, same turn arrives twice with later
  // payload (e.g. tool_result attached). Must replace, not duplicate.
  let arr = [
    { turn_idx: 0, run_id: 'r1', ts: 100, message: { content: [{ text: 'partial' }] } },
  ]
  arr = insertTurn(arr, { turn_idx: 0, run_id: 'r1', ts: 100, message: { content: [{ text: 'final' }] } }, 100)
  assert.equal(arr.length, 1)
  assert.equal(arr[0].message.content[0].text, 'final')
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

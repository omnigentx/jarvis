import { test } from 'node:test'
import assert from 'node:assert/strict'
import { applyPregenEvent } from './pregenState.js'

test('snapshot replaces stale state and keeps failures distinct from ready', () => {
  const old = new Map([['stale.txt', { status: 'generating' }]])
  const state = applyPregenEvent(old, 'snapshot', {
    failures: [{ chapter_file: 'one.txt', retry_at: 1000 }],
    generating: { chapter_file: 'two.txt', completed_chunks: 4, total_chunks: 8 },
  })
  assert.equal(state.has('stale.txt'), false)
  assert.equal(state.get('one.txt').status, 'error')
  assert.deepEqual(state.get('two.txt'), { chapter_file: 'two.txt', completed_chunks: 4, total_chunks: 8, status: 'generating' })
})

test('progress never publishes ready; ready and cancellation clear stale progress', () => {
  let state = applyPregenEvent(new Map(), 'chapter_progress', { chapter_file: 'one.txt', completed_chunks: 3, total_chunks: 3 })
  assert.equal(state.get('one.txt').status, 'generating')
  state = applyPregenEvent(state, 'chapter_ready', { chapter_file: 'one.txt' })
  assert.deepEqual(state.get('one.txt'), { chapter_file: 'one.txt', status: 'ready' })
  state = applyPregenEvent(state, 'chapter_pending', { chapter_file: 'one.txt' })
  assert.equal(state.get('one.txt').status, 'none')
})

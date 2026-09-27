import { test } from 'node:test'
import assert from 'node:assert/strict'
import { summarizeBulkInjectResults } from './bulkInjectFeedback.js'

test('bulk feedback surfaces each rejected target and 409 ambiguity text', () => {
  const out = summarizeBulkInjectResults([
    { status: 'fulfilled' },
    { status: 'rejected', reason: { status: 409 } },
    { status: 'rejected', reason: new Error('offline') },
  ], [{ name: 'Alpha' }, { name: 'Worker' }, { name: 'Beta' }], key => key === 'ambiguousActionBlocked' ? 'Ambiguous target' : key)
  assert.deepEqual(out, { ok: 1, failures: ['Worker: Ambiguous target', 'Beta: offline'] })
})

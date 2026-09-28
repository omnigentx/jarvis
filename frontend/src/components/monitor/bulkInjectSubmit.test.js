import { test } from 'node:test'
import assert from 'node:assert/strict'
import { submitBulkInject } from './bulkInjectSubmit.js'

test('submit invokes translation callback and formats success without shadowing it', async () => {
  const translations = []
  let payload
  const result = await submitBulkInject({
    text: '  hello  ', files: [], targets: [{ name: 'Worker' }],
    onSubmit: async value => { payload = value; return [{ status: 'fulfilled' }] },
    translate: (key, args) => { translations.push(key); return key === 'bulkInject.injectedTo' ? `${args.ok}/${args.total}` : key },
  })
  assert.deepEqual(payload, { text: 'hello', files: [] })
  assert.equal(result.feedback, '1/1')
  assert.deepEqual(translations, ['bulkInject.injectedTo'])
})

test('submit produces no-target feedback using translation function', async () => {
  const result = await submitBulkInject({ text: 'go', files: [], targets: [], onSubmit: async () => [], translate: key => key })
  assert.equal(result.feedback, 'bulkInject.noAgents')
})

test('submit formats per-agent 409 failure and invokes translator for feedback', async () => {
  const translated = []
  const result = await submitBulkInject({
    text: 'go', files: [], targets: [{ name: 'Worker' }],
    onSubmit: async () => [{ status: 'rejected', reason: { status: 409 } }],
    translate: (key, args) => { translated.push(key); return key === 'teamMonitor.ambiguousActionBlocked' ? 'Ambiguous team' : `${args.ok}/${args.total}` },
  })
  assert.equal(result.feedback, '0/1. Worker: Ambiguous team')
  assert.deepEqual(translated, ['teamMonitor.ambiguousActionBlocked', 'bulkInject.injectedTo'])
})

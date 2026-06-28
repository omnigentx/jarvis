/**
 * Memory store unit tests — the live-reactive SSE subset (spec §17).
 * Pinia vanilla-JS surface; no Vue runtime needed for state mutations.
 */
import { test, beforeEach } from 'node:test'
import assert from 'node:assert/strict'
import { createPinia, setActivePinia } from 'pinia'

const { useMemoryStore } = await import('./memory.js')

beforeEach(() => {
  setActivePinia(createPinia())
})

function ev(event_type, agent_name = 'Jarvis') {
  return { event_type, agent_name }
}

// Pending-candidate counting moved OUT of this store: the central approvals
// store (sidebar badge) owns it, so memory_candidate_* events are no longer
// consumed here (no per-agent count to duplicate).

test('memory_indexed bumps the index tick', () => {
  const s = useMemoryStore()
  const before = s.indexTick
  s.processMemoryEvent(ev('memory_indexed'))
  assert.equal(s.indexTick, before + 1)
})

test('retrieval_degraded sets flag; retrieval_completed clears it', () => {
  const s = useMemoryStore()
  assert.equal(s.isDegraded('Jarvis'), false)
  s.processMemoryEvent(ev('retrieval_degraded'))
  assert.equal(s.isDegraded('Jarvis'), true)
  s.processMemoryEvent(ev('retrieval_completed'))
  assert.equal(s.isDegraded('Jarvis'), false)
})

test('events without agent_name are ignored', () => {
  const s = useMemoryStore()
  s.processMemoryEvent({ event_type: 'retrieval_degraded' })
  assert.deepEqual(s.degradedByAgent, {})
})

test('memory_reranker_loading drives the global rerankerLoad progress', () => {
  // GLOBAL event (no agent_name) — must be handled before the per-agent guard,
  // like memory_indexed, so the Settings progress bar updates.
  const s = useMemoryStore()
  assert.equal(s.rerankerLoad, null)
  s.processMemoryEvent({ event_type: 'memory_reranker_loading', state: 'downloading', progress: 42, model: 'itdainb/PhoRanker' })
  assert.deepEqual(s.rerankerLoad, { state: 'downloading', progress: 42, model: 'itdainb/PhoRanker' })
  s.processMemoryEvent({ event_type: 'memory_reranker_loading', state: 'ready', progress: 100, model: 'itdainb/PhoRanker' })
  assert.equal(s.rerankerLoad.state, 'ready')
})

test('memory_reranker_loading defaults progress to 0 when omitted', () => {
  const s = useMemoryStore()
  s.processMemoryEvent({ event_type: 'memory_reranker_loading', state: 'loading', model: 'x' })
  assert.equal(s.rerankerLoad.progress, 0)
})

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

test('candidate_created increments pending; approve/reject decrements (floored)', () => {
  const s = useMemoryStore()
  assert.equal(s.pendingCount('Jarvis'), 0)
  s.processMemoryEvent(ev('memory_candidate_created'))
  s.processMemoryEvent(ev('memory_candidate_created'))
  assert.equal(s.pendingCount('Jarvis'), 2)
  s.processMemoryEvent(ev('memory_candidate_approved'))
  assert.equal(s.pendingCount('Jarvis'), 1)
  s.processMemoryEvent(ev('memory_candidate_rejected'))
  s.processMemoryEvent(ev('memory_candidate_rejected')) // already 0 → floored
  assert.equal(s.pendingCount('Jarvis'), 0)
})

test('pending is per-agent', () => {
  const s = useMemoryStore()
  s.processMemoryEvent(ev('memory_candidate_created', 'Jarvis'))
  s.processMemoryEvent(ev('memory_candidate_created', 'Riley [SA]'))
  assert.equal(s.pendingCount('Jarvis'), 1)
  assert.equal(s.pendingCount('Riley [SA]'), 1)
  assert.equal(s.pendingCount('Unknown'), 0)
})

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
  s.processMemoryEvent({ event_type: 'memory_candidate_created' })
  assert.deepEqual(s.pendingByAgent, {})
})

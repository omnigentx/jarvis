/**
 * Chat store — live "memories used" recall block (memory_recalled SSE).
 * Asserts the reactive UI update: the chip block is inserted DURING the turn,
 * before the streaming assistant placeholder, and only for the streaming
 * conversation of the matching agent.
 */
import { test, beforeEach } from 'node:test'
import assert from 'node:assert/strict'
import { createPinia, setActivePinia } from 'pinia'

// The store reads persisted meta from localStorage on setup; node:test has none.
globalThis.localStorage = {
  _d: {},
  getItem(k) { return this._d[k] ?? null },
  setItem(k, v) { this._d[k] = String(v) },
  removeItem(k) { delete this._d[k] },
}

const { useChatStore } = await import('./chat.js')

const MARKER = '⟦memory:recalled⟧'

beforeEach(() => {
  setActivePinia(createPinia())
})

function startStreamingTurn(s) {
  s.createConversation('Jarvis')
  s.addUserMessage('tên đầy đủ của tôi là gì')
  s.isStreaming = true
  s.addAgentMessagePlaceholder()
}

function recallData() {
  return {
    content: `${MARKER} [System memory recall — not user input]:\n- [semantic] nguyễn văn phúc là người tạo ra ai agent`,
    recall_lanes: [['fts', 'dense']],
    recall_scores: [{ rrf: 0.0325, rerank: 0.9993, conf: 0.6, authority: 'user_confirmed' }],
  }
}

test('memory_recalled inserts a recall block before the streaming reply', () => {
  const s = useChatStore()
  startStreamingTurn(s)
  s.addMemoryRecallBlock(recallData(), 'Jarvis')

  const msgs = s.activeConversation.messages
  const memIdx = msgs.findIndex(m => (m.content || '').includes(MARKER))
  const asstIdx = msgs.findIndex(m => m.role === 'assistant')
  assert.ok(memIdx >= 0, 'memory block inserted')
  assert.ok(memIdx < asstIdx, 'memory block sits before the assistant placeholder')
  assert.deepEqual(msgs[memIdx].recallLanes, [['fts', 'dense']])
  assert.equal(msgs[memIdx].recallScores[0].rrf, 0.0325)
  assert.equal(msgs[memIdx].recallScores[0].rerank, 0.9993)
  assert.equal(msgs[memIdx].role, 'user') // isMemoryBlock() in ChatMessages keys on role=user + marker
})

test('memory_recalled is ignored when no turn is streaming', () => {
  const s = useChatStore()
  s.createConversation('Jarvis')
  s.addUserMessage('hi')
  s.addMemoryRecallBlock(recallData(), 'Jarvis') // isStreaming === false
  assert.ok(!s.activeConversation.messages.some(m => (m.content || '').includes(MARKER)))
})

test('memory_recalled is ignored for a different agent', () => {
  const s = useChatStore()
  startStreamingTurn(s)
  s.addMemoryRecallBlock(recallData(), 'SomeOtherAgent')
  assert.ok(!s.activeConversation.messages.some(m => (m.content || '').includes(MARKER)))
})

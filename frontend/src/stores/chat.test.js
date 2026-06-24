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

// ── memory_saved live chip (auto-save + pending approval) ──
function saved(id, status, extra = {}) {
  return { candidate_id: id, content: `fact ${id}`, memory_type: 'semantic', status, ...extra }
}
function savedBlock(s) { return s.activeConversation.messages.find(m => m.isMemorySaved) }

test('memory_saved inserts a chip; same-turn saves batch into one block', () => {
  const s = useChatStore()
  s.createConversation('Jarvis')
  s.addUserMessage('tôi tên Phúc, landing page là x')
  s.addMemorySavedBlock(saved('c1', 'saved', { record_id: 'r1' }), 'Jarvis')
  s.addMemorySavedBlock(saved('c2', 'saved', { record_id: 'r2' }), 'Jarvis')
  const blocks = s.activeConversation.messages.filter(m => m.isMemorySaved)
  assert.equal(blocks.length, 1, 'one block for the turn')
  assert.equal(blocks[0].memorySaved.length, 2, 'both items batched')
  assert.equal(blocks[0].memorySaved[0].recordId, 'r1')
})

test('memory_saved transition pending→saved updates the same item in place', () => {
  const s = useChatStore()
  s.createConversation('Jarvis')
  s.addUserMessage('số thẻ của tôi là ...')
  s.addMemorySavedBlock(saved('c1', 'pending'), 'Jarvis')
  assert.equal(savedBlock(s).memorySaved[0].status, 'pending')
  s.addMemorySavedBlock(saved('c1', 'saved', { record_id: 'r9' }), 'Jarvis')
  assert.equal(savedBlock(s).memorySaved.length, 1, 'no duplicate item')
  assert.equal(savedBlock(s).memorySaved[0].status, 'saved')
  assert.equal(savedBlock(s).memorySaved[0].recordId, 'r9')
})

test('memory_saved rejected for a never-seen candidate is ignored', () => {
  const s = useChatStore()
  s.createConversation('Jarvis')
  s.addUserMessage('hi')
  s.addMemorySavedBlock(saved('cX', 'rejected'), 'Jarvis')
  assert.equal(savedBlock(s), undefined)
})

test('a new user turn starts a fresh saved block', () => {
  const s = useChatStore()
  s.createConversation('Jarvis')
  s.addUserMessage('turn 1')
  s.addMemorySavedBlock(saved('a', 'saved', { record_id: 'r' }), 'Jarvis')
  s.addUserMessage('turn 2')
  s.addMemorySavedBlock(saved('b', 'saved', { record_id: 'r' }), 'Jarvis')
  assert.equal(s.activeConversation.messages.filter(m => m.isMemorySaved).length, 2)
})

test('memory_saved is ignored for a different agent', () => {
  const s = useChatStore()
  s.createConversation('Jarvis')
  s.addUserMessage('hi')
  s.addMemorySavedBlock(saved('c1', 'saved', { record_id: 'r' }), 'SomeOtherAgent')
  assert.equal(savedBlock(s), undefined)
})

// ── conversation scoping: drop memory SSE that belongs to ANOTHER conversation ──
// (one agent owns many conversations; the backend stamps the originating id)
test('memory_recalled is dropped when conversation_id != the active conversation', () => {
  const s = useChatStore()
  startStreamingTurn(s)
  s.activeConversation.backendConversationId = 'conv-A'
  s.addMemoryRecallBlock({ ...recallData(), conversation_id: 'conv-B' }, 'Jarvis')
  assert.ok(!s.activeConversation.messages.some(m => (m.content || '').includes(MARKER)),
    'recall from another conversation must not paint here')
})

test('memory_recalled is inserted when conversation_id matches', () => {
  const s = useChatStore()
  startStreamingTurn(s)
  s.activeConversation.backendConversationId = 'conv-A'
  s.addMemoryRecallBlock({ ...recallData(), conversation_id: 'conv-A' }, 'Jarvis')
  assert.ok(s.activeConversation.messages.some(m => (m.content || '').includes(MARKER)))
})

test('memory_recalled still shows on a fresh conversation (no backendConversationId yet)', () => {
  const s = useChatStore()
  startStreamingTurn(s)               // backendConversationId is null on a new conv
  s.addMemoryRecallBlock({ ...recallData(), conversation_id: 'conv-A' }, 'Jarvis')
  assert.ok(s.activeConversation.messages.some(m => (m.content || '').includes(MARKER)),
    'first turn (id not resolved yet) is back-compat: still shows')
})

test('memory_saved is dropped when conversation_id != the active conversation', () => {
  const s = useChatStore()
  s.createConversation('Jarvis')
  s.addUserMessage('hi')
  s.activeConversation.backendConversationId = 'conv-A'
  s.addMemorySavedBlock(saved('c1', 'saved', { record_id: 'r1', conversation_id: 'conv-B' }), 'Jarvis')
  assert.equal(savedBlock(s), undefined, 'saved chip from another conversation must not paint here')
})

test('memory_saved is inserted when conversation_id matches', () => {
  const s = useChatStore()
  s.createConversation('Jarvis')
  s.addUserMessage('hi')
  s.activeConversation.backendConversationId = 'conv-A'
  s.addMemorySavedBlock(saved('c1', 'saved', { record_id: 'r1', conversation_id: 'conv-A' }), 'Jarvis')
  assert.ok(savedBlock(s), 'matching conversation shows the chip')
})

// ── inline chip actions: optimistic flip + revert-on-error (real apiFetch → fetch) ──
function stubFetch(impl) {
  const calls = []
  globalThis.fetch = async (url, opts = {}) => {
    calls.push({ url: String(url), method: (opts.method || 'GET').toUpperCase() })
    return impl()
  }
  return calls
}
const OK = () => ({ ok: true, status: 200, headers: { get: () => null }, text: async () => '' })
const BOOM = () => { throw new Error('network down') }

function pendingItem(s, id = 'c1') {
  s.createConversation('Jarvis')
  s.addUserMessage('số thẻ của tôi là ...')
  s.addMemorySavedBlock(saved(id, 'pending'), 'Jarvis')
  return savedBlock(s).memorySaved[0]
}

test('approveSavedMemory flips pending→saved and POSTs the approve route', async () => {
  const s = useChatStore()
  const item = pendingItem(s)
  const calls = stubFetch(OK)
  await s.approveSavedMemory(item)
  assert.equal(item.status, 'saved')
  assert.equal(calls.length, 1)
  assert.equal(calls[0].method, 'POST')
  assert.ok(calls[0].url.endsWith('/api/agents/Jarvis/memory-candidates/c1/approve'))
})

test('rejectSavedMemory flips pending→rejected and POSTs the reject route', async () => {
  const s = useChatStore()
  const item = pendingItem(s)
  const calls = stubFetch(OK)
  await s.rejectSavedMemory(item)
  assert.equal(item.status, 'rejected')
  assert.ok(calls[0].url.endsWith('/api/agents/Jarvis/memory-candidates/c1/reject'))
})

test('archiveSavedMemory flips saved→archived and POSTs the archive route', async () => {
  const s = useChatStore()
  s.createConversation('Jarvis')
  s.addUserMessage('tôi tên Phúc')
  s.addMemorySavedBlock(saved('c1', 'saved', { record_id: 'r1' }), 'Jarvis')
  const item = savedBlock(s).memorySaved[0]
  const calls = stubFetch(OK)
  await s.archiveSavedMemory(item)
  assert.equal(item.status, 'archived')
  assert.ok(calls[0].url.endsWith('/api/agents/Jarvis/memories/r1/archive'))
})

test('a failed approve reverts the optimistic status (no silent loss)', async () => {
  const s = useChatStore()
  const item = pendingItem(s)
  stubFetch(BOOM)
  await s.approveSavedMemory(item)        // catches internally, never rejects
  assert.equal(item.status, 'pending', 'reverted to the pre-click status')
})

test('archiveSavedMemory is a no-op without a recordId (nothing to archive yet)', async () => {
  const s = useChatStore()
  const item = pendingItem(s)             // pending → recordId is null
  const calls = stubFetch(OK)
  await s.archiveSavedMemory(item)
  assert.equal(calls.length, 0, 'no request fired')
  assert.equal(item.status, 'pending', 'status untouched')
})

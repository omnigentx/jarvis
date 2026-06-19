/**
 * Regression: hands-free voice "nói chen" (barge-in before the first reply
 * lands) must NOT delete the user's previous message bubble.
 *
 * Reproduces the "câu trước biến mất" bug: user says A, then says B before
 * Jarvis answers A (A's turn is cancelled by barge-in on the backend, so its
 * placeholder spinner is orphaned). The frontend used to treat B as an STT
 * correction of A and delete A — losing what the user actually said.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'

import { applyVoiceUserMessage } from './voiceChatBinding.js'

// Minimal faithful chatStore: only the surface applyVoiceUserMessage touches,
// behaving like the real Pinia store (addUserMessage pushes a {id,role} row,
// addAgentMessagePlaceholder pushes an assistant placeholder, removeMessage
// splices by id).
function makeStore() {
  let seq = 0
  const conv = { messages: [] }
  return {
    activeConversation: conv,
    activeAgentName: 'Jarvis',
    createConversation() { /* conv already active */ },
    addUserMessage(content) { conv.messages.push({ id: `u${++seq}`, role: 'user', content }) },
    addAgentMessagePlaceholder() { const id = `a${++seq}`; conv.messages.push({ id, role: 'assistant', content: '', isStreaming: true }); return id },
    removeMessage(id) { const i = conv.messages.findIndex(m => m.id === id); if (i !== -1) conv.messages.splice(i, 1) },
    _texts() { return conv.messages.filter(m => m.role === 'user').map(m => m.content) },
    _conv: conv,
  }
}

test('barge-in before the first reply keeps the previous user message', () => {
  const store = makeStore()
  let pending = null

  // Turn A: user says "Xin chào" → user_message → agent_thinking (placeholder).
  pending = applyVoiceUserMessage(store, pending, 'Xin chào')
  pending = store.addAgentMessagePlaceholder()      // agent_thinking sets pending

  // Turn B arrives BEFORE A is answered ("nói chen"): backend already cancelled
  // A's turn, so its placeholder is orphaned — but A is a real spoken sentence.
  pending = applyVoiceUserMessage(store, pending, 'tổng hợp các tin tức AI')

  // Both utterances the user spoke must survive; only the orphaned spinner goes.
  assert.deepEqual(store._texts(), ['Xin chào', 'tổng hợp các tin tức AI'])
  assert.equal(store._conv.messages.filter(m => m.role === 'assistant' && m.isStreaming).length, 0,
    'orphaned thinking placeholder should be cleared')
  assert.equal(pending, null)
})

test('normal sequence (reply landed before next turn) is unaffected', () => {
  const store = makeStore()
  let pending = null
  pending = applyVoiceUserMessage(store, pending, 'first')
  pending = store.addAgentMessagePlaceholder()
  pending = null                                    // assistant_message cleared it
  // remove the streaming flag to mimic a finalized reply
  store._conv.messages.forEach(m => { if (m.role === 'assistant') m.isStreaming = false })
  pending = applyVoiceUserMessage(store, pending, 'second')
  assert.deepEqual(store._texts(), ['first', 'second'])
})

/**
 * voiceChatBinding — the chatStore mutation a hands-free voice ``user_message``
 * event performs, extracted from useVoiceSession so it is unit-testable without
 * WebSocket / mic / AudioContext shims (same pattern as playbackDoneTracker).
 *
 * Returns the new ``pendingAgentMsgId``.
 */
export function applyVoiceUserMessage(chatStore, pendingAgentMsgId, text) {
  if (!chatStore.activeConversation && typeof chatStore.createConversation === 'function') {
    chatStore.createConversation(chatStore.activeAgentName || null)
  }
  if (typeof chatStore.addUserMessage !== 'function' || !text) {
    return pendingAgentMsgId
  }
  // A user_message ALWAYS corresponds to a FINAL (complete) STT utterance — the
  // backend only emits it after final_transcript, never as a half-correction.
  // So a second one arriving while the previous turn is still pending is a
  // genuine NEW sentence the user spoke ("nói chen" / barge-in before the reply
  // landed), NOT a correction of the previous one. KEEP the previous user
  // bubble; only the orphaned "thinking" placeholder of the turn the backend
  // cancelled is dropped. (Deleting the previous user message here was the
  // "câu trước biến mất" bug.)
  if (pendingAgentMsgId) {
    try { chatStore.removeMessage?.(pendingAgentMsgId) } catch { /* best-effort */ }
    pendingAgentMsgId = null
  }
  chatStore.addUserMessage(text)
  return pendingAgentMsgId
}

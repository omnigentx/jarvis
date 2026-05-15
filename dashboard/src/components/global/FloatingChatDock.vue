<script setup>
/**
 * FloatingChatDock — minimisable chat widget visible on every route
 * EXCEPT /chat (where the full ChatView already takes the space).
 *
 * Two modes:
 *  • collapsed: small floating button (bottom-left). Shows 🟢 streaming
 *    indicator when the agent is mid-turn so the user knows their last
 *    message is still being answered.
 *  • expanded: docked panel (bottom-left, fixed) with the active
 *    conversation's last few messages, plus a minimal input. Reuses the
 *    same chatStore + useChatStream as ChatView so message state is
 *    consistent.
 *
 * Open/close state is local to this component (per browser tab) — not
 * persisted. User toggles freely.
 */
import { ref, computed, watch, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useChatStore } from '../../stores/chat.js'
import { useChatStream } from '../../composables/useChatStream.js'
import MarkdownRenderer from '../MarkdownRenderer.vue'

const chatStore = useChatStore()
const { send, isStreaming } = useChatStream()
const route = useRoute()
const router = useRouter()

const expanded = ref(false)
const draft = ref('')
const scrollRef = ref(null)

// Hide on /chat (full UI already there) and on bare layouts (auth/setup).
const visible = computed(() => {
  if (route.name === 'Chat') return false
  if (route.meta?.layout === 'bare') return false
  return true
})

// "Streaming" badge on the collapsed button — surfaces in-flight turns
// so the user knows their last message is still being answered after
// they navigated away.
const showStreamingBadge = computed(
  () => !expanded.value && (isStreaming.value || chatStore.isStreaming),
)

// Last 6 messages of the active conversation — keep the dock small.
const recentMessages = computed(() => {
  const all = chatStore.activeMessages
  return all.length > 6 ? all.slice(-6) : all
})

const activeTitle = computed(() => chatStore.activeConversation?.title || 'Chat')

function toggle() {
  expanded.value = !expanded.value
  if (expanded.value) {
    // Lazy fetch on first expand — keeps boot-time API surface clean
    // on routes where the dock is mounted but never used (auth, setup,
    // settings, audio-player, etc). Eager onMounted fetch was leaking
    // ``GET /api/conversations`` requests into every page-load and
    // breaking unrelated e2e fixtures that assert ``backend.unexpected
    // .length === 0``.
    if (chatStore.conversations.length === 0) {
      chatStore.fetchConversations().catch(() => { /* ignored */ })
    }
    nextTick(() => scrollToBottom())
  }
}

function popOut() {
  expanded.value = false
  router.push({ name: 'Chat' })
}

function scrollToBottom() {
  if (scrollRef.value) {
    scrollRef.value.scrollTop = scrollRef.value.scrollHeight
  }
}

// Auto-scroll when new messages arrive while expanded.
watch(
  () => recentMessages.value.length,
  () => {
    if (expanded.value) nextTick(() => scrollToBottom())
  },
)

async function handleSend() {
  const text = draft.value.trim()
  if (!text || isStreaming.value) return
  draft.value = ''

  if (!chatStore.activeConversation) {
    chatStore.createConversation(chatStore.activeAgentName)
  }

  chatStore.addUserMessage(text)
  const msgId = chatStore.addAgentMessagePlaceholder()
  chatStore.isStreaming = true

  await send(
    text,
    chatStore.activeConversation?.backendConversationId,
    (event) => {
      switch (event.type) {
        case 'tool_call':
        case 'tool_request':
          chatStore.pushToolCall(msgId, {
            tool: event.tools?.[0]?.name || event.tool || 'tool',
            command: event.message || '',
            args: event.tools?.[0]?.args || null,
          })
          break
        case 'tool_result':
        case 'tool_done':
          chatStore.pushToolCall(msgId, {
            tool: event.tools?.[0]?.name || event.tool || 'tool',
            command: event.message || 'result',
            isResult: true,
            duration: event.duration_ms ? `${(event.duration_ms / 1000).toFixed(1)}s` : undefined,
            resultPreview: event.result_preview || null,
          })
          break
        case 'done':
          chatStore.finalizeAgentMessage(msgId, event.response || '', {
            conversation_id: event.conversation_id,
            audio: event.audio,
            total_tokens: event.total_tokens,
          })
          break
        case 'error':
          chatStore.setMessageError(msgId, event.message || 'Unknown error')
          break
      }
    },
    null,
    chatStore.activeAgentName,
  )

  chatStore.isStreaming = false
}

function handleKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    handleSend()
  }
}

// NOTE: NO eager fetch on mount. The fetch is moved to ``toggle()``
// (first expand) so the dock doesn't issue ``/api/conversations`` on
// every page mount — that was leaking onto routes where the dock is
// hidden via ``visible`` and breaking ``backend.unexpected.length===0``
// assertions in unrelated e2e fixtures.
// If the user goes /chat first, ChatView's own onMounted handles the
// fetch and ``chatStore.conversations`` is populated before the dock
// ever expands.
</script>

<template>
  <div v-if="visible" class="floating-dock" :class="{ expanded }">
    <!-- Expanded panel -->
    <transition name="dock-slide">
      <div v-if="expanded" class="dock-panel">
        <header class="dock-header">
          <span class="dock-title" :title="activeTitle">💬 {{ activeTitle }}</span>
          <button class="dock-icon-btn" type="button" title="Open full chat" @click="popOut">↗</button>
          <button class="dock-icon-btn" type="button" title="Minimise" @click="toggle">─</button>
        </header>

        <div ref="scrollRef" class="dock-body">
          <div v-if="!recentMessages.length" class="dock-empty">
            <p>No messages yet. Type below to chat with the agent — replies stream here without leaving this view.</p>
          </div>
          <div
            v-for="msg in recentMessages"
            :key="msg.id"
            class="dock-msg"
            :class="['dock-msg-' + msg.role, { streaming: msg.isStreaming }]"
          >
            <span class="dock-msg-role">{{ msg.role === 'user' ? 'You' : (msg.role === 'assistant' ? 'Agent' : msg.role) }}</span>
            <div class="dock-msg-content">
              <MarkdownRenderer
                v-if="msg.content"
                :content="msg.content"
                content-type="markdown"
                :enable-mermaid="false"
              />
              <span v-else-if="msg.isStreaming" class="dock-typing">
                <span class="typing-dot" /><span class="typing-dot" /><span class="typing-dot" />
              </span>
            </div>
          </div>
        </div>

        <div class="dock-input-row">
          <textarea
            v-model="draft"
            class="dock-input"
            rows="1"
            placeholder="Type a message…"
            :disabled="isStreaming"
            @keydown="handleKeydown"
          />
          <button
            class="dock-send"
            type="button"
            :disabled="!draft.trim() || isStreaming"
            @click="handleSend"
          >→</button>
        </div>
      </div>
    </transition>

    <!-- Collapsed FAB -->
    <transition name="dock-fab">
      <button
        v-if="!expanded"
        class="dock-fab"
        :class="{ streaming: showStreamingBadge }"
        type="button"
        title="Open chat dock"
        @click="toggle"
      >
        💬
        <span v-if="showStreamingBadge" class="fab-badge" title="Agent is replying">●</span>
      </button>
    </transition>
  </div>
</template>

<style scoped>
.floating-dock {
  position: fixed;
  bottom: 18px;
  left: 18px;
  z-index: 140;  /* below voice indicator (150) so they don't overlap badly */
}

/* ── Collapsed FAB ── */
.dock-fab {
  width: 48px;
  height: 48px;
  border-radius: 50%;
  background: #1e3a5f;
  border: 1px solid #2a3556;
  color: #f0f2f5;
  font-size: 18px;
  cursor: pointer;
  box-shadow: 0 6px 18px rgba(0, 0, 0, 0.40);
  position: relative;
  transition: transform 0.15s, box-shadow 0.15s, background 0.15s;
}

.dock-fab:hover {
  transform: scale(1.06);
  background: #2a4a72;
}

.dock-fab.streaming {
  animation: dock-fab-pulse 1.6s ease-in-out infinite;
}

@keyframes dock-fab-pulse {
  0%, 100% { box-shadow: 0 6px 18px rgba(0,0,0,0.4); }
  50%      { box-shadow: 0 0 0 6px rgba(59, 130, 246, 0.20), 0 6px 18px rgba(0,0,0,0.4); }
}

.fab-badge {
  position: absolute;
  top: -2px;
  right: -2px;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: #10b981;
  color: white;
  font-size: 9px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 2px solid #0c0e15;
  animation: badge-blink 1.2s infinite;
}

@keyframes badge-blink {
  0%, 100% { opacity: 1; }
  50%      { opacity: 0.55; }
}

/* ── Expanded panel ── */
.dock-panel {
  width: 360px;
  max-width: calc(100vw - 36px);
  height: 480px;
  max-height: calc(100vh - 100px);
  background: rgba(12, 14, 21, 0.97);
  border: 1px solid #1a1d2e;
  border-radius: 12px;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.5);
  backdrop-filter: blur(8px);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.dock-header {
  padding: 10px 12px;
  border-bottom: 1px solid #1a1d2e;
  display: flex;
  align-items: center;
  gap: 6px;
  background: #0a0d14;
  flex-shrink: 0;
}

.dock-title {
  flex: 1;
  font-size: 12px;
  font-weight: 600;
  color: #f0f2f5;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.dock-icon-btn {
  width: 26px;
  height: 26px;
  background: #111318;
  border: 1px solid #1a1d2e;
  color: #8b8fa3;
  border-radius: 6px;
  cursor: pointer;
  font-size: 12px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
}

.dock-icon-btn:hover {
  background: #1e2233;
  color: #f0f2f5;
}

.dock-body {
  flex: 1;
  overflow-y: auto;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.dock-body::-webkit-scrollbar { width: 4px; }
.dock-body::-webkit-scrollbar-thumb { background: #1a1d2e; border-radius: 4px; }

.dock-empty {
  color: #555872;
  font-size: 12px;
  font-style: italic;
  text-align: center;
  padding: 30px 10px;
}

.dock-msg {
  display: flex;
  flex-direction: column;
  gap: 3px;
  font-size: 12px;
  line-height: 1.5;
}

.dock-msg-role {
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: #555872;
}

.dock-msg-user .dock-msg-role { color: #3b82f6; }
.dock-msg-assistant .dock-msg-role { color: #00d4aa; }

.dock-msg-content {
  color: #c4c8d4;
  word-break: break-word;
}

.dock-msg-content :deep(p) { margin: 0 0 0.4em; }
.dock-msg-content :deep(p:last-child) { margin-bottom: 0; }
.dock-msg-content :deep(code) {
  background: #0a0d14;
  padding: 1px 4px;
  border-radius: 3px;
  font-size: 11px;
}

.dock-msg-content :deep(pre) {
  background: #0a0d14;
  padding: 6px 8px;
  border-radius: 4px;
  font-size: 10.5px;
  overflow-x: auto;
}

.dock-typing {
  display: inline-flex;
  gap: 3px;
  padding: 4px 0;
}

.typing-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #8b8fa3;
  animation: typing-bounce 1s infinite;
}
.typing-dot:nth-child(2) { animation-delay: 0.15s; }
.typing-dot:nth-child(3) { animation-delay: 0.30s; }

@keyframes typing-bounce {
  0%, 100% { opacity: 0.35; transform: scale(0.85); }
  50%      { opacity: 1;    transform: scale(1); }
}

.dock-input-row {
  border-top: 1px solid #1a1d2e;
  padding: 8px;
  display: flex;
  gap: 6px;
  background: #0a0d14;
  flex-shrink: 0;
}

.dock-input {
  flex: 1;
  background: #111318;
  border: 1px solid #1a1d2e;
  border-radius: 8px;
  padding: 7px 10px;
  color: #f0f2f5;
  font: inherit;
  font-size: 12px;
  resize: none;
  max-height: 80px;
  outline: none;
  transition: border-color 0.15s;
}

.dock-input:focus {
  border-color: #3b82f6;
}

.dock-input:disabled {
  opacity: 0.55;
}

.dock-send {
  width: 34px;
  height: 34px;
  background: #3b82f6;
  border: none;
  color: white;
  border-radius: 8px;
  font-size: 16px;
  cursor: pointer;
  flex-shrink: 0;
  transition: background 0.15s;
}

.dock-send:hover:not(:disabled) {
  background: #2563eb;
}

.dock-send:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}

/* Mobile */
@media (max-width: 767px) {
  .floating-dock {
    bottom: 78px;  /* clear bottom nav */
    left: 12px;
  }

  .dock-fab {
    width: 44px;
    height: 44px;
    font-size: 16px;
  }

  .dock-panel {
    width: calc(100vw - 24px);
    height: 60vh;
    max-height: 480px;
  }
}

/* Transitions */
.dock-slide-enter-active,
.dock-slide-leave-active {
  transition: opacity 0.2s ease, transform 0.2s ease;
  transform-origin: bottom left;
}
.dock-slide-enter-from,
.dock-slide-leave-to {
  opacity: 0;
  transform: scale(0.95) translateY(8px);
}

.dock-fab-enter-active,
.dock-fab-leave-active {
  transition: opacity 0.15s ease, transform 0.15s ease;
}
.dock-fab-enter-from,
.dock-fab-leave-to {
  opacity: 0;
  transform: scale(0.8);
}
</style>

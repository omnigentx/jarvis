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
import { useAudioPlayerStore } from '../../stores/audioPlayer.js'
import { useCrawlStatus } from '../../composables/useCrawlStatus.js'
import { useBreakpoint } from '../../composables/useBreakpoint'
import { useFabVisibility } from '../../composables/useFabVisibility'
import MarkdownRenderer from '../MarkdownRenderer.vue'

const chatStore = useChatStore()
const { send, isStreaming } = useChatStream()
const audioStore = useAudioPlayerStore()
const crawl = useCrawlStatus()
const route = useRoute()
const router = useRouter()
const { isMobile } = useBreakpoint()
const { visible: fabShown } = useFabVisibility()

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
// Agent name renders in the header as the primary identity (the "who
// am I talking to" question users ask first). Conversation title is
// shown as a secondary line so a long first-message doesn't masquerade
// as the title.
const activeAgentName = computed(() => chatStore.activeAgentName || 'Jarvis')
const isLive = computed(() => isStreaming.value || chatStore.isStreaming)

// Single initial for the avatar — keeps the dock compact at 24px.
function agentInitial(name) {
  if (!name) return 'J'
  return name.charAt(0).toUpperCase()
}

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
          // Same unified playback path as ChatView — story replies become
          // full story playback in the singleton player; plain replies are
          // chat-TTS, gated by the read-aloud preference.
          audioStore.playFromChat(event, chatStore.ttsEnabled)
          // Track a crawl started from the dock so its progress shows when the
          // user opens the full /chat view (shared singleton composable).
          if (event.crawl_job_id) crawl.track(event.crawl_job_id)
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
  <div v-if="visible" class="floating-dock jv" :class="{ expanded, 'floating-dock--off': isMobile && !fabShown && !expanded }">
    <!-- Expanded panel -->
    <transition name="dock-slide">
      <div v-if="expanded" class="dock-panel">
        <header class="dock-header">
          <span class="dock-ava" aria-hidden="true">{{ agentInitial(activeAgentName) }}</span>
          <div class="dock-head-info">
            <div class="dock-head-name">{{ activeAgentName }}</div>
            <div class="dock-head-meta">
              <span class="dock-head-dot" :class="{ live: isLive }" />
              <span class="dock-head-sub" :title="activeTitle">{{ isLive ? 'replying…' : activeTitle }}</span>
            </div>
          </div>
          <button class="dock-icon-btn" type="button" title="Open full chat" @click="popOut" aria-label="Open full chat">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
              <path d="M14 5h5v5M19 5l-9 9M5 7v12h12" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </button>
          <button class="dock-icon-btn" type="button" title="Minimise" @click="toggle" aria-label="Minimise">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
              <path d="M5 12h14" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
            </svg>
          </button>
        </header>

        <div ref="scrollRef" class="dock-body">
          <div v-if="!recentMessages.length" class="dock-empty">
            <p>No messages yet. Type below to chat with {{ activeAgentName }} — replies stream here without leaving this view.</p>
          </div>
          <div
            v-for="msg in recentMessages"
            :key="msg.id"
            class="dock-msg"
            :class="['dock-msg-' + msg.role, { streaming: msg.isStreaming }]"
          >
            <span
              class="dock-msg-ava"
              :class="msg.role === 'assistant' ? 'dock-msg-ava--agent' : 'dock-msg-ava--user'"
              aria-hidden="true"
            >{{ msg.role === 'user' ? 'U' : agentInitial(activeAgentName) }}</span>
            <div class="dock-msg-bubble">
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
          <div class="dock-input-wrap">
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
              aria-label="Send"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
                <path d="M5 12l14-7-5 14-3-6-6-1z" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="currentColor"/>
              </svg>
            </button>
          </div>
        </div>
      </div>
    </transition>

    <!-- Collapsed FAB -->
    <transition name="dock-fab">
      <!-- <button
        v-if="!expanded"
        class="dock-fab"
        :class="{ streaming: showStreamingBadge, 'dock-fab--hidden': isMobile && !fabShown }"
        type="button"
        title="Open chat dock"
        @click="toggle"
        aria-label="Open chat dock"
      >
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
          <path d="M21 12a8 8 0 0 1-11.6 7.2L4 21l1.8-5.4A8 8 0 1 1 21 12z" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        <span v-if="showStreamingBadge" class="fab-badge" title="Agent is replying" />
      </button> -->

      <button
        v-if="visible"
        class="dock-fab"
        :class="{ streaming: showStreamingBadge, 'dock-fab--hidden': isMobile && !fabShown }"
        aria-label="Open chat dock"
        @click="toggle"
      >
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
          <path d="M21 12a8 8 0 0 1-11.6 7.2L4 21l1.8-5.4A8 8 0 1 1 21 12z" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        <span v-if="showStreamingBadge" class="fab-badge" title="Agent is replying" />
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
  font-family: var(--font-body);
}
/* When hidden (grip/scroll), remove the wrapper entirely. pointer-events:none
   alone wasn't enough — the fixed 44×44 wrapper box stayed in place (the inner
   button only translates + fades), so it still read as a phantom square over
   content. display:none drops the box completely (no slide-out, but no ghost). */
.floating-dock--off { display: none; }

/* ── Collapsed FAB ── */
.dock-fab {
  position: fixed;
  left: 16px;
  /* Stack above the tab bar (56px content + safe-area) AND the mini
     audio player when it's visible. --mini-player-h is 0 by default,
     64 while playing. */
  bottom: calc(var(--mobile-tabbar-h) + var(--safe-bottom) + var(--mini-player-h, 0px) + 12px);
  z-index: 195;
  width: 52px;
  height: 52px;
  border-radius: 50%;
  overflow: hidden;
  border: 0;
  /* Solid brand gradient — crisp, no translucency/blur (the frosted version
     looked washed-out and dimmed the icon). Content clearance is handled by a
     bottom safe-zone + auto-hide-on-scroll + the grip, not by see-through. */
  background: linear-gradient(180deg, var(--primary-hover), var(--primary));
  color: white;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  box-shadow: 0 8px 24px var(--primary-glow);
  transition: transform 0.2s var(--ease-out), opacity 0.2s var(--ease-out),
              background 0.22s var(--ease-out), box-shadow 0.22s var(--ease-out);
}
.dock-fab:hover { transform: scale(1.06); }
.dock-fab:active { transform: scale(0.96); }
/* Hidden (manual grip or scroll-down, mobile only): slide off the left edge. */
.dock-fab--hidden {
  transform: translateX(-150%);
  opacity: 0;
  pointer-events: none;
  /* Drop the frosted blur when hidden — an opacity:0 element with
     backdrop-filter can still composite a faint blurred square in Chrome. */
  -webkit-backdrop-filter: none;
  backdrop-filter: none;
}
.dock-fab.streaming { animation: dock-fab-pulse 1.6s ease-in-out infinite; }

@keyframes dock-fab-pulse {
  0%, 100% { box-shadow: 0 8px 24px var(--primary-glow); }
  50%      { box-shadow: 0 0 0 6px var(--primary-bg-strong), 0 8px 24px var(--primary-glow); }
}

.fab-badge {
  position: absolute;
  top: 2px;
  right: 2px;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 6px var(--accent);
  border: 2px solid var(--bg-0);
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
  max-height: calc(100dvh - 100px);
  background: var(--bg-1);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-lg);
  box-shadow: var(--shadow-lg);
  backdrop-filter: blur(10px);
  -webkit-backdrop-filter: blur(10px);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  color: var(--text);
}

.dock-header {
  padding: 8px 10px 8px 10px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  gap: 8px;
  background: var(--bg-2);
  flex-shrink: 0;
}

.dock-ava {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  background: linear-gradient(135deg, var(--primary), var(--accent));
  color: #fff;
  font-family: var(--font-mono);
  font-size: 11px;
  font-weight: 600;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.dock-head-info { flex: 1; min-width: 0; }
.dock-head-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
  font-family: var(--font-display);
  letter-spacing: -0.005em;
  line-height: 1.2;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.dock-head-meta {
  display: flex;
  align-items: center;
  gap: 5px;
  margin-top: 1px;
  font-family: var(--font-mono);
  font-size: 9.5px;
  color: var(--text-muted);
  letter-spacing: 0.06em;
  text-transform: uppercase;
  min-width: 0;
}
.dock-head-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--text-subtle);
  flex-shrink: 0;
}
.dock-head-dot.live {
  background: var(--accent);
  box-shadow: 0 0 6px var(--accent);
  animation: pulseDot 1.4s ease-in-out infinite;
}
.dock-head-sub {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  min-width: 0;
}

.dock-icon-btn {
  width: 28px;
  height: 28px;
  background: transparent;
  border: 1px solid transparent;
  color: var(--text-muted);
  border-radius: var(--r-sm);
  cursor: pointer;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background 0.15s var(--ease-out), color 0.15s var(--ease-out);
}
.dock-icon-btn:hover {
  background: var(--bg-3);
  color: var(--text);
}

.dock-body {
  flex: 1;
  overflow-y: auto;
  padding: 12px 10px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  background: var(--bg-0);
}
.dock-body::-webkit-scrollbar { width: 4px; }
.dock-body::-webkit-scrollbar-thumb { background: var(--border-strong); border-radius: 4px; }

.dock-empty {
  color: var(--text-muted);
  font-size: 12px;
  text-align: center;
  padding: 30px 14px;
  line-height: 1.5;
}

/* Message: avatar + bubble pattern matching ChatMessages. User bubbles
   right-align with bg-3; agent bubbles left-align with primary-tinted bg. */
.dock-msg {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  font-size: 12.5px;
  line-height: 1.55;
}
.dock-msg-user { flex-direction: row-reverse; }

.dock-msg-ava {
  width: 22px;
  height: 22px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-family: var(--font-mono);
  font-size: 10px;
  font-weight: 600;
  flex-shrink: 0;
}
.dock-msg-ava--user {
  background: var(--bg-3);
  color: var(--text-dim);
  border: 1px solid var(--border-strong);
}
.dock-msg-ava--agent {
  background: linear-gradient(135deg, var(--primary), var(--accent));
  color: #fff;
}

.dock-msg-bubble {
  max-width: 82%;
  padding: 8px 11px;
  border-radius: var(--r-md);
  color: var(--text);
  word-break: break-word;
}
.dock-msg-user .dock-msg-bubble {
  background: var(--bg-3);
  border: 1px solid var(--border-strong);
}
.dock-msg-assistant .dock-msg-bubble {
  background: var(--primary-bg);
  border: 1px solid var(--primary-bg-strong);
}

.dock-msg-bubble :deep(p) { margin: 0 0 0.4em; }
.dock-msg-bubble :deep(p:last-child) { margin-bottom: 0; }
.dock-msg-bubble :deep(code) {
  background: var(--bg-2);
  border: 1px solid var(--border);
  padding: 1px 5px;
  border-radius: 4px;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--accent);
}
.dock-msg-bubble :deep(pre) {
  background: var(--bg-2);
  border: 1px solid var(--border);
  padding: 8px 10px;
  border-radius: 6px;
  font-size: 10.5px;
  overflow-x: auto;
  margin: 6px 0;
}
.dock-msg-bubble :deep(pre code) {
  background: transparent;
  border: 0;
  padding: 0;
  color: var(--text-dim);
}

.dock-typing {
  display: inline-flex;
  gap: 4px;
  padding: 4px 0;
}
.typing-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--primary-hover);
  animation: typing-bounce 1s infinite;
}
.typing-dot:nth-child(2) { animation-delay: 0.15s; }
.typing-dot:nth-child(3) { animation-delay: 0.30s; }

@keyframes typing-bounce {
  0%, 100% { opacity: 0.35; transform: scale(0.85); }
  50%      { opacity: 1;    transform: scale(1); }
}

/* Composer matching ChatInput.compose-row — pill-rounded shell with
   embedded send button so it reads as a single input affordance. */
.dock-input-row {
  border-top: 1px solid var(--border);
  padding: 8px 10px max(8px, var(--safe-bottom));
  background: var(--bg-1);
  flex-shrink: 0;
}
.dock-input-wrap {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 4px 4px 12px;
  background: var(--bg-2);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-full);
  transition: border-color 0.15s var(--ease-out);
}
.dock-input-wrap:focus-within { border-color: var(--primary-bg-strong); }

.dock-input {
  flex: 1;
  background: transparent;
  border: 0;
  outline: 0;
  padding: 4px 0;
  color: var(--text);
  font: inherit;
  font-family: var(--font-body);
  font-size: 13px;
  line-height: 1.4;
  resize: none;
  max-height: 80px;
  min-height: 22px;
}
.dock-input::placeholder { color: var(--text-muted); }
.dock-input:disabled { opacity: 0.55; }

.dock-send {
  width: 30px;
  height: 30px;
  background: linear-gradient(180deg, var(--primary-hover), var(--primary));
  border: 0;
  color: #fff;
  border-radius: 50%;
  cursor: pointer;
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  transition: transform 0.15s var(--ease-out);
  box-shadow: 0 1px 0 rgba(255,255,255,0.18) inset, 0 4px 12px -4px var(--primary-glow);
}
.dock-send:hover:not(:disabled) { transform: translateY(-1px); }
.dock-send:disabled { opacity: 0.4; cursor: not-allowed; box-shadow: none; }

/* Mobile */
@media (max-width: 767px) {
  .floating-dock {
    /* Clear bottom tab bar + safe-area + mini player when visible.
       Mirrors VoiceFAB stacking math. */
    bottom: calc(var(--mobile-tabbar-h) + var(--safe-bottom) + var(--mini-player-h, 0px) + 12px);
    left: 12px;
  }

  .dock-fab {
    /* Same size as VoiceFAB so the two FABs are visually consistent.
       Keeps the solid base gradient — no frosted/translucent (looked blurry). */
    width: 52px;
    height: 52px;
  }

  .dock-panel {
    width: calc(100vw - 24px);
    height: 60dvh;
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

<script setup>
/**
 * ChatView — Track 5 redesign.
 *
 * Wiring is unchanged: chatStore + useChatStream for typed turns,
 * useVoiceSession (singleton) for hands-free. New look only.
 *
 * Layout:
 *   - Left rail: ConversationsPanel
 *   - Right column:
 *       - ChatHeader (agent, voice readiness)
 *       - VoiceBar (mic toggle + waveform + status pill)
 *       - ChatMessages (bubble stream with STT caret / TTS dots / interrupt)
 *       - ChatInput
 *       - hidden TTS audio element
 */
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { useChatStore } from '../stores/chat'
import { useAudioPlayerStore } from '../stores/audioPlayer'
import { useAgentsStore } from '../stores/agents'
import { useCrawlStatus } from '../composables/useCrawlStatus'
import { useLang } from '../composables/useLang'
import { EVENTS, on } from '../auth/bus.js'
import { useChatStream } from '../composables/useChatStream'
import { useConfirm } from '../composables/useConfirm'
import { useToast } from '../composables/useToast'
import { useVoiceSession } from '../composables/useVoiceSession'
import { useBreakpoint } from '../composables/useBreakpoint'
import { expandToolRequest, expandToolDone } from '../utils/toolEvents'
import ConversationsPanel from '../components/chat/ConversationsPanel.vue'
import ChatHeader from '../components/chat/ChatHeader.vue'
import ChatMessages from '../components/chat/ChatMessages.vue'
import ChatInput from '../components/chat/ChatInput.vue'
import VoiceBar from '../components/chat/VoiceBar.vue'

// Explicit name so AppLayout's <keep-alive include="['Chat', ...]"> matches.
defineOptions({ name: 'Chat' })

const { isMobile } = useBreakpoint()
const showMobileConversations = ref(false)

const chatStore = useChatStore()
const audioStore = useAudioPlayerStore()
const agentsStore = useAgentsStore()
const crawl = useCrawlStatus()
// Shared UI language pref (topbar Vi/En toggle) — the crawl banner below
// renders bilingual copy off this, like every user-visible string.
const { lang } = useLang()
const { isStreaming, send, cancel } = useChatStream()
const { confirm } = useConfirm()
const toast = useToast()
const voice = useVoiceSession()

// Load the conversation list + agent roster. Both hit auth-gated APIs, so
// they must (re-)run whenever auth becomes valid — not just on first mount.
function loadWorkspace() {
  chatStore.fetchConversations()
  if (!agentsStore.agentsList.length) agentsStore.fetchAgents()
}

onMounted(() => {
  loadWorkspace()
})

// Post-passkey-login race: this view is kept-alive (AppLayout's
// <keep-alive include="['Chat']">) and AuthGate is an overlay, NOT a route
// guard — so ChatView mounts ONCE while still unauthenticated. Its onMounted
// fetch 401s silently → empty list + "No Agent", and nothing remounts it
// after login, so only a full page refresh recovered. The auth store emits
// EVENTS.RESTORED the moment login succeeds (same signal SSE/meeting/pregen
// streams already reconnect on) — re-fetch the workspace then.
const offRestored = on(EVENTS.RESTORED, () => {
  loadWorkspace()
})
onUnmounted(() => {
  offRestored()
})

watch(
  () => agentsStore.agentsList,
  (list) => {
    if (list.length && !chatStore.activeAgentName) {
      const jarvis = list.find(a => a.name === 'Jarvis')
      chatStore.setActiveAgent(jarvis ? jarvis.name : list[0].name)
    }
  },
  { immediate: true }
)

const currentAgent = computed(() => {
  const name = chatStore.activeAgentName
  return agentsStore.agentsList.find(a => a.name === name) || null
})

async function handleStop({ mode }) {
  // Hard mode is destructive: SIGTERMs running subagents. We do NOT
  // currently roll back side-effects (DB rows committed, files written,
  // external messages sent before the cancel). Confirm explicitly so
  // the user knows what they're trading off — accidentally hitting
  // shift+stop shouldn't kill 5 agents silently.
  if (mode === 'hard') {
    const proceed = await confirm({
      title: 'Force stop — terminate subagents?',
      message:
        'This will immediately SIGTERM every running subagent. ' +
        'Any side-effects already committed by them or by the orchestrator ' +
        '(database rows, written files, external API calls, sent messages) ' +
        'WILL NOT be rolled back automatically — you may need to clean them up by hand.',
      confirmText: 'Force stop',
      cancelText: 'Keep running',
      variant: 'danger',
    })
    if (!proceed) return
  }

  const res = await cancel(mode)
  // Mark the streaming flag false in the store so the chat row stops
  // showing the spinner; ChatInput already flipped its own button via
  // isStreaming prop.
  chatStore.isStreaming = false

  // Surface via toast so the user actually notices — a chat bubble can
  // get scrolled past, a toast can't.
  if (mode === 'soft') {
    toast.info('Stopped', {
      description: 'Tools the agent already called (DB writes, files, sent messages) are not rolled back.',
      duration: 6000,
    })
  } else if (res?.killed_pids?.length) {
    const names = res.killed_pids.map((k) => k.name).filter(Boolean).join(', ')
    toast.warning(`Force-stopped — ${res.killed_pids.length} subagent(s) terminated`, {
      description: `${names}. Side-effects already committed are not rolled back.`,
      duration: 8000,
    })
  } else {
    toast.info('Force-stopped', {
      description: 'No running subagents to terminate.',
      duration: 4000,
    })
  }
}

async function handleSend(payload) {
  const text = payload.text || ''
  const files = payload.files || null
  if (!text.trim() && !files?.length) return
  if (isStreaming.value) return

  if (!chatStore.activeConversation) {
    chatStore.createConversation(chatStore.activeAgentName)
  }

  const displayText = files?.length
    ? `${text}${text ? ' ' : ''}📎 ${files.map(f => f.name).join(', ')}`
    : text
  chatStore.addUserMessage(displayText)

  const msgId = chatStore.addAgentMessagePlaceholder()
  chatStore.isStreaming = true

  await send(
    text,
    chatStore.activeConversation?.backendConversationId,
    (event) => {
      switch (event.type) {
        case 'tool_call':
        case 'tool_request':
          for (const p of expandToolRequest(event)) chatStore.pushToolCall(msgId, p)
          break
        case 'tool_result':
        case 'tool_done':
          for (const p of expandToolDone(event)) chatStore.pushToolCall(msgId, p)
          break
        case 'done':
          chatStore.finalizeAgentMessage(msgId, event.response || '', {
            conversation_id: event.conversation_id,
            audio: event.audio,
            total_tokens: event.total_tokens,
          })
          // Route playback through the singleton audio player (single source
          // of truth) so it persists across navigation and shows in the
          // global mini-player. Story replies become full story playback;
          // plain replies are chat-TTS, gated by the read-aloud preference.
          audioStore.playFromChat(event, chatStore.ttsEnabled)
          // If the agent started a web crawl, attach the live-progress poller
          // (the [[[CRAWL_STARTED]]] tag is stripped from the bubble — the id
          // arrives here as structured data instead).
          if (event.crawl_job_id) crawl.track(event.crawl_job_id)
          break
        case 'error':
          chatStore.setMessageError(msgId, event.message || 'Unknown error')
          break
      }
    },
    files,
    chatStore.activeAgentName,
  )
  chatStore.isStreaming = false
}

function handleSwitchAgent(name) {
  chatStore.setActiveAgent(name)
}
</script>

<template>
  <div class="chat-root jv" :class="{ 'chat-root--mobile': isMobile }">
    <!-- Desktop sidebar -->
    <ConversationsPanel v-if="!isMobile" />

    <!-- Mobile slide-out conversations -->
    <teleport to="body">
      <transition name="conv-overlay">
        <div
          v-if="isMobile && showMobileConversations"
          class="conv-overlay"
          @click.self="showMobileConversations = false"
        >
          <div class="conv-slide-panel">
            <ConversationsPanel
              :show-close="true"
              @close="showMobileConversations = false"
              @select="showMobileConversations = false"
            />
          </div>
        </div>
      </transition>
    </teleport>

    <!-- Right column -->
    <main class="chat-main">
      <ChatHeader
        :agent="currentAgent"
        :agents="agentsStore.agentsList"
        :show-hamburger="isMobile"
        @switch-agent="handleSwitchAgent"
        @toggle-conversations="showMobileConversations = !showMobileConversations"
      />

      <VoiceBar />

      <ChatMessages
        :messages="chatStore.activeMessages"
        :agent="currentAgent"
        :isStreaming="isStreaming"
      />

      <!-- TTS Now Playing strip -->
      <div v-if="chatStore.ttsPlaying" class="tts-now-playing">
        <div class="tts-eq">
          <div class="tts-bar" style="animation-delay: 0s"></div>
          <div class="tts-bar" style="animation-delay: 0.2s"></div>
          <div class="tts-bar" style="animation-delay: 0.4s"></div>
          <div class="tts-bar" style="animation-delay: 0.1s"></div>
        </div>
        <span class="tts-label">Playing response…</span>
        <button class="tts-stop" @click="chatStore.stopTts()">
          <svg width="9" height="9" viewBox="0 0 10 10" fill="none">
            <rect x="1" y="1" width="8" height="8" rx="1" fill="var(--danger)"/>
          </svg>
          <span>Stop</span>
        </button>
      </div>

      <!-- Crawl progress strip — live chapter X/Y + cancel/dismiss -->
      <div
        v-if="crawl.jobId.value"
        class="crawl-strip"
        :class="{ done: !crawl.isActive.value, warn: crawl.needsAttention.value }"
      >
        <span class="crawl-spinner" v-if="crawl.isActive.value" />
        <span class="crawl-label">
          <template v-if="crawl.status.value === 'completed'">
            ✓ {{ lang === 'vi' ? 'Tải xong' : 'Downloaded' }}{{ crawl.storyTitle.value ? ` "${crawl.storyTitle.value}"` : '' }} — {{ crawl.current.value }}/{{ crawl.total.value }} {{ lang === 'vi' ? 'chương' : 'chapters' }}
          </template>
          <template v-else-if="crawl.status.value === 'failed' || crawl.status.value === 'error'">
            ⚠ {{ lang === 'vi' ? 'Tải truyện thất bại' : 'Story download failed' }}{{ crawl.message.value ? `: ${crawl.message.value}` : '' }}
          </template>
          <template v-else-if="crawl.status.value === 'cancelled'">
            {{ lang === 'vi' ? 'Đã huỷ tải truyện' : 'Story download cancelled' }}
          </template>
          <template v-else-if="crawl.needsAttention.value">
            ⚠ {{ lang === 'vi' ? 'Crawl gặp vấn đề — Jarvis đang xử lý…' : 'Crawl hit a snag — Jarvis is on it…' }} {{ crawl.message.value ? `(${crawl.message.value})` : '' }}
          </template>
          <template v-else>
            {{ lang === 'vi' ? 'Đang tải' : 'Downloading' }}{{ crawl.storyTitle.value ? ` "${crawl.storyTitle.value}"` : (lang === 'vi' ? ' truyện' : ' story') }}…
            {{ crawl.current.value }}/{{ crawl.total.value || '?' }} {{ lang === 'vi' ? 'chương' : 'chapters' }}
            <span v-if="crawl.total.value" class="crawl-pct">{{ crawl.percent.value }}%</span>
          </template>
        </span>
        <button v-if="crawl.isActive.value" class="crawl-btn" @click="crawl.cancel()">{{ lang === 'vi' ? 'Huỷ' : 'Cancel' }}</button>
        <button v-else class="crawl-btn" @click="crawl.dismiss()">{{ lang === 'vi' ? 'Ẩn' : 'Dismiss' }}</button>
      </div>

      <!-- Status footer (above input) -->
      <div v-if="statusFooter" class="status-footer">
        <span class="status-footer-dot" :class="{ pulse: statusFooterActive }" />
        <span class="status-footer-text">{{ statusFooter }}</span>
      </div>

      <ChatInput
        :isStreaming="isStreaming"
        @send="handleSend"
        @stop="handleStop"
      />
    </main>
  </div>
</template>

<style scoped>
.chat-root {
  display: flex;
  height: calc(100% + 48px);
  margin: -24px -36px;
  background: var(--bg-0);
  color: var(--text);
}
/* Bleed past parent's mobile content padding so the chat fills the
   viewport edge-to-edge AND the composer (last flex child) lands
   directly above the bottom tab bar / mini player / safe-area. The
   `--chat-bottom-pad` mirrors AppLayout's .app-main__content padding-
   bottom on mobile — keep these two in sync. 100dvh higher up the
   tree already collapses to exclude the iOS keyboard, so no extra
   visualViewport listener is needed. */
.chat-root--mobile {
  --chat-bottom-pad: calc(var(--mobile-tabbar-h) + var(--mini-player-h, 0px) + max(16px, var(--safe-bottom)));
  margin: -16px -12px;
  margin-bottom: calc(-1 * var(--chat-bottom-pad));
  height: calc(100% + 16px + var(--chat-bottom-pad));
  padding-bottom: var(--chat-bottom-pad);
}

.chat-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  background: var(--bg-0);
}

/* Conversations slide-out (mobile) */
.conv-overlay {
  position: fixed;
  inset: 0;
  z-index: 200;
  background: var(--bg-overlay);
  display: flex;
}
.conv-slide-panel {
  width: 300px;
  max-width: 80vw;
  height: 100%;
  background: var(--bg-1);
}
.conv-overlay-enter-active, .conv-overlay-leave-active { transition: opacity 0.2s ease; }
.conv-overlay-enter-active .conv-slide-panel,
.conv-overlay-leave-active .conv-slide-panel { transition: transform 0.25s cubic-bezier(0.4, 0, 0.2, 1); }
.conv-overlay-enter-from, .conv-overlay-leave-to { opacity: 0; }
.conv-overlay-enter-from .conv-slide-panel,
.conv-overlay-leave-to .conv-slide-panel { transform: translateX(-100%); }

/* Crawl progress strip */
.crawl-strip {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 24px;
  background: var(--primary-bg);
  border-top: 1px solid var(--primary-bg-strong);
  font-size: 12px;
  color: var(--text);
}
.crawl-strip.done { background: var(--bg-2); border-top-color: var(--border); }
.crawl-strip.warn { background: var(--warning-bg); border-top-color: rgba(245,158,11,0.30); }
.crawl-label { flex: 1; }
.crawl-pct { color: var(--primary-hover); font-weight: 600; margin-left: 4px; }
.crawl-spinner {
  width: 14px; height: 14px; flex-shrink: 0;
  border: 2px solid var(--primary-bg-strong);
  border-top-color: var(--primary-hover);
  border-radius: 50%;
  animation: crawl-spin 0.8s linear infinite;
}
@keyframes crawl-spin { to { transform: rotate(360deg); } }
.crawl-btn {
  flex-shrink: 0;
  padding: 4px 12px;
  background: var(--bg-3);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-sm);
  color: var(--text-dim);
  font-size: 11px;
  cursor: pointer;
}
.crawl-btn:hover { color: var(--text); background: var(--bg-4); }

/* Status footer */
.status-footer {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 24px;
  background: var(--bg-1);
  border-top: 1px solid var(--border);
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-muted);
  letter-spacing: 0.08em;
}
.status-footer-dot {
  width: 6px; height: 6px;
  border-radius: 50%;
  background: var(--text-muted);
}
.status-footer-dot.pulse {
  background: var(--accent);
  animation: status-pulse 1.2s ease-in-out infinite;
}
@keyframes status-pulse {
  0%, 100% { opacity: 1;    transform: scale(1);    }
  50%      { opacity: 0.45; transform: scale(1.4);  }
}

/* TTS Now Playing */
.tts-now-playing {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 24px;
  height: 36px;
  background: var(--success-bg);
  border-top: 1px solid rgba(16, 185, 129, 0.20);
}
.tts-eq { display: flex; align-items: end; gap: 2px; height: 14px; }
.tts-bar { width: 3px; background: var(--success); border-radius: 1px; animation: tts-bar-bounce 0.8s ease-in-out infinite alternate; }
@keyframes tts-bar-bounce { 0% { height: 3px; } 100% { height: 14px; } }
.tts-label { flex: 1; font-size: 11px; font-weight: 500; color: var(--success); }
.tts-stop {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 4px 12px;
  background: var(--danger-bg);
  border: 1px solid rgba(239, 68, 68, 0.30);
  border-radius: var(--r-sm);
  cursor: pointer;
  color: var(--danger);
  font-size: 11px;
  font-weight: 500;
}
</style>

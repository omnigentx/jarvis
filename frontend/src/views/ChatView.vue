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
import { ref, computed, watch, onMounted } from 'vue'
import { useChatStore } from '../stores/chat'
import { useAgentsStore } from '../stores/agents'
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
const agentsStore = useAgentsStore()
const { isStreaming, send, cancel } = useChatStream()
const { confirm } = useConfirm()
const toast = useToast()
const voice = useVoiceSession()

const ttsAudio = ref(null)

onMounted(async () => {
  if (ttsAudio.value) {
    chatStore.setTtsAudioRef(ttsAudio.value)
    ttsAudio.value.addEventListener('play',  () => { chatStore.ttsPlaying = true })
    ttsAudio.value.addEventListener('ended', () => { chatStore.ttsPlaying = false })
    ttsAudio.value.addEventListener('pause', () => { chatStore.ttsPlaying = false })
  }
  await chatStore.fetchConversations()
})

if (!agentsStore.agentsList.length) agentsStore.fetchAgents()

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
          if (chatStore.ttsEnabled && event.audio && ttsAudio.value) {
            ttsAudio.value.src = event.audio
            ttsAudio.value.play().catch(err => console.warn('[TTS] Auto-play blocked:', err))
          }
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

      <ChatInput
        :isStreaming="isStreaming"
        @send="handleSend"
        @stop="handleStop"
      />

      <audio ref="ttsAudio" style="display: none;" />
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

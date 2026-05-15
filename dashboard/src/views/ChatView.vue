<script setup>
import { ref, computed, watch, nextTick, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useChatStore } from '../stores/chat'
import { useAgentsStore } from '../stores/agents'
import { useChatStream } from '../composables/useChatStream'
import { useBreakpoint } from '../composables/useBreakpoint'
import ConversationsPanel from '../components/chat/ConversationsPanel.vue'
import ChatHeader from '../components/chat/ChatHeader.vue'
import ChatMessages from '../components/chat/ChatMessages.vue'
import ChatInput from '../components/chat/ChatInput.vue'
import VoiceBar from '../components/chat/VoiceBar.vue'

// Explicit name so AppLayout's `<keep-alive include="['Chat', ...]">` can
// match this component and preserve in-flight chat state across nav.
defineOptions({ name: 'Chat' })

const { isMobile } = useBreakpoint()
const showMobileConversations = ref(false)

const chatStore = useChatStore()
const agentsStore = useAgentsStore()
const { isStreaming, send } = useChatStream()
const router = useRouter()

// TTS audio element
const ttsAudio = ref(null)

onMounted(async () => {
  if (ttsAudio.value) {
    chatStore.setTtsAudioRef(ttsAudio.value)
    ttsAudio.value.addEventListener('play', () => {
      chatStore.ttsPlaying = true
    })
    ttsAudio.value.addEventListener('ended', () => {
      chatStore.ttsPlaying = false
    })
    ttsAudio.value.addEventListener('pause', () => {
      chatStore.ttsPlaying = false
    })
  }
  // Load conversation list from backend
  await chatStore.fetchConversations()
})

// Load agents if not yet loaded
if (!agentsStore.agentsList.length) {
  agentsStore.fetchAgents()
}

// Set default agent to Jarvis (orchestrator)
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
          chatStore.pushToolCall(msgId, {
            tool: event.tools?.[0]?.name || event.tool || event.server || 'tool',
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
          // TTS auto-play if enabled
          if (chatStore.ttsEnabled && event.audio && ttsAudio.value) {
            ttsAudio.value.src = event.audio
            ttsAudio.value.play().catch(err => {
              console.warn('[TTS] Auto-play blocked:', err)
            })
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
  <div class="flex h-full chat-root" :class="{ 'chat-root--mobile': isMobile }">
    <!-- Desktop: inline sidebar -->
    <ConversationsPanel v-if="!isMobile" />

    <!-- Mobile: slide-out overlay -->
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

    <!-- Chat Area -->
    <div class="flex-1 flex flex-col min-w-0" style="background: var(--bg-base);">
      <!-- Header -->
      <ChatHeader
        :agent="currentAgent"
        :agents="agentsStore.agentsList"
        :show-hamburger="isMobile"
        @switch-agent="handleSwitchAgent"
        @toggle-conversations="showMobileConversations = !showMobileConversations"
      />

      <!-- Hands-free voice strip (mic toggle + live transcript) -->
      <VoiceBar />

      <!-- Messages -->
      <ChatMessages
        :messages="chatStore.activeMessages"
        :agent="currentAgent"
        :isStreaming="isStreaming"
      />

      <!-- TTS Now Playing bar -->
      <div
        v-if="chatStore.ttsPlaying"
        class="flex items-center shrink-0"
        :style="{
          height: '36px',
          background: '#0d2818',
          borderTop: '1px solid #1a4428',
          padding: isMobile ? '0 12px' : '0 24px',
          gap: '10px',
        }"
      >
        <div class="flex items-end" style="gap: 2px; height: 14px;">
          <div class="tts-bar" style="animation-delay: 0s;"></div>
          <div class="tts-bar" style="animation-delay: 0.2s;"></div>
          <div class="tts-bar" style="animation-delay: 0.4s;"></div>
          <div class="tts-bar" style="animation-delay: 0.1s;"></div>
        </div>
        <span style="font-size: 11px; font-weight: 500; color: #10b981; flex: 1;">
          Playing response…
        </span>
        <button
          class="flex items-center cursor-pointer"
          :style="{
            padding: '4px 12px',
            background: 'rgba(239, 68, 68, 0.15)',
            border: '1px solid rgba(239, 68, 68, 0.3)',
            borderRadius: '6px',
            gap: '5px',
          }"
          @click="chatStore.stopTts()"
        >
          <svg width="10" height="10" viewBox="0 0 10 10" fill="none">
            <rect x="1" y="1" width="8" height="8" rx="1" fill="#ef4444"/>
          </svg>
          <span style="font-size: 11px; font-weight: 500; color: #ef4444;">Stop</span>
        </button>
      </div>

      <!-- Input -->
      <ChatInput
        :isStreaming="isStreaming"
        @send="handleSend"
      />
    </div>

    <!-- Hidden audio element for TTS -->
    <audio ref="ttsAudio" style="display: none;" />
  </div>
</template>

<style scoped>
.chat-root {
  margin: -24px -36px;
  height: calc(100% + 48px);
}
.chat-root--mobile {
  margin: -16px -12px;
  height: calc(100% + 32px);
}

/* Slide-out overlay */
.conv-overlay {
  position: fixed;
  inset: 0;
  z-index: 200;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
}
.conv-slide-panel {
  width: 300px;
  max-width: 80vw;
  height: 100%;
}

/* Slide animation */
.conv-overlay-enter-active {
  transition: opacity 0.2s ease;
}
.conv-overlay-enter-active .conv-slide-panel {
  transition: transform 0.25s cubic-bezier(0.4, 0, 0.2, 1);
}
.conv-overlay-leave-active {
  transition: opacity 0.2s ease;
}
.conv-overlay-leave-active .conv-slide-panel {
  transition: transform 0.2s cubic-bezier(0.4, 0, 0.2, 1);
}
.conv-overlay-enter-from {
  opacity: 0;
}
.conv-overlay-enter-from .conv-slide-panel {
  transform: translateX(-100%);
}
.conv-overlay-leave-to {
  opacity: 0;
}
.conv-overlay-leave-to .conv-slide-panel {
  transform: translateX(-100%);
}

.tts-bar {
  width: 3px;
  background: #10b981;
  border-radius: 1px;
  animation: tts-bounce 0.8s ease-in-out infinite alternate;
}
@keyframes tts-bounce {
  0% { height: 3px; }
  100% { height: 14px; }
}
</style>

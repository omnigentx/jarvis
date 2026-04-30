<script setup>
import { ref, computed } from 'vue'
import { useChatStore } from '../../stores/chat'

const props = defineProps({
  agent: { type: Object, default: null },
  agents: { type: Array, default: () => [] },
  showHamburger: { type: Boolean, default: false },
})

const emit = defineEmits(['switch-agent', 'toggle-conversations'])
const chatStore = useChatStore()
const showDropdown = ref(false)

const initials = computed(() => {
  if (!props.agent?.name) return '?'
  return props.agent.name
    .split(/[\s_-]+/)
    .map(w => w[0]?.toUpperCase() || '')
    .join('')
    .slice(0, 2)
})

const statusLine = computed(() => {
  if (!props.agent) return ''
  const parts = ['Online']
  if (props.agent.model) parts.push(props.agent.model)
  if (props.agent.servers?.length) parts.push(`${props.agent.servers.length} MCP servers`)
  return parts.join(' · ')
})

function selectAgent(name) {
  emit('switch-agent', name)
  showDropdown.value = false
}
</script>

<template>
  <div
    class="flex items-center shrink-0"
    :style="{
      height: showHamburger ? '56px' : '60px',
      background: 'var(--bg-sidebar)',
      borderBottom: '1px solid var(--border-sidebar)',
      padding: showHamburger ? '0 12px' : '0 24px',
      gap: showHamburger ? '10px' : '12px',
    }"
  >
    <!-- Hamburger (mobile) -->
    <button
      v-if="showHamburger"
      class="flex items-center justify-center shrink-0 cursor-pointer"
      :style="{
        width: '32px',
        height: '32px',
        borderRadius: '8px',
        background: 'var(--bg-card)',
        border: 'none',
      }"
      @click="emit('toggle-conversations')"
    >
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
        <line x1="2" y1="4" x2="14" y2="4" stroke="var(--text-sub)" stroke-width="1.5" stroke-linecap="round"/>
        <line x1="2" y1="8" x2="14" y2="8" stroke="var(--text-sub)" stroke-width="1.5" stroke-linecap="round"/>
        <line x1="2" y1="12" x2="14" y2="12" stroke="var(--text-sub)" stroke-width="1.5" stroke-linecap="round"/>
      </svg>
    </button>
    <!-- Agent avatar -->
    <div
      class="flex items-center justify-center shrink-0"
      :style="{
        width: '36px',
        height: '36px',
        borderRadius: '50%',
        background: '#1e3a5f',
        fontSize: '12px',
        fontWeight: '700',
        color: '#3b82f6',
      }"
    >
      {{ initials }}
    </div>

    <!-- Agent info -->
    <div class="flex-1 min-w-0">
      <div style="font-size: 14px; font-weight: 600; color: var(--text-primary); line-height: 17px;">
        {{ agent?.name || 'No Agent' }}
      </div>
      <div class="flex items-center" style="gap: 6px; margin-top: 2px;">
        <!-- Green dot -->
        <div
          v-if="agent"
          style="width: 6px; height: 6px; border-radius: 50%; background: #10b981; flex-shrink: 0;"
        ></div>
        <div style="font-size: 11px; font-weight: 400; color: var(--text-sub); line-height: 13px;">
          {{ statusLine }}
        </div>
      </div>
    </div>

    <!-- Switch Agent -->
    <div class="relative">
      <button
        class="flex items-center transition-colors cursor-pointer"
        :style="{
          padding: '6px 10px',
          background: 'transparent',
          border: 'none',
          gap: '4px',
        }"
        @click="showDropdown = !showDropdown"
      >
        <span style="font-size: 11px; font-weight: 500; color: var(--text-nav);">
          Switch Agent
        </span>
        <!-- Chevron -->
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
          <path d="M3 5L6 8L9 5" stroke="#8b8fa3" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </button>

      <!-- Dropdown -->
      <div
        v-if="showDropdown"
        class="absolute right-0 z-50"
        :style="{
          top: '100%',
          marginTop: '4px',
          width: '200px',
          background: 'var(--bg-sidebar)',
          border: '1px solid var(--border-sidebar)',
          borderRadius: '10px',
          padding: '4px',
          boxShadow: '0 8px 32px rgba(0,0,0,0.4)',
        }"
      >
        <div
          v-for="a in agents"
          :key="a.name"
          class="flex items-center cursor-pointer transition-colors"
          :style="{
            padding: '8px 12px',
            borderRadius: '8px',
            gap: '8px',
            background: a.name === agent?.name ? 'var(--bg-active)' : 'transparent',
          }"
          @mouseenter="$event.target.style.background = 'var(--bg-active)'"
          @mouseleave="$event.target.style.background = a.name === agent?.name ? 'var(--bg-active)' : 'transparent'"
          @click="selectAgent(a.name)"
        >
          <div
            class="flex items-center justify-center shrink-0"
            style="width: 24px; height: 24px; border-radius: 50%; background: #1e3a5f; font-size: 9px; font-weight: 700; color: #3b82f6;"
          >
            {{ a.name.split(/[\s_-]+/).map(w => w[0]?.toUpperCase()).join('').slice(0,2) }}
          </div>
          <span style="font-size: 12px; color: var(--text-secondary); font-weight: 500;">
            {{ a.name }}
          </span>
        </div>
      </div>
    </div>

    <!-- TTS toggle -->
    <button
      class="flex items-center justify-center cursor-pointer"
      :class="{ 'tts-playing-btn': chatStore.ttsPlaying }"
      :style="{
        width: '30px',
        height: '30px',
        borderRadius: '6px',
        background: chatStore.ttsPlaying ? '#0d2818' : chatStore.ttsEnabled ? '#0d2818' : 'transparent',
        border: chatStore.ttsEnabled || chatStore.ttsPlaying ? '1px solid #1a4428' : 'none',
        transition: 'all 0.2s ease',
      }"
      :title="chatStore.ttsPlaying ? 'Click to stop playback' : chatStore.ttsEnabled ? 'TTS On (click to disable)' : 'TTS Off (click to enable)'"
      @click="chatStore.ttsPlaying ? chatStore.stopTts() : chatStore.toggleTts()"
    >
      <!-- Playing state: stop icon -->
      <svg v-if="chatStore.ttsPlaying" width="15" height="15" viewBox="0 0 15 15" fill="none">
        <rect x="3" y="3" width="9" height="9" rx="1.5" fill="#ef4444"/>
      </svg>
      <!-- Enabled (idle) -->
      <svg v-else-if="chatStore.ttsEnabled" width="15" height="15" viewBox="0 0 15 15" fill="none">
        <path d="M2 5.5H4L7.5 2V13L4 9.5H2C1.5 9.5 1 9 1 8.5V6.5C1 6 1.5 5.5 2 5.5Z" fill="#10b981" stroke="#10b981" stroke-width="0.8"/>
        <path d="M10 4.5C11 5.5 11.5 6.5 11.5 7.5C11.5 8.5 11 9.5 10 10.5" stroke="#10b981" stroke-width="1.2" stroke-linecap="round"/>
        <path d="M11.5 3C13 4.5 14 6 14 7.5C14 9 13 10.5 11.5 12" stroke="#10b981" stroke-width="1.2" stroke-linecap="round"/>
      </svg>
      <!-- Disabled -->
      <svg v-else width="15" height="15" viewBox="0 0 15 15" fill="none">
        <path d="M2 5.5H4L7.5 2V13L4 9.5H2C1.5 9.5 1 9 1 8.5V6.5C1 6 1.5 5.5 2 5.5Z" stroke="#555872" stroke-width="1.2"/>
        <path d="M10 5.5L14 9.5M14 5.5L10 9.5" stroke="#555872" stroke-width="1.2" stroke-linecap="round"/>
      </svg>
    </button>
  </div>

  <!-- Click-outside handler -->
  <teleport to="body">
    <div
      v-if="showDropdown"
      class="fixed inset-0 z-40"
      @click="showDropdown = false"
    ></div>
  </teleport>
</template>


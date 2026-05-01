<script setup>
import { ref, computed, watch, nextTick } from 'vue'
import MarkdownRenderer from '../MarkdownRenderer.vue'

const props = defineProps({
  meeting: { type: Object, default: null },
  transcript: { type: Array, default: () => [] },
  meetingState: { type: Object, default: () => ({}) },
  isConnected: { type: Boolean, default: false },
  isConnecting: { type: Boolean, default: false },
})

const emit = defineEmits(['close'])

// Scroll handling
const scrollRef = ref(null)
const autoScroll = ref(true)
const expandAll = ref(false)

// Auto-scroll to bottom when new entries arrive
watch(
  () => props.transcript.length,
  async () => {
    if (autoScroll.value && props.transcript.length > 0) {
      await nextTick()
      if (scrollRef.value) {
        scrollRef.value.scrollTop = scrollRef.value.scrollHeight
      }
    }
  }
)

// Detect manual scroll
function handleScroll() {
  if (!scrollRef.value) return
  const el = scrollRef.value
  const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight
  autoScroll.value = distFromBottom < 80
}

function scrollToBottom() {
  autoScroll.value = true
  if (scrollRef.value) {
    scrollRef.value.scrollTop = scrollRef.value.scrollHeight
  }
}

// Agent color map
const AGENT_COLORS = [
  '#3b82f6', '#10b981', '#f59e0b', '#8b5cf6', '#ec4899',
  '#14b8a6', '#f97316', '#6366f1', '#06b6d4', '#84cc16',
]
const agentColorMap = computed(() => {
  const map = {}
  const participants = props.meetingState?.participants || props.meeting?.participants || []
  participants.forEach((name, i) => {
    map[name] = AGENT_COLORS[i % AGENT_COLORS.length]
  })
  return map
})

function agentColor(name) {
  return agentColorMap.value[name] || '#8b8fa3'
}

function agentInitial(name) {
  if (!name) return '?'
  return name.charAt(0).toUpperCase()
}

function formatTimestamp(ts) {
  if (!ts) return ''
  try {
    const d = new Date(ts)
    return d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })
  } catch {
    return ''
  }
}

// Expanded entry tracking
const expandedEntries = ref(new Set())

function toggleExpand(turn) {
  const s = new Set(expandedEntries.value)
  if (s.has(turn)) {
    s.delete(turn)
  } else {
    s.add(turn)
  }
  expandedEntries.value = s
}

function isLong(message) {
  return message && message.length > 300
}

// Connection status
const connectionLabel = computed(() => {
  if (props.meetingState?.ended) return 'Meeting Ended'
  if (props.isConnecting) return 'Connecting...'
  if (props.isConnected) return 'Live'
  return 'Disconnected'
})

const connectionColor = computed(() => {
  if (props.meetingState?.ended) return '#555872'
  if (props.isConnected) return '#10b981'
  if (props.isConnecting) return '#f59e0b'
  return '#ef4444'
})
</script>

<template>
  <div class="transcript-panel">
    <!-- Header -->
    <div class="transcript-header">
      <!-- Row 1: Back + Title -->
      <div class="transcript-header-row1">
        <button class="back-btn" @click="emit('close')" title="Back to list">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
            <path d="M19 12H5"/><polyline points="12 19 5 12 12 5"/>
          </svg>
        </button>
        <h3 class="transcript-title">{{ meeting?.agenda || 'Meeting' }}</h3>
        <span class="connection-badge" :style="{ background: connectionColor + '20', color: connectionColor, borderColor: connectionColor + '40' }">
          <span class="connection-dot" :style="{ background: connectionColor }"></span>
          {{ connectionLabel }}
        </span>
      </div>

      <!-- Row 2: Meta + Expand All -->
      <div class="transcript-header-row2">
        <span class="meta-item">R{{ meetingState.current_round || 1 }}</span>
        <span class="meta-sep">•</span>
        <span class="meta-item">{{ transcript.length }} msgs</span>
        <button
          class="expand-all-toggle"
          :class="{ active: expandAll }"
          @click="expandAll = !expandAll"
          title="Toggle expand all messages"
        >
          {{ expandAll ? '⊟' : '⊞' }}
        </button>
      </div>

      <!-- Row 3: Participants scroll -->
      <div class="transcript-participants-row">
        <div
          v-for="p in (meetingState.participants || meeting?.participants || [])"
          :key="p"
          class="transcript-participant"
          :class="{ speaking: !meetingState.ended && meetingState.participants?.[meetingState.current_turn] === p }"
          :style="{ '--agent-color': agentColor(p) }"
        >
          <div class="tp-avatar" :style="{ background: agentColor(p) + '25', color: agentColor(p) }">
            {{ agentInitial(p) }}
          </div>
          <span class="tp-name">{{ p }}</span>
          <span v-if="meetingState.joined?.includes(p)" class="tp-joined">✓</span>
        </div>
      </div>
    </div>

    <!-- Transcript body -->
    <div
      ref="scrollRef"
      class="transcript-scroll"
      @scroll="handleScroll"
    >
      <div v-if="!transcript.length && !meetingState.ended" class="transcript-empty">
        <div class="waiting-animation">
          <span class="dot"></span>
          <span class="dot"></span>
          <span class="dot"></span>
        </div>
        <p>Waiting for participants to join...</p>
      </div>

      <div v-else-if="!transcript.length && meetingState.ended" class="transcript-empty">
        <p>No transcript available</p>
      </div>

      <template v-else>
        <div
          v-for="(entry, index) in transcript"
          :key="entry.turn || index"
          class="transcript-entry"
          :class="{ 'entry-system': entry.type === 'system' }"
        >
          <!-- Agent avatar -->
          <div class="entry-avatar" :style="{ background: agentColor(entry.agent) + '20', color: agentColor(entry.agent) }">
            {{ agentInitial(entry.agent) }}
          </div>

          <div class="entry-body">
            <div class="entry-header">
              <span class="entry-agent" :style="{ color: agentColor(entry.agent) }">
                {{ entry.agent }}
              </span>
              <span v-if="entry.round" class="entry-round">
                R{{ entry.round }}
              </span>
              <span class="entry-time">{{ formatTimestamp(entry.timestamp) }}</span>
            </div>

            <div
              class="entry-message"
              :class="{ truncated: isLong(entry.message) && !expandAll && !expandedEntries.has(entry.turn) }"
              @click="!expandAll && isLong(entry.message) && toggleExpand(entry.turn)"
            >
              <MarkdownRenderer
                :content="expandAll || expandedEntries.has(entry.turn) || !isLong(entry.message)
                  ? (entry.message || '')
                  : (entry.message?.slice(0, 300) || '') + '…'"
                content-type="markdown"
                :enable-mermaid="expandAll || expandedEntries.has(entry.turn)"
              />
              <button
                v-if="isLong(entry.message) && !expandAll"
                class="expand-btn"
              >
                {{ expandedEntries.has(entry.turn) ? 'Show less' : 'Show more' }}
              </button>
            </div>
          </div>
        </div>
      </template>
    </div>

    <!-- Scroll-to-bottom FAB -->
    <button
      v-if="!autoScroll && transcript.length > 5"
      class="scroll-fab"
      @click="scrollToBottom"
      title="Scroll to latest"
    >
      ↓ New messages
    </button>

    <!-- Meeting outcome footer -->
    <div v-if="meetingState.ended" class="outcome-footer">
      <span class="outcome-icon">{{ meetingState.outcome === 'consensus' ? '🎯' : '🔔' }}</span>
      <span class="outcome-text">
        Meeting ended — {{ meetingState.outcome === 'consensus' ? 'Consensus reached' : meetingState.outcome || 'Completed' }}
      </span>
    </div>
  </div>
</template>

<style scoped>
.transcript-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  position: relative;
}

/* Header */
.transcript-header {
  padding: 10px 12px 8px;
  border-bottom: 1px solid #1a1d2e;
  flex-shrink: 0;
  background: #0c0e15;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

/* Row 1: Back button + title + connection badge */
.transcript-header-row1 {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.back-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  width: 32px;
  height: 32px;
  background: #111318;
  border: 1px solid #1e2030;
  color: #8b8fa3;
  border-radius: 8px;
  cursor: pointer;
  transition: all 0.15s;
  /* Increase tap area with pseudo content trick */
  position: relative;
}

.back-btn::before {
  content: '';
  position: absolute;
  inset: -6px;
}

.back-btn:hover {
  background: #1e2233;
  border-color: #2a3556;
  color: #f0f2f5;
}

.transcript-title {
  flex: 1;
  font-size: 13px;
  font-weight: 600;
  color: #f0f2f5;
  margin: 0;
  line-height: 1.35;
  /* Clamp to 2 lines max — keeps header compact */
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.connection-badge {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 10px;
  font-weight: 600;
  padding: 2px 7px;
  border-radius: 10px;
  border: 1px solid;
  white-space: nowrap;
}

.connection-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  flex-shrink: 0;
}

/* Row 2: meta info */
.transcript-header-row2 {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  color: #8b8fa3;
  padding-left: 40px; /* indent to align with title (past back-btn) */
}

.meta-item {
  color: #8b8fa3;
}

.meta-sep {
  color: #333;
}

.expand-all-toggle {
  margin-left: auto;
  background: #111318;
  border: 1px solid #1a1d2e;
  color: #8b8fa3;
  font-size: 14px;
  line-height: 1;
  width: 26px;
  height: 26px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.15s;
  flex-shrink: 0;
}

.expand-all-toggle:hover {
  background: #1e2233;
  color: #c4c8d4;
  border-color: #2a3556;
}

.expand-all-toggle.active {
  background: #1e3a5f;
  color: #3b82f6;
  border-color: #2a3556;
}

/* Row 3: Participants — always horizontal scroll */
.transcript-participants-row {
  display: flex;
  gap: 6px;
  flex-wrap: nowrap;
  overflow-x: auto;
  scrollbar-width: none;
  padding-bottom: 1px;
}

.transcript-participants-row::-webkit-scrollbar { display: none; }

.transcript-participant {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 4px 8px;
  background: #111318;
  border-radius: 16px;
  border: 1px solid #1a1d2e;
  transition: all 0.2s ease;
}

.transcript-participant.speaking {
  border-color: var(--agent-color, #3b82f6);
  background: color-mix(in srgb, var(--agent-color) 10%, #111318);
  box-shadow: 0 0 8px color-mix(in srgb, var(--agent-color) 20%, transparent);
}

.tp-avatar {
  width: 20px;
  height: 20px;
  border-radius: 50%;
  font-size: 10px;
  font-weight: 600;
  display: flex;
  align-items: center;
  justify-content: center;
}

.tp-name {
  font-size: 11px;
  color: #c4c8d4;
  font-weight: 500;
}

.tp-joined {
  font-size: 10px;
  color: #10b981;
}

/* Transcript scroll area */
.transcript-scroll {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
}

.transcript-scroll::-webkit-scrollbar {
  width: 5px;
}
.transcript-scroll::-webkit-scrollbar-track {
  background: transparent;
}
.transcript-scroll::-webkit-scrollbar-thumb {
  background: #1a1d2e;
  border-radius: 4px;
}

.transcript-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 200px;
  color: #555872;
  gap: 12px;
}

.waiting-animation {
  display: flex;
  gap: 6px;
}

.waiting-animation .dot {
  width: 8px;
  height: 8px;
  background: #3b82f6;
  border-radius: 50%;
  animation: bounce 1.4s ease-in-out infinite;
}

.waiting-animation .dot:nth-child(2) { animation-delay: 0.2s; }
.waiting-animation .dot:nth-child(3) { animation-delay: 0.4s; }

@keyframes bounce {
  0%, 80%, 100% { transform: scale(0.6); opacity: 0.4; }
  40% { transform: scale(1); opacity: 1; }
}

/* Transcript entry */
.transcript-entry {
  display: flex;
  gap: 10px;
  padding: 12px 0;
  border-bottom: 1px solid #111318;
  animation: fadeIn 0.2s ease;
}

.transcript-entry:last-child {
  border-bottom: none;
}

@keyframes fadeIn {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: translateY(0); }
}

.entry-avatar {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  font-size: 13px;
  font-weight: 600;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.entry-body {
  flex: 1;
  min-width: 0;
}

.entry-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}

.entry-agent {
  font-size: 13px;
  font-weight: 600;
}

.entry-round {
  font-size: 10px;
  color: #555872;
  padding: 1px 5px;
  background: #111318;
  border-radius: 4px;
}

.entry-time {
  font-size: 11px;
  color: #555872;
  margin-left: auto;
}

.entry-message {
  font-size: 13px;
  color: #c4c8d4;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}

.entry-message.truncated {
  cursor: pointer;
}

.expand-btn {
  background: none;
  border: none;
  color: #3b82f6;
  font-size: 12px;
  cursor: pointer;
  padding: 0;
  margin-left: 4px;
}

.expand-btn:hover {
  text-decoration: underline;
}

.entry-system {
  opacity: 0.6;
}

.entry-system .entry-message {
  font-style: italic;
  color: #8b8fa3;
}

/* Scroll FAB */
.scroll-fab {
  position: absolute;
  bottom: 80px;
  right: 24px;
  padding: 6px 14px;
  background: #1e3a5f;
  color: #3b82f6;
  border: 1px solid #2a3556;
  border-radius: 20px;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.2s;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
  z-index: 10;
}

.scroll-fab:hover {
  background: #2a3556;
  transform: translateY(-2px);
}

/* Outcome footer */
.outcome-footer {
  padding: 12px 16px;
  border-top: 1px solid #1a1d2e;
  background: #0c0e15;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.outcome-icon { font-size: 16px; }

.outcome-text {
  font-size: 13px;
  color: #8b8fa3;
  font-weight: 500;
}

/* ─── Mobile overrides ─── */
@media (max-width: 767px) {
  /* Participant pills: smaller tap, flush */
  .transcript-participant {
    flex-shrink: 0;
    padding: 3px 7px;
  }

  .tp-name {
    font-size: 10px;
    white-space: nowrap;
  }

  /* Transcript entries: tighter padding */
  .transcript-scroll {
    padding: 10px 12px;
  }

  .entry-avatar {
    width: 28px;
    height: 28px;
    font-size: 11px;
  }

  .entry-agent { font-size: 12px; }
  .entry-message { font-size: 12px; }

  /* FAB: above outcome footer */
  .scroll-fab {
    bottom: 70px;
    right: 14px;
  }
}
</style>

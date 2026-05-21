<script setup>
import { ref, computed, watch, nextTick, onMounted, onUnmounted } from 'vue'
import MarkdownRenderer from '../MarkdownRenderer.vue'
import { normalizeTs } from '../../utils/timeFormat.js'

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

// Details drawer (long-form description from create_meeting's `description` arg)
const showDetails = ref(false)

// "Now" tick — drives the live "X ago" reading on the active-turn indicator
// without forcing a parent rerender. 5s cadence is enough for human-scale UX.
const nowSec = ref(Math.floor(Date.now() / 1000))
let nowTimer = null
onMounted(() => {
  nowTimer = setInterval(() => {
    nowSec.value = Math.floor(Date.now() / 1000)
  }, 5000)
})
onUnmounted(() => {
  if (nowTimer) clearInterval(nowTimer)
})

// Current speaker (the participant whose turn it currently is). Derived
// from meetingState; empty string if meeting is ended or state not loaded.
const currentSpeaker = computed(() => {
  if (props.meetingState?.ended) return ''
  const parts = props.meetingState?.participants || props.meeting?.participants || []
  const idx = props.meetingState?.current_turn ?? 0
  return parts[idx] || ''
})

// Time-since-last-action for the current turn — surfaces when a meeting
// is hung waiting on someone to speak (b61af7db incident: 24h hang).
// Falls back to the most recent transcript entry's timestamp.
const lastActionAgo = computed(() => {
  if (props.meetingState?.ended) return ''
  // Prefer turn_started_at from state (set by speak/skip_turn), fall back
  // to the latest transcript entry timestamp.
  let ts = props.meeting?.turn_started_at || props.meetingState?.turn_started_at
  if (!ts && props.transcript.length) {
    const last = props.transcript[props.transcript.length - 1]
    const t = last?.timestamp
    if (t) {
      const parsed = typeof t === 'number' ? t : Date.parse(t) / 1000
      if (!Number.isNaN(parsed)) ts = parsed
    }
  }
  if (!ts) return ''
  const sec = Math.max(0, nowSec.value - Math.floor(Number(ts)))
  if (sec < 5) return 'just now'
  if (sec < 60) return `${sec}s ago`
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${sec % 60}s ago`
  return `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m ago`
})

// Bottleneck warning — flag turns that have been waiting longer than 60s.
const isStalled = computed(() => {
  if (props.meetingState?.ended || !currentSpeaker.value) return false
  let ts = props.meeting?.turn_started_at || props.meetingState?.turn_started_at
  if (!ts && props.transcript.length) {
    const last = props.transcript[props.transcript.length - 1]
    const t = last?.timestamp
    if (t) {
      const parsed = typeof t === 'number' ? t : Date.parse(t) / 1000
      if (!Number.isNaN(parsed)) ts = parsed
    }
  }
  if (!ts) return false
  return (nowSec.value - Math.floor(Number(ts))) > 60
})

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
  const ms = normalizeTs(ts)
  if (ms === null) return ''
  try {
    const d = new Date(ms)
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

// Threshold above which a single transcript entry gets a max-height + "Show
// more" toggle. Bigger than the old 300-char hard cut — lets short-medium
// agent messages render fully (no more "..." everywhere) while still
// protecting the scroll viewport from a single 5KB wall of text.
const LONG_MESSAGE_CHARS = 1200

function isLong(message) {
  return message && message.length > LONG_MESSAGE_CHARS
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
    <!-- ─── Header ─── -->
    <div class="transcript-header">
      <!-- Row 1: back + title + connection + actions -->
      <div class="transcript-header-row1">
        <button class="back-btn" @click="emit('close')" title="Back to list">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
            <path d="M19 12H5"/><polyline points="12 19 5 12 12 5"/>
          </svg>
        </button>
        <h3 class="transcript-title" :title="meeting?.agenda">{{ meeting?.agenda || 'Meeting' }}</h3>
        <span class="connection-badge" :style="{ background: connectionColor + '20', color: connectionColor, borderColor: connectionColor + '40' }">
          <span class="connection-dot" :style="{ background: connectionColor }"></span>
          {{ connectionLabel }}
        </span>
        <button
          v-if="meeting?.description || meeting?.agenda"
          class="header-icon-btn"
          :class="{ active: showDetails }"
          @click="showDetails = !showDetails"
          :title="showDetails ? 'Hide details' : 'Show full details (description)'"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="9"/><path d="M12 8v4"/><circle cx="12" cy="16" r="0.5" fill="currentColor"/>
          </svg>
        </button>
        <button
          class="header-icon-btn"
          :class="{ active: expandAll }"
          @click="expandAll = !expandAll"
          :title="expandAll ? 'Collapse all messages' : 'Expand all messages'"
        >
          {{ expandAll ? '⊟' : '⊞' }}
        </button>
      </div>

      <!-- Row 2: live meta + bottleneck indicator + participants -->
      <div class="transcript-header-row2">
        <span class="meta-pill">R{{ meetingState.current_round || meeting?.current_round || 1 }}<span v-if="meeting?.max_rounds">/{{ meeting.max_rounds }}</span></span>
        <span class="meta-pill">{{ transcript.length }} msgs</span>
        <span v-if="currentSpeaker" class="meta-pill speaking-pill" :class="{ 'pill-stalled': isStalled }">
          🎙️ {{ currentSpeaker }}
          <span v-if="lastActionAgo" class="pill-time">· {{ lastActionAgo }}</span>
        </span>
        <span v-if="meetingState.ended" class="meta-pill ended-pill">
          ✅ ended<span v-if="meetingState.outcome"> · {{ meetingState.outcome }}</span>
        </span>

        <div class="participant-strip" aria-label="Participants">
          <div
            v-for="p in (meetingState.participants || meeting?.participants || [])"
            :key="p"
            class="participant-pill"
            :class="{ speaking: !meetingState.ended && currentSpeaker === p }"
            :style="{ '--agent-color': agentColor(p) }"
            :title="p"
          >
            <span class="pp-avatar" :style="{ background: agentColor(p) + '25', color: agentColor(p) }">
              {{ agentInitial(p) }}
            </span>
            <span class="pp-name">{{ p }}</span>
          </div>
        </div>
      </div>

      <!-- Optional: details drawer (description + agenda full) -->
      <div v-if="showDetails" class="details-drawer">
        <div class="details-header">
          <span class="details-icon">📋</span>
          <span class="details-title">Meeting details</span>
          <button class="details-close" @click="showDetails = false" title="Close">×</button>
        </div>
        <div class="details-body">
          <div class="details-block">
            <div class="details-label">Agenda</div>
            <div class="details-value">{{ meeting?.agenda || '(no agenda)' }}</div>
          </div>
          <div v-if="meeting?.description" class="details-block">
            <div class="details-label">Description</div>
            <div class="details-value details-markdown">
              <MarkdownRenderer :content="meeting.description" content-type="markdown" />
            </div>
          </div>
          <div v-else class="details-block">
            <div class="details-label">Description</div>
            <div class="details-value details-empty">
              No description was provided. Tip: pass <code>description="…"</code>
              to <code>create_meeting</code> for long-form context (links, scope,
              deliverables) — keeps agenda short for the dashboard title.
            </div>
          </div>
          <div v-if="meeting?.created_by" class="details-block">
            <div class="details-label">Created by</div>
            <div class="details-value">{{ meeting.created_by }}</div>
          </div>
          <div v-if="meeting?.meeting_id" class="details-block">
            <div class="details-label">Meeting ID</div>
            <div class="details-value details-mono">{{ meeting.meeting_id }}</div>
          </div>
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
              :class="{
                'is-long-collapsed':
                  isLong(entry.message) && !expandAll && !expandedEntries.has(entry.turn),
              }"
            >
              <MarkdownRenderer
                :content="entry.message || ''"
                content-type="markdown"
                :enable-mermaid="expandAll || expandedEntries.has(entry.turn)"
              />
              <button
                v-if="isLong(entry.message) && !expandAll"
                class="expand-btn"
                @click="toggleExpand(entry.turn)"
              >
                {{ expandedEntries.has(entry.turn) ? '↑ Show less' : '↓ Show more' }}
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

/* Header — single compact stack: row1 (title + actions) + row2 (live meta) */
.transcript-header {
  padding: 10px 12px 8px;
  border-bottom: 1px solid #1a1d2e;
  flex-shrink: 0;
  background: #0c0e15;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

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
  font-size: 14px;
  font-weight: 600;
  color: #f0f2f5;
  margin: 0;
  line-height: 1.4;
  /* Single-line ellipsis — agenda is now ≤120 chars (Bug 5 enforcement),
     full markdown details available via the drawer. */
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
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

.header-icon-btn {
  flex-shrink: 0;
  background: #111318;
  border: 1px solid #1a1d2e;
  color: #8b8fa3;
  font-size: 13px;
  line-height: 1;
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.15s;
}

.header-icon-btn:hover {
  background: #1e2233;
  color: #c4c8d4;
  border-color: #2a3556;
}

.header-icon-btn.active {
  background: #1e3a5f;
  color: #3b82f6;
  border-color: #2a3556;
}

/* Row 2: live meta — round, msg count, current speaker, participants */
.transcript-header-row2 {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: #8b8fa3;
  padding-left: 40px;
  flex-wrap: wrap;
}

.meta-pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  background: #111318;
  border: 1px solid #1a1d2e;
  border-radius: 12px;
  color: #8b8fa3;
  font-size: 11px;
  font-weight: 500;
  white-space: nowrap;
}

.speaking-pill {
  background: rgba(0, 212, 170, 0.10);
  border-color: rgba(0, 212, 170, 0.35);
  color: #00d4aa;
}

.speaking-pill.pill-stalled {
  background: rgba(245, 158, 11, 0.12);
  border-color: rgba(245, 158, 11, 0.45);
  color: #f59e0b;
  /* Soft pulse — draws attention to a hung turn without being noisy. */
  animation: stalled-pulse 1.6s ease-in-out infinite;
}

@keyframes stalled-pulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(245, 158, 11, 0); }
  50%      { box-shadow: 0 0 0 4px rgba(245, 158, 11, 0.18); }
}

.pill-time {
  opacity: 0.75;
  font-weight: 400;
}

.ended-pill {
  background: rgba(85, 88, 114, 0.20);
  border-color: rgba(85, 88, 114, 0.40);
  color: #8b8fa3;
}

.participant-strip {
  display: flex;
  gap: 4px;
  flex-wrap: nowrap;
  overflow-x: auto;
  scrollbar-width: none;
  margin-left: auto;
  max-width: 60%;
}

.participant-strip::-webkit-scrollbar { display: none; }

.participant-pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 7px 2px 2px;
  background: #111318;
  border-radius: 14px;
  border: 1px solid #1a1d2e;
  flex-shrink: 0;
  transition: all 0.2s ease;
}

.participant-pill.speaking {
  border-color: var(--agent-color, #3b82f6);
  background: color-mix(in srgb, var(--agent-color) 10%, #111318);
  box-shadow: 0 0 6px color-mix(in srgb, var(--agent-color) 20%, transparent);
}

.pp-avatar {
  width: 18px;
  height: 18px;
  border-radius: 50%;
  font-size: 9px;
  font-weight: 600;
  display: flex;
  align-items: center;
  justify-content: center;
}

.pp-name {
  font-size: 10px;
  color: #c4c8d4;
  font-weight: 500;
  max-width: 100px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* Details drawer — collapsible, holds the full agenda + description */
.details-drawer {
  margin-top: 4px;
  background: #111318;
  border: 1px solid #1e2233;
  border-radius: 8px;
  overflow: hidden;
  animation: drawer-slide 0.2s ease;
}

@keyframes drawer-slide {
  from { opacity: 0; transform: translateY(-4px); }
  to   { opacity: 1; transform: translateY(0); }
}

.details-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background: #0c0e15;
  border-bottom: 1px solid #1a1d2e;
  font-size: 12px;
  font-weight: 600;
  color: #c4c8d4;
}

.details-icon { font-size: 13px; }
.details-title { flex: 1; }

.details-close {
  background: transparent;
  border: none;
  color: #8b8fa3;
  font-size: 18px;
  line-height: 1;
  width: 22px;
  height: 22px;
  cursor: pointer;
  border-radius: 4px;
}

.details-close:hover {
  background: #1e2233;
  color: #f0f2f5;
}

.details-body {
  padding: 10px 14px 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-height: 320px;
  overflow-y: auto;
}

.details-block {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.details-label {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  font-weight: 600;
  color: #555872;
}

.details-value {
  font-size: 13px;
  color: #c4c8d4;
  line-height: 1.5;
  word-break: break-word;
}

.details-markdown :deep(p) { margin: 0 0 0.5em; }
.details-markdown :deep(h1),
.details-markdown :deep(h2),
.details-markdown :deep(h3) {
  font-size: 13px;
  margin: 0.6em 0 0.3em;
  color: #f0f2f5;
}
.details-markdown :deep(code) {
  font-size: 12px;
  padding: 1px 4px;
  background: #0a0d14;
  border-radius: 3px;
}

.details-empty {
  font-size: 12px;
  color: #8b8fa3;
  font-style: italic;
}

.details-empty code {
  font-style: normal;
  background: #0a0d14;
  padding: 1px 5px;
  border-radius: 3px;
  color: #c4c8d4;
  font-size: 11px;
}

.details-mono {
  font-family: 'SF Mono', Menlo, monospace;
  font-size: 12px;
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
  position: relative;
}

/* Long-message collapse: cap the height + soft fade-out at the bottom +
   "Show more" button. Replaces the old hard 300-char truncate with "…".
   User can still scroll inside the message OR click expand. */
.entry-message.is-long-collapsed {
  max-height: 320px;
  overflow: hidden;
}

.entry-message.is-long-collapsed::after {
  content: '';
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  height: 60px;
  pointer-events: none;
  background: linear-gradient(to bottom, rgba(10,13,20,0), rgba(10,13,20,0.95));
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
  /* Header row 2: drop indent, allow wrapping into 2 lines */
  .transcript-header-row2 {
    padding-left: 0;
    gap: 4px;
  }

  /* Participant strip: cap width so other meta pills get room */
  .participant-strip {
    max-width: 100%;
    margin-left: 0;
    width: 100%;
  }

  .pp-name {
    max-width: 70px;
  }

  /* Title gets full width on its row */
  .transcript-title {
    font-size: 13px;
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

  /* Long-collapse threshold a bit shorter on mobile (less vertical room) */
  .entry-message.is-long-collapsed {
    max-height: 260px;
  }

  /* Drawer body shorter so it doesn't eat the transcript */
  .details-body {
    max-height: 220px;
  }

  /* FAB: above outcome footer */
  .scroll-fab {
    bottom: 70px;
    right: 14px;
  }
}

/* ─── Desktop refinements (≥1280px) ─── */
@media (min-width: 1280px) {
  .transcript-scroll {
    padding: 18px 24px;
  }

  .entry-avatar {
    width: 36px;
    height: 36px;
    font-size: 14px;
  }

  .entry-agent { font-size: 14px; }
  .entry-message { font-size: 14px; line-height: 1.65; }
  .transcript-title { font-size: 15px; }
}
</style>

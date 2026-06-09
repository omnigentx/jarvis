<script setup>
/**
 * MeetingTranscriptPanel — right side of MeetingsView.
 *
 * Sections:
 *   1. Header: agenda + live/ended badge + round counter
 *   2. Agenda strip + participant avatars row (active speaker = colored ring)
 *   3. Stall warning if active speaker idle >60s (b61af7db incident pattern)
 *   4. Transcript body: avatar + role tag + role-band + markdown body
 *       - PM [DECISION] VERDICT highlights with primary-bg-strong band
 *   5. Typing indicator for current speaker (3 dots in role color)
 *   6. Footer: SSE endpoint reference (mono)
 *
 * Reactive data comes from useMeetingStream via parent props. Composable
 * unmodified.
 */
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

// ── Role color resolution ────────────────────────────────────────────────
// Participants are e.g. "Elliot [PM]" / "Tuan [Dev]" / plain "AudioReaderAgent".
// Pull the bracket-tagged role first (maps to design tokens --role-pm etc).
// Fall back to a stable palette indexed by participant order, so unknown
// roles still get a consistent color across the transcript.
const PALETTE = [
  '#3b82f6', '#10b981', '#f59e0b', '#8b5cf6', '#ec4899',
  '#14b8a6', '#f97316', '#6366f1', '#06b6d4', '#84cc16',
]
const ROLE_TOKEN = {
  PM:  'var(--role-pm)',
  SA:  'var(--role-sa)',
  DEV: 'var(--role-dev)',
  QE:  'var(--role-qe)',
  DES: 'var(--role-des)',
  BA:  'var(--role-ba)',
  DSO: 'var(--role-dso)',
}

function extractRole(name) {
  if (!name) return ''
  const m = /\[([A-Za-z]+)\]/.exec(name)
  return m ? m[1].toUpperCase() : ''
}

function colorFor(name, participants) {
  const role = extractRole(name)
  if (role && ROLE_TOKEN[role]) return ROLE_TOKEN[role]
  const idx = (participants || []).indexOf(name)
  if (idx >= 0) return PALETTE[idx % PALETTE.length]
  return '#7B8094'
}

function initialFor(name) { return (name || '?').charAt(0).toUpperCase() }
function tagFor(name) {
  const role = extractRole(name)
  if (role) return role
  // Stripped, uppercased, first word — e.g. "AudioReaderAgent" → "AUDIO"
  const base = (name || '').replace(/\[[^\]]+\]/, '').trim().split(/\s+/)[0] || ''
  return base.slice(0, 6).toUpperCase()
}

const participants = computed(() =>
  props.meetingState?.participants?.length
    ? props.meetingState.participants
    : (props.meeting?.participants || []),
)

const currentSpeaker = computed(() => {
  if (props.meetingState?.ended) return ''
  const idx = props.meetingState?.current_turn ?? 0
  return participants.value[idx] || ''
})

// ── Stall detection (>60s idle on current turn) ─────────────────────────
const nowSec = ref(Math.floor(Date.now() / 1000))
let nowTimer = null
onMounted(() => { nowTimer = setInterval(() => { nowSec.value = Math.floor(Date.now() / 1000) }, 5000) })
onUnmounted(() => { if (nowTimer) clearInterval(nowTimer) })

function _turnStartedAtSec() {
  let ts = props.meeting?.turn_started_at || props.meetingState?.turn_started_at
  if (!ts && props.transcript.length) {
    const last = props.transcript[props.transcript.length - 1]
    const t = last?.timestamp
    if (t) {
      const parsed = typeof t === 'number' ? t : Date.parse(t) / 1000
      if (!Number.isNaN(parsed)) ts = parsed
    }
  }
  return ts ? Math.floor(Number(ts)) : null
}

const turnAgeSec = computed(() => {
  const t = _turnStartedAtSec()
  return t === null ? 0 : Math.max(0, nowSec.value - t)
})
const isStalled = computed(() =>
  !props.meetingState?.ended && currentSpeaker.value && turnAgeSec.value > 60,
)
const stallAgeText = computed(() => {
  const s = turnAgeSec.value
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m`
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`
})

// ── Connection / status ─────────────────────────────────────────────────
const connection = computed(() => {
  if (props.meetingState?.ended) return { label: 'MEETING ENDED', color: 'var(--text-muted)', live: false }
  if (props.isConnected) return { label: 'LIVE', color: 'var(--success)', live: true }
  if (props.isConnecting) return { label: 'CONNECTING…', color: 'var(--warning)', live: false }
  return { label: 'DISCONNECTED', color: 'var(--danger)', live: false }
})

// ── Auto-scroll ─────────────────────────────────────────────────────────
const scrollRef = ref(null)
const autoScroll = ref(true)

watch(
  () => props.transcript.length,
  async () => {
    if (autoScroll.value && props.transcript.length > 0) {
      await nextTick()
      if (scrollRef.value) scrollRef.value.scrollTop = scrollRef.value.scrollHeight
    }
  },
)

function handleScroll() {
  const el = scrollRef.value
  if (!el) return
  const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight
  autoScroll.value = distFromBottom < 80
}

// ── Details drawer ──────────────────────────────────────────────────────
const showDetails = ref(false)

// ── Long-message collapse ───────────────────────────────────────────────
const LONG = 1200
const expanded = ref(new Set())
const expandAll = ref(false)
function isLong(msg) { return (msg || '').length > LONG }
function toggleExpand(turn) {
  const s = new Set(expanded.value)
  s.has(turn) ? s.delete(turn) : s.add(turn)
  expanded.value = s
}
function showFull(turn) { return expandAll.value || expanded.value.has(turn) }

// ── Verdict detection ───────────────────────────────────────────────────
// PM messages tagged with "[DECISION] VERDICT" get highlighted style.
function isVerdict(entry) {
  return typeof entry?.message === 'string'
    && /\[DECISION\]\s*VERDICT/i.test(entry.message)
}

function formatTimestamp(ts) {
  const ms = normalizeTs(ts)
  if (ms === null) return ''
  try {
    return new Date(ms).toLocaleTimeString('en-US', {
      hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit',
    })
  } catch { return '' }
}
</script>

<template>
  <div class="tp">
    <!-- Header row 1 -->
    <div class="tp-head">
      <button class="tp-back" @click="emit('close')" title="Back to list">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
          <path d="M19 12H5"/><polyline points="12 19 5 12 12 5"/>
        </svg>
      </button>
      <h2 class="tp-title" :title="meeting?.agenda">{{ meeting?.agenda || 'Meeting' }}</h2>
      <span
        class="tp-badge"
        :style="{
          color: connection.color,
          borderColor: connection.color + '40',
          background: connection.color + '15',
        }"
      >
        <span class="tp-badge-dot" :style="{ background: connection.color }" :class="{ pulse: connection.live }" />
        {{ connection.label }}
      </span>
      <button
        v-if="meeting?.description || meeting?.agenda"
        class="tp-iconbtn"
        :class="{ active: showDetails }"
        @click="showDetails = !showDetails"
        :title="showDetails ? 'Hide details' : 'Show full description'"
      >
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="12" r="9"/><path d="M12 8v4"/><circle cx="12" cy="16" r="0.5" fill="currentColor"/>
        </svg>
      </button>
      <button class="tp-iconbtn" :class="{ active: expandAll }" @click="expandAll = !expandAll" :title="expandAll ? 'Collapse long messages' : 'Expand long messages'">
        {{ expandAll ? '⊟' : '⊞' }}
      </button>
    </div>

    <!-- Header row 2: round counter + agenda strip pills + participants -->
    <div class="tp-meta">
      <span class="tp-pill">
        round {{ meetingState.current_round || meeting?.current_round || 1 }}<span v-if="meeting?.max_rounds"> / {{ meeting.max_rounds }}</span>
      </span>
      <span class="tp-pill">{{ transcript.length }} msgs</span>
      <span v-if="currentSpeaker && !meetingState.ended" class="tp-pill speak-pill" :class="{ stalled: isStalled }">
        🎙 {{ currentSpeaker }}
        <span class="tp-pill-age">· {{ stallAgeText }}</span>
        <span v-if="isStalled" class="tp-pill-warn">⚠ stalled</span>
      </span>
      <span v-if="meetingState.ended && meetingState.outcome" class="tp-pill ended-pill">
        ✓ {{ meetingState.outcome }}
      </span>

      <!-- Participant strip -->
      <div class="tp-participants">
        <span
          v-for="p in participants"
          :key="p"
          class="tp-part"
          :class="{ speaking: !meetingState.ended && currentSpeaker === p }"
          :style="{ '--c': colorFor(p, participants) }"
          :title="p"
        >
          <span class="tp-part-avatar">{{ initialFor(p) }}</span>
          <span class="tp-part-name">{{ p }}</span>
        </span>
      </div>
    </div>

    <!-- Stall warning banner (separate from pill — full-width attention grab) -->
    <div v-if="isStalled" class="tp-stall-banner">
      <span class="tp-stall-icon">⚠</span>
      <span>Stalled · {{ currentSpeaker }} hasn't spoken in {{ stallAgeText }}.</span>
      <span class="tp-stall-ref">(b61af7db pattern)</span>
    </div>

    <!-- Details drawer -->
    <div v-if="showDetails" class="tp-drawer">
      <div class="tp-drawer-head">
        <span class="mono-label">MEETING DETAILS</span>
        <button class="tp-drawer-close" @click="showDetails = false">×</button>
      </div>
      <div class="tp-drawer-body">
        <div class="tp-drawer-block">
          <div class="tp-drawer-label">Agenda</div>
          <div class="tp-drawer-val">{{ meeting?.agenda || '(no agenda)' }}</div>
        </div>
        <div v-if="meeting?.description" class="tp-drawer-block">
          <div class="tp-drawer-label">Description</div>
          <div class="tp-drawer-val">
            <MarkdownRenderer :content="meeting.description" content-type="markdown" />
          </div>
        </div>
        <div v-if="meeting?.created_by" class="tp-drawer-block">
          <div class="tp-drawer-label">Created by</div>
          <div class="tp-drawer-val">{{ meeting.created_by }}</div>
        </div>
        <div v-if="meeting?.meeting_id" class="tp-drawer-block">
          <div class="tp-drawer-label">Meeting ID</div>
          <code class="tp-drawer-mono">{{ meeting.meeting_id }}</code>
        </div>
      </div>
    </div>

    <!-- Transcript body -->
    <div ref="scrollRef" class="tp-body" @scroll="handleScroll">
      <div v-if="!transcript.length && !meetingState.ended" class="tp-empty">
        <div class="tp-empty-dots">
          <span /><span /><span />
        </div>
        <p>Waiting for participants to join…</p>
      </div>

      <div v-else-if="!transcript.length && meetingState.ended" class="tp-empty">
        <p>No transcript available</p>
      </div>

      <template v-else>
        <article
          v-for="(entry, idx) in transcript"
          :key="entry.turn || idx"
          class="entry"
          :class="{ 'entry-verdict': isVerdict(entry) }"
          :style="{ '--role-color': colorFor(entry.agent, participants) }"
        >
          <div class="entry-band" />
          <div class="entry-avatar">{{ initialFor(entry.agent) }}</div>
          <div class="entry-body">
            <header class="entry-header">
              <span class="entry-agent">{{ entry.agent }}</span>
              <span class="entry-tag">{{ tagFor(entry.agent) }}</span>
              <span v-if="entry.round" class="entry-round">R{{ entry.round }}</span>
              <span class="entry-time">{{ formatTimestamp(entry.timestamp) }}</span>
              <span v-if="isVerdict(entry)" class="entry-verdict-chip">[DECISION] VERDICT</span>
            </header>
            <div
              class="entry-msg"
              :class="{ 'entry-msg-collapsed': isLong(entry.message) && !showFull(entry.turn) }"
            >
              <MarkdownRenderer
                :content="entry.message || ''"
                content-type="markdown"
                :enable-mermaid="showFull(entry.turn)"
              />
            </div>
            <button
              v-if="isLong(entry.message) && !expandAll"
              class="entry-more"
              @click="toggleExpand(entry.turn)"
            >
              {{ expanded.has(entry.turn) ? '↑ Show less' : '↓ Show more' }}
            </button>
          </div>
        </article>

        <!-- Typing indicator for current speaker -->
        <div
          v-if="currentSpeaker && !meetingState.ended"
          class="entry-typing"
          :style="{ '--role-color': colorFor(currentSpeaker, participants) }"
        >
          <div class="entry-avatar">{{ initialFor(currentSpeaker) }}</div>
          <div class="entry-body">
            <span class="entry-agent">{{ currentSpeaker }}</span>
            <span class="entry-tag">{{ tagFor(currentSpeaker) }}</span>
            <span class="typing-dots">
              <span /><span /><span />
            </span>
          </div>
        </div>
      </template>
    </div>

    <!-- Footer -->
    <div class="tp-footer">
      <span class="tp-foot-dot" :class="{ live: connection.live }" />
      <span class="tp-foot-label">{{ connection.live ? 'SSE connected' : connection.label.toLowerCase() }}</span>
      <code class="tp-foot-url">/api/agent/meetings/{{ meeting?.meeting_id }}/stream</code>
      <span class="tp-foot-tail">
        {{ meetingState.ended ? 'meeting ended · transcript frozen' : 'auto-scroll · meeting drives itself' }}
      </span>
    </div>
  </div>
</template>

<style scoped>
.tp {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  background: var(--bg-0);
}

/* Header row 1 */
.tp-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px 6px;
  border-bottom: 1px solid var(--border);
  background: var(--bg-1);
  flex-shrink: 0;
}
.tp-back {
  width: 30px; height: 30px;
  display: flex; align-items: center; justify-content: center;
  background: var(--bg-2);
  border: 1px solid var(--border);
  border-radius: var(--r-sm);
  color: var(--text-muted);
  cursor: pointer;
  flex-shrink: 0;
}
.tp-back:hover { background: var(--bg-3); color: var(--text); }
.tp-title {
  flex: 1;
  font-size: 14px;
  font-weight: 600;
  margin: 0;
  letter-spacing: -0.01em;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.tp-badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 9px;
  border-radius: var(--r-full);
  border: 1px solid;
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.08em;
  white-space: nowrap;
  flex-shrink: 0;
}
.tp-badge-dot {
  width: 5px; height: 5px;
  border-radius: 50%;
  flex-shrink: 0;
}
.tp-badge-dot.pulse { animation: tp-dot-pulse 1.4s ease-in-out infinite; }
@keyframes tp-dot-pulse { 50% { opacity: 0.4; } }

.tp-iconbtn {
  width: 26px; height: 26px;
  display: flex; align-items: center; justify-content: center;
  background: var(--bg-2);
  border: 1px solid var(--border);
  border-radius: var(--r-sm);
  color: var(--text-muted);
  cursor: pointer;
  font-size: 13px;
}
.tp-iconbtn:hover { background: var(--bg-3); color: var(--text-dim); }
.tp-iconbtn.active {
  background: var(--primary-bg);
  color: var(--primary-hover);
  border-color: var(--primary-bg-strong);
}

/* Header row 2 — meta strip */
.tp-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 14px 10px;
  border-bottom: 1px solid var(--border);
  background: var(--bg-1);
  flex-shrink: 0;
  flex-wrap: wrap;
}
.tp-pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 9px;
  border-radius: var(--r-full);
  background: var(--bg-3);
  border: 1px solid var(--border);
  font-family: var(--font-mono);
  font-size: 10.5px;
  color: var(--text-dim);
  letter-spacing: 0.04em;
  white-space: nowrap;
}
.tp-pill.speak-pill {
  background: var(--accent-bg);
  color: var(--accent);
  border-color: rgba(34, 211, 238, 0.30);
}
.tp-pill.speak-pill.stalled {
  background: var(--warning-bg);
  color: var(--warning);
  border-color: rgba(245, 158, 11, 0.40);
  animation: pill-stall 1.6s ease-in-out infinite;
}
@keyframes pill-stall {
  0%, 100% { box-shadow: 0 0 0 0 rgba(245, 158, 11, 0); }
  50%      { box-shadow: 0 0 0 4px rgba(245, 158, 11, 0.18); }
}
.tp-pill-age { opacity: 0.75; font-weight: 400; }
.tp-pill-warn { margin-left: 2px; }
.tp-pill.ended-pill {
  background: var(--success-bg);
  color: var(--success);
  border-color: rgba(16, 185, 129, 0.30);
  text-transform: uppercase;
  letter-spacing: 0.06em;
}

.tp-participants {
  display: flex;
  gap: 4px;
  flex-wrap: nowrap;
  overflow-x: auto;
  margin-left: auto;
  max-width: 60%;
  scrollbar-width: none;
}
.tp-participants::-webkit-scrollbar { display: none; }
.tp-part {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 2px 8px 2px 3px;
  border-radius: var(--r-full);
  background: var(--bg-2);
  border: 1px solid var(--border);
  font-size: 11px;
  color: var(--text-dim);
  flex-shrink: 0;
  transition: all 0.2s var(--ease-out);
}
.tp-part.speaking {
  background: color-mix(in srgb, var(--c) 22%, var(--bg-2));
  border-color: var(--c);
  /* Always use text for contrast; the colored ring/background communicates the
     role identity. Light role pastels (#A5B4FC etc.) as text were unreadable
     on white in light mode. */
  color: var(--text);
  transform: scale(1.06);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--c) 35%, transparent);
}
.tp-part-avatar {
  width: 18px; height: 18px;
  border-radius: 50%;
  background: color-mix(in srgb, var(--c) 45%, transparent);
  color: var(--text);
  display: flex; align-items: center; justify-content: center;
  font-family: var(--font-mono);
  font-size: 9px;
  font-weight: 600;
}
.tp-part-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 80px;
}

/* Stall warning banner */
.tp-stall-banner {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 14px;
  background: var(--warning-bg);
  border-bottom: 1px solid rgba(245, 158, 11, 0.30);
  color: var(--warning);
  font-size: 12px;
  flex-shrink: 0;
}
.tp-stall-icon { font-size: 14px; }
.tp-stall-ref {
  margin-left: auto;
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-muted);
}

/* Details drawer */
.tp-drawer {
  padding: 12px 14px;
  background: var(--bg-1);
  border-bottom: 1px solid var(--border);
  max-height: 280px;
  overflow-y: auto;
  flex-shrink: 0;
}
.tp-drawer-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.tp-drawer-head .mono-label {
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.16em;
  color: var(--text-muted);
  flex: 1;
}
.tp-drawer-close {
  width: 22px; height: 22px;
  border: 0; background: transparent;
  color: var(--text-muted);
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
  border-radius: var(--r-sm);
}
.tp-drawer-close:hover { background: var(--bg-3); color: var(--text); }
.tp-drawer-body {
  display: grid;
  grid-template-columns: 100px 1fr;
  gap: 4px 14px;
  font-size: 12.5px;
}
.tp-drawer-block { display: contents; }
.tp-drawer-label {
  font-family: var(--font-mono);
  font-size: 9.5px;
  color: var(--text-subtle);
  letter-spacing: 0.10em;
  text-transform: uppercase;
  padding-top: 3px;
}
.tp-drawer-val { color: var(--text-dim); line-height: 1.55; }
.tp-drawer-mono {
  font-family: var(--font-mono);
  font-size: 11.5px;
  color: var(--accent);
}

/* Transcript body */
.tp-body {
  flex: 1;
  overflow-y: auto;
  padding: 16px 20px;
  min-height: 0;
}
.tp-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 200px;
  gap: 12px;
  color: var(--text-muted);
  font-size: 13px;
}
.tp-empty-dots { display: flex; gap: 6px; }
.tp-empty-dots span {
  width: 7px; height: 7px;
  background: var(--primary);
  border-radius: 50%;
  animation: tp-bounce 1.4s ease-in-out infinite;
}
.tp-empty-dots span:nth-child(2) { animation-delay: 0.2s; }
.tp-empty-dots span:nth-child(3) { animation-delay: 0.4s; }
@keyframes tp-bounce {
  0%, 80%, 100% { transform: scale(0.6); opacity: 0.4; }
  40%           { transform: scale(1);   opacity: 1;   }
}

/* Transcript entry */
.entry {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 12px 0 12px 0;
  border-bottom: 1px solid var(--border);
  animation: entry-fade 0.2s ease;
}
.entry:last-child { border-bottom: 0; }
@keyframes entry-fade {
  from { opacity: 0; transform: translateY(4px); }
  to   { opacity: 1; transform: translateY(0); }
}
.entry-band {
  width: 3px;
  flex-shrink: 0;
  border-radius: 2px;
  background: var(--role-color);
  align-self: stretch;
}
.entry-avatar {
  width: 32px; height: 32px;
  border-radius: 50%;
  background: color-mix(in srgb, var(--role-color) 20%, transparent);
  color: var(--role-color);
  display: flex; align-items: center; justify-content: center;
  font-family: var(--font-mono);
  font-size: 12px;
  font-weight: 600;
  flex-shrink: 0;
}
.entry-body { flex: 1 1 0; min-width: 0; }
.entry-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
  flex-wrap: wrap;
}
.entry-agent {
  font-size: 12.5px;
  font-weight: 600;
  /* Role identity comes from the .entry-band stripe + .entry-tag chip.
     Use plain --text so the name is readable in both themes — light-mode
     pastels (#A5B4FC etc.) had ~2.3:1 contrast on white before. */
  color: var(--text);
}
.entry-tag {
  padding: 1px 6px;
  border-radius: var(--r-sm);
  background: color-mix(in srgb, var(--role-color) 28%, transparent);
  color: var(--text);
  font-family: var(--font-mono);
  font-size: 9.5px;
  letter-spacing: 0.10em;
  text-transform: uppercase;
}
.entry-round {
  padding: 0 5px;
  height: 14px;
  display: inline-flex; align-items: center;
  border-radius: var(--r-sm);
  background: var(--bg-3);
  border: 1px solid var(--border);
  font-family: var(--font-mono);
  font-size: 9px;
  color: var(--text-muted);
}
.entry-time {
  margin-left: auto;
  font-family: var(--font-mono);
  font-size: 9.5px;
  color: var(--text-subtle);
  letter-spacing: 0.06em;
}
.entry-verdict-chip {
  padding: 1px 7px;
  border-radius: var(--r-full);
  background: var(--primary-bg-strong);
  color: var(--primary-hover);
  border: 1px solid var(--primary);
  font-family: var(--font-mono);
  font-size: 9.5px;
  letter-spacing: 0.10em;
}

.entry-msg {
  font-size: 13px;
  color: var(--text-dim);
  line-height: 1.65;
  word-break: break-word;
  position: relative;
}
.entry-msg-collapsed {
  max-height: 320px;
  overflow: hidden;
}
.entry-msg-collapsed::after {
  content: '';
  position: absolute;
  inset: auto 0 0 0;
  height: 60px;
  background: linear-gradient(to bottom, transparent, var(--bg-0));
  pointer-events: none;
}
.entry-more {
  margin-top: 4px;
  background: transparent;
  border: 0;
  color: var(--primary-hover);
  font-size: 12px;
  cursor: pointer;
  padding: 2px 0;
}
.entry-more:hover { text-decoration: underline; }

/* Verdict entry — primary background highlight */
.entry-verdict {
  background: var(--primary-bg);
  border: 1px solid var(--primary-bg-strong);
  border-radius: var(--r-md);
  padding: 14px 16px;
  margin: 14px 0;
}
.entry-verdict .entry-band { display: none; }

/* Typing indicator placeholder for current speaker */
.entry-typing {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 0;
  margin-left: 14px;
  opacity: 0.92;
}
.entry-typing .entry-body { flex: 1 1 0; min-width: 0; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.entry-typing .entry-agent { color: var(--role-color); }
.typing-dots { display: inline-flex; gap: 4px; margin-left: 8px; }
.typing-dots span {
  width: 6px; height: 6px;
  border-radius: 50%;
  background: var(--role-color);
  display: inline-block;
  animation: tp-bounce 1.2s ease-in-out infinite;
}
.typing-dots span:nth-child(2) { animation-delay: 0.2s; }
.typing-dots span:nth-child(3) { animation-delay: 0.4s; }

/* Footer */
.tp-footer {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 20px;
  border-top: 1px solid var(--border);
  background: var(--bg-1);
  font-family: var(--font-mono);
  font-size: 10.5px;
  color: var(--text-muted);
  letter-spacing: 0.04em;
}
.tp-foot-dot {
  width: 5px; height: 5px;
  border-radius: 50%;
  background: var(--text-muted);
}
.tp-foot-dot.live {
  background: var(--accent);
  animation: tp-dot-pulse 1.4s ease-in-out infinite;
}
.tp-foot-url {
  font-family: var(--font-mono);
  font-size: 10.5px;
  color: var(--accent);
}
.tp-foot-tail {
  margin-left: auto;
  color: var(--text-subtle);
}

/* Mobile: when the meta strip wraps and participants drop to their
   own row, drop the 60% cap + the right-edge push (margin-left:auto).
   Let the strip claim the full row so every participant pill is
   visible — the desktop cap made the most-recently-added members
   silently clip at the right edge ("Reac…" cropped at iPhone-class
   widths). Switch from horizontal-scroll to multi-row wrap so users
   don't have to swipe to discover hidden participants. */
@media (max-width: 767px) {
  /* Diagnostic footer (SSE status + raw /stream URL) is dev noise on a phone
     and duplicates the connection badge already shown in the header — hide it
     to give the transcript that vertical space back. */
  .tp-footer { display: none; }

  /* Keep the roster on ONE horizontally-scrollable row instead of wrapping
     into 3–4 rows — wrapping ate most of the screen and left almost no room
     for the transcript. Names stay; swipe sideways to see everyone. */
  .tp-participants {
    width: 100%;
    max-width: none;
    margin-left: 0;
    flex-wrap: nowrap;
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
    gap: 6px;
    padding-bottom: 2px;
  }
  .tp-part { flex: 0 0 auto; }
}
</style>

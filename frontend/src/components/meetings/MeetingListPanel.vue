<script setup>
/**
 * MeetingListPanel — left-rail of MeetingsView.
 *
 * Filter tabs (All / Active / Ended) at the top, scrollable list below.
 * Each row: agenda + status + round counter + participant avatars + updated.
 * Selection is event-driven via @select(meetingId).
 */
import { computed } from 'vue'
import { normalizeTs } from '../../utils/timeFormat.js'
import { useLang } from '../../composables/useLang'

const { t } = useLang()

const props = defineProps({
  meetings: { type: Array, default: () => [] },
  selectedId: { type: String, default: null },
  activeCount: { type: Number, default: 0 },
  isLoading: { type: Boolean, default: false },
  error: { type: String, default: null },
  viewMode: { type: String, default: 'all' },
})

const emit = defineEmits(['select', 'retry', 'update:view-mode'])

const tabs = computed(() => [
  { key: 'all',    label: t('meetings.tabAll') },
  { key: 'active', label: t('meetings.tabActive') },
  { key: 'ended',  label: t('meetings.tabEnded') },
])

function statusInfo(m) {
  if (m.ended) {
    if (m.outcome === 'consensus')  return { color: 'var(--success)', label: t('meetings.statusConsensus'), icon: '✓' }
    if (m.outcome === 'max_rounds') return { color: 'var(--text-muted)', label: t('meetings.statusMaxRoundsShort'), icon: '◼' }
    return { color: 'var(--text-muted)', label: t('meetings.statusEnded'), icon: '◼' }
  }
  if (m.started) return { color: 'var(--success)', label: t('meetings.statusLive'), icon: '●' }
  return { color: 'var(--warning)', label: t('meetings.statusWaiting'), icon: '○' }
}

function formatTime(ts) {
  const ms = normalizeTs(ts)
  if (ms === null) return ''
  const diff = Date.now() - ms
  if (diff < 60_000) return t('meetings.justNow')
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h`
  return `${Math.floor(diff / 86_400_000)}d`
}

function truncate(s, n = 60) {
  if (!s) return t('meetings.noAgenda')
  return s.length > n ? s.slice(0, n) + '…' : s
}

function agentInitial(name) { return (name || '?').charAt(0).toUpperCase() }

// Stable color from agent index in participants — keeps initial swatches
// consistent across the list and detail panels.
const AGENT_COLORS = [
  '#3b82f6', '#10b981', '#f59e0b', '#8b5cf6', '#ec4899',
  '#14b8a6', '#f97316', '#6366f1', '#06b6d4', '#84cc16',
]
function agentColor(name, participants) {
  const i = (participants || []).indexOf(name)
  return i >= 0 ? AGENT_COLORS[i % AGENT_COLORS.length] : '#7B8094'
}

const hasMeetings = computed(() => props.meetings.length > 0)
</script>

<template>
  <div class="ml-panel">
    <!-- Head -->
    <div class="ml-head">
      <div class="ml-head-top">
        <h2 class="ml-title">📋 {{ t('meetings.listTitle') }}</h2>
        <span v-if="activeCount > 0" class="ml-active">{{ t('meetings.activeCount', { n: activeCount }) }}</span>
      </div>
      <div class="ml-seg">
        <button
          v-for="tab in tabs"
          :key="tab.key"
          class="ml-seg-btn"
          :class="{ 'is-active': viewMode === tab.key }"
          @click="emit('update:view-mode', tab.key)"
        >
          {{ tab.label }}
          <span v-if="tab.key === 'active' && activeCount > 0" class="ml-seg-count">{{ activeCount }}</span>
        </button>
      </div>
    </div>

    <!-- Body -->
    <div v-if="isLoading && !meetings.length" class="ml-state">
      <div class="ml-spinner" />
      <span>{{ t('meetings.loadingList') }}</span>
    </div>
    <div v-else-if="error" class="ml-state error">
      <span>⚠ {{ error }}</span>
      <button class="ml-retry" @click="emit('retry')">{{ t('meetings.retry') }}</button>
    </div>
    <div v-else-if="!hasMeetings" class="ml-state">
      <span class="ml-state-icon">📭</span>
      <span>{{ t('meetings.noMeetings') }}</span>
      <span class="ml-state-hint">{{ t('meetings.noMeetingsHint') }}</span>
    </div>

    <div v-else class="ml-list">
      <div
        v-for="m in meetings"
        :key="m.meeting_id"
        class="ml-item"
        :class="{ selected: m.meeting_id === selectedId, ended: m.ended }"
        @click="emit('select', m.meeting_id)"
      >
        <span class="ml-status-dot" :style="{ background: statusInfo(m).color }" />
        <div class="ml-item-body">
          <div class="ml-item-top">
            <span class="ml-item-agenda">{{ truncate(m.agenda) }}</span>
            <span class="ml-item-time">{{ formatTime(m.created_at) }}</span>
          </div>

          <!-- Participant avatars + count -->
          <div class="ml-item-participants">
            <div class="ml-avatars">
              <span
                v-for="(p, i) in (m.participants || []).slice(0, 4)"
                :key="p"
                class="ml-avatar"
                :style="{
                  background: agentColor(p, m.participants) + '4D',
                  color: 'var(--text)',
                  marginLeft: i > 0 ? '-6px' : '0',
                  zIndex: 10 - i,
                }"
                :title="p"
              >{{ agentInitial(p) }}</span>
              <span
                v-if="(m.participants || []).length > 4"
                class="ml-avatar ml-avatar-more"
              >+{{ m.participants.length - 4 }}</span>
            </div>
            <span class="ml-pcount">{{ t('meetings.participantsCount', { n: (m.participants || []).length }) }}</span>
          </div>

          <!-- Footer line: status badge, round counter, current speaker -->
          <div class="ml-item-foot">
            <span
              class="ml-badge"
              :style="{
                color: statusInfo(m).color,
                borderColor: statusInfo(m).color + '40',
                background: statusInfo(m).color + '15',
              }"
            >{{ statusInfo(m).icon }} {{ statusInfo(m).label }}</span>
            <span v-if="m.current_round" class="ml-round">
              R{{ m.current_round }}<span v-if="m.max_rounds">/{{ m.max_rounds }}</span>
            </span>
            <span v-if="m.turn_count" class="ml-turncount">{{ t('meetings.turnsCount', { n: m.turn_count }) }}</span>
            <span v-if="m.current_speaker && !m.ended" class="ml-speaker">
              🎙 {{ String(m.current_speaker).split(' ')[0] }}
            </span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ml-panel {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.ml-head {
  padding: 14px 14px 10px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.ml-head-top {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}
.ml-title {
  font-size: 14px;
  font-weight: 600;
  margin: 0;
  letter-spacing: -0.01em;
  color: var(--text);
}
.ml-active {
  padding: 1px 8px;
  border-radius: var(--r-full);
  background: var(--success-bg);
  color: var(--success);
  border: 1px solid rgba(16, 185, 129, 0.25);
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.06em;
}

.ml-seg {
  display: flex;
  padding: 3px;
  gap: 2px;
  background: var(--bg-2);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
}
.ml-seg-btn {
  flex: 1;
  height: 26px;
  border-radius: var(--r-sm);
  background: transparent;
  border: 0;
  color: var(--text-dim);
  font: inherit;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  transition: all 0.15s var(--ease-out);
}
.ml-seg-btn:hover { color: var(--text); }
.ml-seg-btn.is-active {
  background: var(--primary);
  color: white;
}
.ml-seg-count {
  font-family: var(--font-mono);
  font-size: 10px;
  padding: 0 4px;
  border-radius: var(--r-full);
  background: rgba(255, 255, 255, 0.18);
}

.ml-state {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 40px 20px;
  color: var(--text-muted);
  font-size: 12.5px;
  text-align: center;
}
.ml-state.error { color: var(--danger); }
.ml-state-icon { font-size: 28px; opacity: 0.45; }
.ml-state-hint { font-size: 11px; color: var(--text-subtle); }
.ml-spinner {
  width: 22px; height: 22px;
  border: 2px solid var(--border);
  border-top-color: var(--primary);
  border-radius: 50%;
  animation: mlspin 0.8s linear infinite;
}
@keyframes mlspin { to { transform: rotate(360deg); } }
.ml-retry {
  margin-top: 4px;
  padding: 5px 14px;
  background: var(--bg-3);
  color: var(--text-dim);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-sm);
  font-size: 11px;
  cursor: pointer;
}
.ml-retry:hover { background: var(--bg-4); color: var(--text); }

.ml-list {
  flex: 1;
  overflow-y: auto;
  padding: 6px;
}

.ml-item {
  display: grid;
  grid-template-columns: 8px 1fr;
  gap: 10px;
  padding: 12px;
  border-radius: var(--r-md);
  border-left: 2px solid transparent;
  cursor: pointer;
  margin-bottom: 2px;
  background: transparent;
  transition: all 0.15s var(--ease-out);
}
.ml-item:hover { background: var(--bg-2); }
.ml-item.selected {
  background: var(--primary-bg);
  border-left-color: var(--primary);
}
.ml-item.ended { opacity: 0.72; }
.ml-item.ended.selected, .ml-item.ended:hover { opacity: 1; }

.ml-status-dot {
  width: 6px; height: 6px;
  border-radius: 50%;
  margin-top: 6px;
}

.ml-item-body { min-width: 0; display: flex; flex-direction: column; gap: 6px; }
.ml-item-top {
  display: flex;
  justify-content: space-between;
  gap: 8px;
}
.ml-item-agenda {
  font-size: 12.5px;
  font-weight: 500;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
  min-width: 0;
}
.ml-item-time {
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-subtle);
  letter-spacing: 0.06em;
  flex-shrink: 0;
}

.ml-item-participants {
  display: flex;
  align-items: center;
  gap: 8px;
}
.ml-avatars { display: inline-flex; align-items: center; }
.ml-avatar {
  width: 20px; height: 20px;
  border-radius: 50%;
  font-family: var(--font-mono);
  font-size: 9px;
  font-weight: 600;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1.5px solid var(--bg-1);
}
.ml-avatar-more {
  background: var(--bg-3);
  color: var(--text-muted);
  margin-left: -6px;
}
.ml-pcount { font-size: 11px; color: var(--text-muted); }

.ml-item-foot {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.ml-badge {
  padding: 1px 7px;
  border-radius: var(--r-full);
  border: 1px solid;
  font-family: var(--font-mono);
  font-size: 9.5px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}
.ml-round {
  padding: 1px 7px;
  border-radius: var(--r-full);
  background: var(--bg-3);
  border: 1px solid var(--border);
  font-family: var(--font-mono);
  font-size: 9.5px;
  color: var(--text-muted);
  letter-spacing: 0.04em;
}
.ml-turncount {
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-subtle);
}
.ml-speaker {
  margin-left: auto;
  font-size: 10.5px;
  color: var(--accent);
}
</style>

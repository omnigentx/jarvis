<script setup>
import { computed } from 'vue'

const props = defineProps({
  meetings: { type: Array, required: true },
  selectedId: { type: String, default: null },
})

const emit = defineEmits(['select'])

// Helper functions
function statusColor(meeting) {
  if (meeting.ended) return '#555872'
  if (meeting.started) return '#10b981'
  return '#f59e0b' // waiting
}

function statusLabel(meeting) {
  if (meeting.ended) {
    if (meeting.outcome === 'consensus') return 'Consensus'
    if (meeting.outcome === 'max_rounds') return 'Max Rounds'
    return 'Ended'
  }
  if (meeting.started) return 'In Progress'
  return 'Waiting'
}

function statusIcon(meeting) {
  if (meeting.ended) return '✅'
  if (meeting.started) return '🔄'
  return '⏳'
}

function formatTime(ts) {
  if (!ts) return ''
  try {
    const d = new Date(ts)
    const now = new Date()
    const isToday = d.toDateString() === now.toDateString()
    if (isToday) {
      return d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' })
    }
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) + ' ' +
      d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' })
  } catch {
    return ''
  }
}

function truncateAgenda(agenda, max = 60) {
  if (!agenda) return 'No agenda'
  return agenda.length > max ? agenda.slice(0, max) + '…' : agenda
}

function agentInitial(name) {
  if (!name) return '?'
  return name.charAt(0).toUpperCase()
}
</script>

<template>
  <div class="meeting-list">
    <div
      v-for="meeting in meetings"
      :key="meeting.meeting_id"
      class="meeting-card"
      :class="{ selected: meeting.meeting_id === selectedId, ended: meeting.ended }"
      @click="emit('select', meeting.meeting_id)"
    >
      <!-- Status indicator -->
      <div class="meeting-status-dot" :style="{ background: statusColor(meeting) }"></div>

      <div class="meeting-card-body">
        <!-- Top row: agenda + time -->
        <div class="meeting-card-top">
          <span class="meeting-agenda">{{ truncateAgenda(meeting.agenda) }}</span>
          <span class="meeting-time">{{ formatTime(meeting.created_at) }}</span>
        </div>

        <!-- Middle: participants -->
        <div class="meeting-participants">
          <div class="participant-avatars">
            <div
              v-for="(p, i) in (meeting.participants || []).slice(0, 4)"
              :key="p"
              class="participant-avatar"
              :style="{ '--offset': i }"
              :title="p"
            >
              {{ agentInitial(p) }}
            </div>
            <div v-if="(meeting.participants || []).length > 4" class="participant-more">
              +{{ meeting.participants.length - 4 }}
            </div>
          </div>
          <span class="participant-count">
            {{ (meeting.participants || []).length }} participants
          </span>
        </div>

        <!-- Bottom: status + turn count -->
        <div class="meeting-card-bottom">
          <span class="meeting-badge" :style="{ color: statusColor(meeting), borderColor: statusColor(meeting) + '40', background: statusColor(meeting) + '15' }">
            {{ statusIcon(meeting) }} {{ statusLabel(meeting) }}
          </span>
          <span v-if="meeting.turn_count" class="turn-count">
            {{ meeting.turn_count }} turns
          </span>
          <span v-if="meeting.current_speaker && !meeting.ended" class="current-speaker">
            🎤 {{ meeting.current_speaker }}
          </span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.meeting-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}

.meeting-list::-webkit-scrollbar {
  width: 4px;
}
.meeting-list::-webkit-scrollbar-track {
  background: transparent;
}
.meeting-list::-webkit-scrollbar-thumb {
  background: #1a1d2e;
  border-radius: 4px;
}

.meeting-card {
  display: flex;
  gap: 10px;
  padding: 12px;
  border-radius: 8px;
  cursor: pointer;
  transition: all 0.15s ease;
  border: 1px solid transparent;
  margin-bottom: 4px;
}

.meeting-card:hover {
  background: #111318;
  border-color: #1a1d2e;
}

.meeting-card.selected {
  background: #111830;
  border-color: #2a3556;
}

.meeting-card.ended {
  opacity: 0.7;
}

.meeting-card.ended:hover,
.meeting-card.ended.selected {
  opacity: 1;
}

.meeting-status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-top: 6px;
  flex-shrink: 0;
}

.meeting-card-body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.meeting-card-top {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 8px;
}

.meeting-agenda {
  font-size: 13px;
  font-weight: 500;
  color: #f0f2f5;
  line-height: 1.3;
  flex: 1;
  min-width: 0;
}

.meeting-time {
  font-size: 11px;
  color: #555872;
  white-space: nowrap;
  flex-shrink: 0;
}

.meeting-participants {
  display: flex;
  align-items: center;
  gap: 8px;
}

.participant-avatars {
  display: flex;
  align-items: center;
}

.participant-avatar {
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: #1e3a5f;
  color: #3b82f6;
  font-size: 10px;
  font-weight: 600;
  display: flex;
  align-items: center;
  justify-content: center;
  margin-left: calc(var(--offset, 0) * 0px - 4px);
  border: 2px solid #0c0e15;
  position: relative;
  z-index: calc(10 - var(--offset, 0));
}

.participant-avatar:first-child {
  margin-left: 0;
}

.participant-more {
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: #1a1d2e;
  color: #8b8fa3;
  font-size: 9px;
  font-weight: 600;
  display: flex;
  align-items: center;
  justify-content: center;
  margin-left: -4px;
  border: 2px solid #0c0e15;
}

.participant-count {
  font-size: 11px;
  color: #8b8fa3;
}

.meeting-card-bottom {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.meeting-badge {
  font-size: 10px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 8px;
  border: 1px solid;
  white-space: nowrap;
}

.turn-count {
  font-size: 11px;
  color: #555872;
}

.current-speaker {
  font-size: 11px;
  color: #00d4aa;
  margin-left: auto;
}
</style>

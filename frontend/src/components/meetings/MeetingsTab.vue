<script setup>
import { ref, computed, watch } from 'vue'
import { useMeetingList } from '../../composables/useMeetingList'
import { useMeetingStream } from '../../composables/useMeetingStream'
import MeetingList from './MeetingList.vue'
import MeetingTranscript from './MeetingTranscript.vue'

const { meetings, activeMeetings, endedMeetings, isLoading, error, fetchMeetings } = useMeetingList()

const selectedMeetingId = ref(null)
const selectedMeeting = computed(() => {
  return meetings.value.find(m => m.meeting_id === selectedMeetingId.value) || null
})

// Meeting transcript stream
const { transcript, meetingState, isConnected, isConnecting, connect, disconnect } = useMeetingStream()

// Connect/disconnect when selection changes
watch(selectedMeetingId, (newId, oldId) => {
  if (oldId) disconnect()
  if (newId) connect(newId)
})

function selectMeeting(meetingId) {
  selectedMeetingId.value = meetingId
}

function clearSelection() {
  selectedMeetingId.value = null
  disconnect()
}

// View mode: 'all' | 'active' | 'ended'
const viewMode = ref('all')

const displayedMeetings = computed(() => {
  switch (viewMode.value) {
    case 'active': return activeMeetings.value
    case 'ended': return endedMeetings.value
    default: return meetings.value
  }
})
</script>

<template>
  <div class="meetings-tab">
    <!-- Master-Detail Layout -->
    <div class="meetings-layout" :class="{ 'has-detail': selectedMeetingId }">
      <!-- LEFT: Meeting List (Master) -->
      <div class="meetings-master">
        <div class="meetings-list-header">
          <div class="meetings-title-row">
            <h2 class="meetings-title">
              <span class="meetings-icon">📋</span>
              Meetings
            </h2>
            <span v-if="activeMeetings.length" class="active-badge">
              {{ activeMeetings.length }} Active
            </span>
          </div>
          <!-- View mode tabs -->
          <div class="view-tabs">
            <button
              v-for="tab in [
                { key: 'all', label: 'All' },
                { key: 'active', label: 'Active' },
                { key: 'ended', label: 'Ended' },
              ]"
              :key="tab.key"
              class="view-tab"
              :class="{ active: viewMode === tab.key }"
              @click="viewMode = tab.key"
            >
              {{ tab.label }}
              <span v-if="tab.key === 'active' && activeMeetings.length" class="tab-count">
                {{ activeMeetings.length }}
              </span>
            </button>
          </div>
        </div>

        <!-- Loading state -->
        <div v-if="isLoading && !meetings.length" class="meetings-loading">
          <div class="loading-spinner"></div>
          <span>Loading meetings...</span>
        </div>

        <!-- Error state -->
        <div v-else-if="error" class="meetings-error">
          <span class="error-icon">⚠️</span>
          <span>{{ error }}</span>
          <button class="retry-btn" @click="fetchMeetings">Retry</button>
        </div>

        <!-- Empty state -->
        <div v-else-if="!displayedMeetings.length" class="meetings-empty">
          <span class="empty-icon">📭</span>
          <p>No meetings found</p>
          <p class="empty-hint">Meetings appear here when agents create them</p>
        </div>

        <!-- Meeting list -->
        <MeetingList
          v-else
          :meetings="displayedMeetings"
          :selected-id="selectedMeetingId"
          @select="selectMeeting"
        />
      </div>

      <!-- RIGHT: Meeting Transcript (Detail) -->
      <div v-if="selectedMeetingId" class="meetings-detail">
        <MeetingTranscript
          :meeting="selectedMeeting"
          :transcript="transcript"
          :meeting-state="meetingState"
          :is-connected="isConnected"
          :is-connecting="isConnecting"
          @close="clearSelection"
        />
      </div>

      <!-- No selection placeholder -->
      <div v-else class="meetings-detail-empty">
        <div class="empty-detail-content">
          <span class="empty-detail-icon">💬</span>
          <h3>Select a meeting</h3>
          <p>Choose a meeting from the list to view its transcript in real time</p>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.meetings-tab {
  height: 100%;
  display: flex;
  flex-direction: column;
}

.meetings-layout {
  display: grid;
  grid-template-columns: 380px 1fr;
  gap: 0;
  height: calc(100vh - 200px);
  min-height: 500px;
  border: 1px solid #1a1d2e;
  border-radius: 12px;
  overflow: hidden;
  background: #0c0e15;
}

/* Master panel */
.meetings-master {
  display: flex;
  flex-direction: column;
  border-right: 1px solid #1a1d2e;
  background: #0c0e15;
  overflow: hidden;
}

.meetings-list-header {
  padding: 16px 16px 12px;
  border-bottom: 1px solid #1a1d2e;
  flex-shrink: 0;
}

.meetings-title-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}

.meetings-title {
  font-size: 16px;
  font-weight: 600;
  color: #f0f2f5;
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0;
}

.meetings-icon {
  font-size: 18px;
}

.active-badge {
  background: rgba(16, 185, 129, 0.15);
  color: #10b981;
  font-size: 11px;
  font-weight: 600;
  padding: 3px 8px;
  border-radius: 10px;
  border: 1px solid rgba(16, 185, 129, 0.25);
}

.view-tabs {
  display: flex;
  gap: 4px;
  background: #111318;
  border-radius: 8px;
  padding: 3px;
}

.view-tab {
  flex: 1;
  padding: 5px 10px;
  font-size: 12px;
  font-weight: 500;
  color: #8b8fa3;
  background: transparent;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.15s ease;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
}

.view-tab:hover {
  color: #c4c8d4;
}

.view-tab.active {
  background: #1e2233;
  color: #f0f2f5;
}

.tab-count {
  background: rgba(16, 185, 129, 0.2);
  color: #10b981;
  font-size: 10px;
  padding: 1px 5px;
  border-radius: 6px;
}

/* Loading / Error / Empty states */
.meetings-loading,
.meetings-error,
.meetings-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 40px 20px;
  gap: 12px;
  color: #8b8fa3;
  flex: 1;
}

.loading-spinner {
  width: 24px;
  height: 24px;
  border: 2px solid #1a1d2e;
  border-top: 2px solid #3b82f6;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.error-icon { font-size: 24px; }
.empty-icon { font-size: 32px; }

.retry-btn {
  margin-top: 8px;
  padding: 6px 16px;
  background: #1e2233;
  color: #c4c8d4;
  border: 1px solid #1a1d2e;
  border-radius: 6px;
  font-size: 12px;
  cursor: pointer;
  transition: all 0.15s;
}

.retry-btn:hover {
  background: #2a3556;
  color: #f0f2f5;
}

.meetings-empty p {
  margin: 0;
  font-size: 14px;
}

.empty-hint {
  font-size: 12px !important;
  color: #555872;
}

/* Detail panel */
.meetings-detail {
  display: flex;
  flex-direction: column;
  background: #0a0d14;
  overflow: hidden;
}

.meetings-detail-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  background: #0a0d14;
}

.empty-detail-content {
  text-align: center;
  color: #555872;
}

.empty-detail-icon {
  font-size: 48px;
  display: block;
  margin-bottom: 16px;
  opacity: 0.4;
}

.empty-detail-content h3 {
  color: #8b8fa3;
  font-weight: 500;
  margin-bottom: 8px;
}

.empty-detail-content p {
  font-size: 13px;
}

/* ─── Responsive ─── */
@media (max-width: 767px) {
  /* Layout becomes a stacking context for slide-in */
  .meetings-layout {
    position: relative;
    display: block;          /* collapse grid, use absolute children */
    height: calc(100dvh - 52px - 60px - 80px); /* viewport - mobile header - bottom nav - sub-nav approx */
    min-height: 400px;
    overflow: hidden;        /* clips the sliding panels */
    border-radius: 0;
    border-left: none;
    border-right: none;
  }

  /* Master: occupies full available space */
  .meetings-master {
    position: absolute;
    inset: 0;
    border-right: none;
    border-bottom: none;
    max-height: unset;       /* remove the 400px hard-cap */
    transform: translateX(0);
    transition: transform 0.28s cubic-bezier(0.4, 0, 0.2, 1);
    will-change: transform;
    overflow-y: auto;
  }

  /* When detail is visible, master slides off to the left */
  .meetings-layout.has-detail .meetings-master {
    transform: translateX(-100%);
    pointer-events: none;
  }

  /* Detail: starts off-screen to the right */
  .meetings-detail {
    position: absolute;
    inset: 0;
    transform: translateX(100%);
    transition: transform 0.28s cubic-bezier(0.4, 0, 0.2, 1);
    will-change: transform;
    overflow: hidden;
  }

  /* When meeting selected, detail slides in */
  .meetings-layout.has-detail .meetings-detail {
    transform: translateX(0);
  }

  /* Empty placeholder: always hidden on mobile (no room) */
  .meetings-detail-empty {
    display: none;
  }
}
</style>

<script setup>
/**
 * MeetingsView — Track 5 new view.
 *
 * Master-detail layout: meeting list on the left, live transcript on the right.
 * Uses useMeetingList for the list (SSE-driven) and useMeetingStream for the
 * selected meeting's transcript. Both composables are unmodified.
 *
 * Components live in components/meetings/* (MeetingListPanel,
 * MeetingTranscriptPanel) — separated so the view can mount/dismount the
 * transcript stream cleanly when selection changes.
 */
import { ref, computed, watch } from 'vue'
import { useMeetingList } from '../composables/useMeetingList'
import { useMeetingStream } from '../composables/useMeetingStream'
import { useRoute, useRouter } from 'vue-router'
import { useBreakpoint } from '../composables/useBreakpoint'
import { useLang } from '../composables/useLang'
import MeetingListPanel from '../components/meetings/MeetingListPanel.vue'
import MeetingTranscriptPanel from '../components/meetings/MeetingTranscriptPanel.vue'
import MeetingsEmpty from '../components/meetings/MeetingsEmpty.vue'

defineOptions({ name: 'Meetings' })

const { isMobile } = useBreakpoint()
const { t } = useLang()
const route = useRoute()
const router = useRouter()

const { meetings, activeMeetings, endedMeetings, isLoading, error, fetchMeetings } = useMeetingList()
const { transcript, meetingState, isConnected, isConnecting, connect, disconnect } = useMeetingStream()

// View mode tabs
const viewMode = ref('all')
const displayedMeetings = computed(() => {
  switch (viewMode.value) {
    case 'active': return activeMeetings.value
    case 'ended':  return endedMeetings.value
    default:       return meetings.value
  }
})

// Selection — kept on the URL via ?id= so deep-links work. We don't have an
// owned /meetings/:id route (router-touch budget), but ?id= is good enough.
const selectedMeetingId = ref(route.query.id || null)
const selectedMeeting = computed(() =>
  meetings.value.find(m => m.meeting_id === selectedMeetingId.value) || null,
)

watch(selectedMeetingId, (newId, oldId) => {
  if (oldId) disconnect()
  if (newId) connect(newId)
  // Reflect in URL without adding a stack entry
  if (router && route.name === 'Meetings') {
    router.replace({ name: 'Meetings', query: newId ? { id: newId } : {} })
  }
}, { immediate: true })

function selectMeeting(id) { selectedMeetingId.value = id }
function clearSelection() { selectedMeetingId.value = null }
</script>

<template>
  <div class="meetings-root jv" :class="{ 'meetings-root-mobile': isMobile }">
    <!-- Page header -->
    <header class="meetings-pagehead">
      <div class="eyebrow">{{ t('meetings.eyebrow') }}</div>
      <h1 class="meetings-title">{{ t('meetings.title') }}</h1>
      <span v-if="activeMeetings.length" class="meetings-active-pill">
        {{ t('meetings.activeCountPill', { n: activeMeetings.length }) }}
      </span>
      <span class="meetings-hint">
        {{ t('meetings.hintPrefix') }} <code>create_meeting</code>{{ t('meetings.hintSuffix') }}
      </span>
    </header>

    <div class="meetings-body" :class="{ 'has-detail': selectedMeetingId }">
      <!-- Master: meeting list -->
      <aside class="meetings-master">
        <MeetingListPanel
          :meetings="displayedMeetings"
          :selected-id="selectedMeetingId"
          :active-count="activeMeetings.length"
          :is-loading="isLoading"
          :error="error"
          :view-mode="viewMode"
          @update:view-mode="viewMode = $event"
          @select="selectMeeting"
          @retry="fetchMeetings"
        />
      </aside>

      <!-- Detail: transcript or empty -->
      <section class="meetings-detail">
        <MeetingTranscriptPanel
          v-if="selectedMeeting"
          :meeting="selectedMeeting"
          :transcript="transcript"
          :meeting-state="meetingState"
          :is-connected="isConnected"
          :is-connecting="isConnecting"
          @close="clearSelection"
        />
        <MeetingsEmpty v-else />
      </section>
    </div>
  </div>
</template>

<style scoped>
.meetings-root {
  display: flex;
  flex-direction: column;
  height: calc(100% + 48px);
  margin: -24px -36px;
  background: var(--bg-0);
  color: var(--text);
}
.meetings-root-mobile { margin: -16px -12px; height: calc(100% + 32px); }

.meetings-pagehead {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 14px 24px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
  background: var(--bg-1);
}
.meetings-title {
  font-size: 16px;
  font-weight: 600;
  margin: 0;
  letter-spacing: -0.01em;
}
.meetings-active-pill {
  display: inline-flex;
  align-items: center;
  padding: 2px 10px;
  border-radius: var(--r-full);
  background: var(--success-bg);
  color: var(--success);
  border: 1px solid rgba(16, 185, 129, 0.25);
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.10em;
}
.meetings-hint {
  margin-left: auto;
  font-size: 12px;
  color: var(--text-muted);
}
.meetings-hint code {
  font-family: var(--font-mono);
  font-size: 11px;
  background: var(--bg-3);
  padding: 1px 6px;
  border-radius: var(--r-sm);
  color: var(--accent);
}

.meetings-body {
  flex: 1;
  display: flex;
  overflow: hidden;
  min-height: 0;
}
.meetings-master {
  width: 320px;
  flex-shrink: 0;
  border-right: 1px solid var(--border);
  background: var(--bg-1);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.meetings-detail {
  flex: 1;
  display: flex;
  flex-direction: column;
  background: var(--bg-0);
  overflow: hidden;
  min-width: 0;
}

/* Mobile: slide master/detail */
@media (max-width: 767px) {
  .meetings-pagehead { padding: 10px 14px; gap: 10px; }
  .meetings-hint { display: none; }

  .meetings-body {
    position: relative;
    flex: 1;
    display: block;
    overflow: hidden;
  }
  .meetings-master {
    position: absolute;
    inset: 0;
    width: 100%;
    border-right: 0;
    transition: transform 0.28s cubic-bezier(0.4, 0, 0.2, 1);
  }
  .meetings-detail {
    position: absolute;
    inset: 0;
    transform: translateX(100%);
    transition: transform 0.28s cubic-bezier(0.4, 0, 0.2, 1);
  }
  .meetings-body.has-detail .meetings-master {
    transform: translateX(-100%);
    pointer-events: none;
  }
  .meetings-body.has-detail .meetings-detail {
    transform: translateX(0);
  }
}
</style>

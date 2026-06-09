<script setup>
/**
 * NotificationDetail — full-page detail for a single notification.
 *
 * Logic preserved verbatim (fetch + mark read, TTS via audioPlayerStore,
 * mark-unread + delete actions). Visual shell restyled.
 */
import { ref, onMounted, onBeforeUnmount, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useBreakpoint } from '../composables/useBreakpoint'
import { apiFetch } from '../api'
import { useAudioPlayerStore } from '../stores/audioPlayer'
import MarkdownRenderer from '../components/MarkdownRenderer.vue'

const route = useRoute()
const router = useRouter()
const { isMobile } = useBreakpoint()

const notification = ref(null)
const loading = ref(true)
const error = ref(null)

const meta = computed(() => notification.value?.metadata || {})
const audioStore = useAudioPlayerStore()

const ttsState = computed(() => audioStore.notifTtsState)

let _ttsBlockedTimer = null

async function toggleTTS() {
  const state = ttsState.value

  if (state === 'playing') {
    audioStore.notifTtsState = 'paused'
    _pauseNotifTts()
    return
  }
  if (state === 'paused') {
    _resumeNotifTts()
    return
  }
  if (!notification.value) return
  const content = notification.value.content || notification.value.preview || ''
  const text = [notification.value.title, content].filter(Boolean).join('\n\n').trim()
  if (!text) return

  audioStore.notifTtsState = 'loading'
  try {
    const res = await apiFetch('/api/tts/prepare', {
      method: 'POST',
      body: JSON.stringify({ text }),
    })
    if (!res.audio_url) throw new Error('No audio_url in response')

    const allowed = audioStore.startNotifTts(res.audio_url)
    if (!allowed) {
      audioStore.notifTtsState = 'blocked'
      clearTimeout(_ttsBlockedTimer)
      _ttsBlockedTimer = setTimeout(() => {
        if (audioStore.notifTtsState === 'blocked') {
          audioStore.notifTtsState = 'idle'
        }
      }, 3000)
    }
  } catch (e) {
    console.error('[NotifTTS] Error:', e)
    audioStore.notifTtsState = 'error'
    setTimeout(() => {
      if (audioStore.notifTtsState === 'error') audioStore.notifTtsState = 'idle'
    }, 2000)
  }
}

function _pauseNotifTts() {
  audioStore.notifTtsState = 'paused'
  window.dispatchEvent(new CustomEvent('notif-tts-pause'))
}
function _resumeNotifTts() {
  audioStore.notifTtsState = 'playing'
  window.dispatchEvent(new CustomEvent('notif-tts-resume'))
}

onBeforeUnmount(() => {
  clearTimeout(_ttsBlockedTimer)
})

function statusColor(status) {
  if (status === 'success') return 'var(--success)'
  if (status === 'failed') return 'var(--danger)'
  return 'var(--warning)'
}
function typeLabel(type) {
  if (type === 'reminder') return 'Reminder'
  if (type === 'agent_result') return 'Agent Result'
  if (type === 'error') return 'Error'
  return type
}
function typeColor(type) {
  if (type === 'reminder') return 'var(--warning)'
  if (type === 'agent_result') return 'var(--primary-hover)'
  if (type === 'error') return 'var(--danger)'
  return 'var(--text-muted)'
}

function formatTime(ts) {
  if (!ts) return ''
  return new Date(ts * 1000).toLocaleString([], {
    day: '2-digit', month: '2-digit', year: 'numeric',
    hour: '2-digit', minute: '2-digit',
  })
}
function formatDuration(ms) {
  if (!ms) return '—'
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

async function fetchDetail() {
  loading.value = true
  error.value = null
  try {
    const id = route.params.id
    const data = await apiFetch(`/api/notifications/${id}`)
    notification.value = data
    if (!data.is_read) {
      await apiFetch(`/api/notifications/${id}/read`, { method: 'PATCH' })
      notification.value.is_read = true
      window.dispatchEvent(new Event('notification-badge-update'))
    }
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

async function markUnread() {
  if (!notification.value) return
  try {
    await apiFetch(`/api/notifications/${notification.value.id}/unread`, { method: 'PATCH' })
    notification.value.is_read = false
    window.dispatchEvent(new Event('notification-badge-update'))
  } catch (e) { console.error('Failed to mark unread:', e) }
}

async function deleteNotif() {
  if (!notification.value) return
  try {
    await apiFetch(`/api/notifications/${notification.value.id}`, { method: 'DELETE' })
    window.dispatchEvent(new Event('notification-badge-update'))
    router.push('/notifications')
  } catch (e) { console.error('Failed to delete:', e) }
}

function goBack() { router.push('/notifications') }

const ttsLabel = computed(() => {
  if (ttsState.value === 'loading') return 'Loading…'
  if (ttsState.value === 'playing') return 'Pause'
  if (ttsState.value === 'paused') return 'Resume'
  if (ttsState.value === 'blocked') return 'Story playing'
  return 'Listen'
})

onMounted(fetchDetail)
</script>

<template>
  <div class="notif-detail jv" :class="{ 'notif-detail--mobile': isMobile }">
    <!-- Loading / error -->
    <div v-if="loading" class="notif-detail__state">Loading…</div>

    <div v-else-if="error" class="notif-detail__state notif-detail__state--error">
      <span style="font-size: 24px;">⚠</span>
      <span>{{ error }}</span>
      <button class="btn btn-secondary" @click="goBack">← Back to list</button>
    </div>

    <template v-else-if="notification">
      <!-- Header -->
      <div class="notif-detail__header">
        <!-- Mobile already shows a back arrow in the global app-bar; hide this
             in-content one there to avoid two stacked back buttons. -->
        <button v-if="!isMobile" class="btn btn-ghost notif-detail__back" @click="goBack">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M19 12H5"/><polyline points="12 19 5 12 12 5"/>
          </svg>
          Back
        </button>

        <div class="notif-detail__heading">
          <div class="mono-label" :style="{ color: typeColor(notification.type) }">
            ● {{ typeLabel(notification.type) }}
          </div>
          <h1 class="notif-detail__title">
            <span class="grad" v-if="!isMobile" style="font-style: italic; padding-right: 0.1em;">{{ notification.title.split(' ')[0] }}</span>
            {{ isMobile ? notification.title : notification.title.split(' ').slice(1).join(' ') }}
          </h1>
        </div>

        <div class="notif-detail__actions">
          <button
            class="btn"
            :class="ttsState !== 'idle' ? 'btn-primary' : 'btn-secondary'"
            :disabled="ttsState === 'loading'"
            @click="toggleTTS"
          >
            <span v-if="ttsState === 'loading'" class="notif-detail__tts-spin">↻</span>
            <span v-else-if="ttsState === 'playing'">⏸</span>
            <span v-else-if="ttsState === 'blocked'">⊘</span>
            <span v-else>▶</span>
            {{ ttsLabel }}
          </button>
          <button v-if="notification.is_read" class="btn btn-secondary" @click="markUnread">Mark unread</button>
          <button class="btn btn-secondary notif-detail__delete" @click="deleteNotif">Delete</button>
        </div>
      </div>

      <!-- Meta pills -->
      <div v-if="meta.agent || meta.exec_mode || meta.duration_ms || meta.status" class="notif-detail__meta">
        <div v-if="meta.agent" class="notif-detail__pill">
          <span class="mono-label">AGENT</span>
          <span>{{ meta.agent }}</span>
        </div>
        <div v-if="meta.exec_mode" class="notif-detail__pill">
          <span class="mono-label">MODE</span>
          <span>{{ meta.exec_mode }}</span>
        </div>
        <div v-if="meta.duration_ms" class="notif-detail__pill">
          <span class="mono-label">DURATION</span>
          <span>{{ formatDuration(meta.duration_ms) }}</span>
        </div>
        <div v-if="meta.status" class="notif-detail__pill">
          <span class="mono-label">STATUS</span>
          <span :style="{ color: statusColor(meta.status) }">● {{ meta.status }}</span>
        </div>
        <div class="notif-detail__pill">
          <span class="mono-label">TIME</span>
          <span>{{ formatTime(notification.created_at) }}</span>
        </div>
      </div>

      <!-- Content -->
      <div class="notif-detail__body card">
        <div class="notif-detail__body-head">
          <span>Content</span>
          <span class="mono-label">{{ notification.content_type || 'text' }}</span>
        </div>
        <div class="notif-detail__body-content">
          <MarkdownRenderer
            :content="notification.content || notification.preview || 'No content'"
            :content-type="notification.content_type || 'text'"
          />
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.notif-detail {
  display: flex;
  flex-direction: column;
  gap: 14px;
  color: var(--text);
  max-width: 1100px;
}

.notif-detail__state {
  padding: 60px 20px;
  text-align: center;
  color: var(--text-muted);
  font-size: 13px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
}
.notif-detail__state--error { color: var(--warning); }

.notif-detail__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  padding-bottom: 14px;
  border-bottom: 1px solid var(--border);
}
.notif-detail__back {
  height: 30px;
  padding: 0 10px;
  font-size: 12px;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: var(--text-muted);
}
.notif-detail__heading { flex: 1; min-width: 0; }
.notif-detail__title {
  font-family: var(--font-display);
  font-size: 22px;
  letter-spacing: -0.02em;
  margin: 6px 0 0;
}
.notif-detail__actions {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}
.notif-detail__delete:hover {
  color: var(--danger);
  border-color: rgba(239,68,68,0.3);
}

.notif-detail__tts-spin {
  display: inline-block;
  animation: tts-rotate 0.9s linear infinite;
}
@keyframes tts-rotate { to { transform: rotate(360deg); } }

/* Meta pills */
.notif-detail__meta {
  display: flex;
  gap: 10px;
  overflow-x: auto;
  scrollbar-width: none;
  flex-wrap: wrap;
}
.notif-detail__meta::-webkit-scrollbar { display: none; }
.notif-detail__pill {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px 12px;
  background: var(--bg-2);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  min-width: 100px;
}
.notif-detail__pill > span:last-child {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
  white-space: nowrap;
}

.notif-detail__body { padding: 0; }
.notif-detail__body-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 18px;
  border-bottom: 1px solid var(--border);
  font-size: 13px;
  font-weight: 600;
}
.notif-detail__body-content { padding: 20px 22px; font-size: 14px; line-height: 1.65; color: var(--text-dim); }

@media (max-width: 768px) {
  .notif-detail__title { font-size: 18px; overflow-wrap: anywhere; }
  .notif-detail__body-content { padding: 14px 16px; }
  /* (FAB bottom safe-zone is now handled globally in AppLayout's content area.) */
  /* Stack the header so the title gets full width (it was crushed one-word-per
     -line beside the action buttons, which also overlapped the type label).
     Actions move to their own full-width row below. */
  .notif-detail__header { flex-direction: column; align-items: stretch; }
  .notif-detail__actions { width: 100%; }
  .notif-detail__actions .btn { flex: 1; justify-content: center; }
}
</style>

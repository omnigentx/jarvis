<script setup>
/**
 * NotificationDetail — full-page detail view for a single notification.
 * Mobile: sticky header + content scroll + bottom action bar.
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

// ── TTS ────────────────────────────────────────────────────────────────────
/**
 * ttsState is driven by audioStore.notifTtsState:
 *   'idle'    → button shows Listen
 *   'loading' → button shows spinner
 *   'playing' → button shows Pause
 *   'paused'  → button shows Resume
 *   'blocked' → story is playing, TTS skipped, show toast
 *   'error'   → transient, auto-resets to idle
 */
const ttsState = computed(() => audioStore.notifTtsState)

let _ttsBlockedTimer = null

async function toggleTTS() {
  const state = ttsState.value

  // Pause if currently our audio
  if (state === 'playing') {
    audioStore.notifTtsState = 'paused'
    _pauseNotifTts()
    return
  }

  if (state === 'paused') {
    _resumeNotifTts()
    return
  }

  // 'blocked', 'error', 'idle' → start fresh
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

    // startNotifTts returns false if story is actively playing
    const allowed = audioStore.startNotifTts(res.audio_url)
    if (!allowed) {
      // Story is playing — show blocked state briefly
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

function stopTTS() {
  clearTimeout(_ttsBlockedTimer)
  audioStore.stopNotifTts()
}

onBeforeUnmount(() => {
  clearTimeout(_ttsBlockedTimer)
})
// ──────────────────────────────────────────────────────────────────────────

function statusColor(status) {
  if (status === 'success') return '#10b981'
  if (status === 'failed') return '#ef4444'
  return '#f59e0b'
}

function typeLabel(type) {
  if (type === 'reminder') return 'Reminder'
  if (type === 'agent_result') return 'Agent Result'
  if (type === 'error') return 'Error'
  return type
}

function typeColor(type) {
  if (type === 'reminder') return '#FFB547'
  if (type === 'agent_result') return '#3B82F6'
  if (type === 'error') return '#EF4444'
  return '#8B8FA3'
}

function formatTime(ts) {
  if (!ts) return ''
  return new Date(ts * 1000).toLocaleString('vi-VN', {
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

onMounted(fetchDetail)
</script>

<template>
  <!-- ═══════════════════════════════════════════════════════ -->
  <!-- MOBILE layout                                          -->
  <!-- ═══════════════════════════════════════════════════════ -->
  <div v-if="isMobile" class="notif-detail-mobile">

    <!-- Loading -->
    <div v-if="loading" class="notif-detail-mobile__state">
      <span class="notif-detail-mobile__state-text">Loading...</span>
    </div>

    <!-- Error -->
    <div v-else-if="error" class="notif-detail-mobile__state">
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#f59e0b" stroke-width="2">
        <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>
      </svg>
      <span class="notif-detail-mobile__state-text" style="margin-top: 8px;">{{ error }}</span>
    </div>

    <template v-else-if="notification">
      <!-- Scrollable content -->
      <div class="notif-detail-mobile__scroll">

        <!-- Hero section: type badge + title -->
        <div class="notif-detail-mobile__hero" :style="{ borderBottomColor: typeColor(notification.type) + '33' }">
          <!-- Type badge -->
          <div class="notif-detail-mobile__type-badge" :style="{ background: typeColor(notification.type) + '1a', color: typeColor(notification.type) }">
            {{ typeLabel(notification.type) }}
          </div>
          <h1 class="notif-detail-mobile__title">{{ notification.title }}</h1>

          <!-- Time + TTS button on same row -->
          <div class="notif-detail-mobile__hero-foot">
            <span class="notif-detail-mobile__time">{{ formatTime(notification.created_at) }}</span>
            <button
              class="tts-hero-btn"
              :class="{
                'tts-hero-btn--active': ttsState === 'playing' || ttsState === 'paused',
                'tts-hero-btn--blocked': ttsState === 'blocked',
              }"
              :disabled="ttsState === 'loading'"
              @click="toggleTTS"
              :title="ttsState === 'blocked' ? 'Story is playing — stop it first to listen' : ''"
            >
              <svg v-if="ttsState === 'loading'" class="tts-spin" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M12 3a9 9 0 0 1 9 9" stroke-linecap="round"/></svg>
              <svg v-else-if="ttsState === 'playing'" width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/></svg>
              <svg v-else-if="ttsState === 'blocked'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg>
              <svg v-else width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5.14v14l11-7-11-7z"/></svg>
              {{
                ttsState === 'loading' ? 'Loading…'
                : ttsState === 'playing' ? 'Pause'
                : ttsState === 'paused' ? 'Resume'
                : ttsState === 'blocked' ? 'Story playing'
                : 'Listen'
              }}
            </button>
          </div>
        </div>

        <!-- Meta pills -->
        <div v-if="meta.agent || meta.exec_mode || meta.duration_ms || meta.status" class="notif-detail-mobile__meta">
          <div v-if="meta.agent" class="meta-pill">
            <span class="meta-pill__label">Agent</span>
            <span class="meta-pill__value">{{ meta.agent }}</span>
          </div>
          <div v-if="meta.exec_mode" class="meta-pill">
            <span class="meta-pill__label">Mode</span>
            <span class="meta-pill__value">{{ meta.exec_mode }}</span>
          </div>
          <div v-if="meta.duration_ms" class="meta-pill">
            <span class="meta-pill__label">Duration</span>
            <span class="meta-pill__value">{{ formatDuration(meta.duration_ms) }}</span>
          </div>
          <div v-if="meta.status" class="meta-pill">
            <span class="meta-pill__label">Status</span>
            <span class="meta-pill__value" :style="{ color: statusColor(meta.status) }">● {{ meta.status }}</span>
          </div>
        </div>

        <!-- Content body -->
        <div class="notif-detail-mobile__body">
          <MarkdownRenderer
            :content="notification.content || notification.preview || 'No content'"
            :content-type="notification.content_type || 'text'"
          />
        </div>
      </div>

      <!-- Sticky bottom action bar -->
      <div class="notif-detail-mobile__actions">

        <button
          v-if="notification.is_read"
          class="notif-detail-mobile__action-btn"
          @click="markUnread"
        >
          Unread
        </button>
        <button class="notif-detail-mobile__action-btn notif-detail-mobile__action-btn--danger" @click="deleteNotif">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <polyline points="3 6 5 6 21 6"/>
            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
          </svg>
        </button>
      </div>
    </template>
  </div>

  <!-- ═══════════════════════════════════════════════════════ -->
  <!-- DESKTOP layout (unchanged)                             -->
  <!-- ═══════════════════════════════════════════════════════ -->
  <div v-else style="display: flex; flex-direction: column; gap: 24px; max-width: 1148px;">

    <!-- Loading -->
    <div v-if="loading" style="padding: 60px 20px; text-align: center; color: #64748b; font-size: 13px;">
      Loading...
    </div>

    <!-- Error -->
    <div v-else-if="error" style="text-align: center; padding: 60px 0; color: #64748b;">
      <div style="font-size: 14px;">⚠️ {{ error }}</div>
      <button @click="goBack" class="btn-hover" style="margin-top: 12px; padding: 8px 16px; font-size: 13px; color: #b8c0d4; background: #111318; border: 1px solid #1e2030; border-radius: 8px; cursor: pointer;">
        ← Back to list
      </button>
    </div>

    <template v-else-if="notification">
      <!-- Header -->
      <div style="display: flex; justify-content: space-between; align-items: flex-start;">
        <div style="display: flex; flex-direction: column; gap: 8px;">
          <div style="display: flex; align-items: center; gap: 12px;">
            <button
              @click="goBack"
              class="btn-hover"
              style="display: flex; align-items: center; gap: 4px; padding: 6px 12px; font-size: 12px; font-weight: 500; color: #8b8fa3; background: #111318; border: 1px solid #1e2030; border-radius: 8px; cursor: pointer; transition: all 0.2s;"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M19 12H5"/><polyline points="12 19 5 12 12 5"/></svg>
              Back
            </button>
            <span style="font-size: 12px; padding: 3px 10px; border-radius: 12px; background: rgba(59,130,246,0.1); color: #3b82f6;">
              {{ typeLabel(notification.type) }}
            </span>
          </div>
          <h1 style="font-size: 24px; font-weight: 700; color: #f3f6fc; line-height: 29px; margin: 0;">
            {{ notification.title }}
          </h1>
        </div>

        <div style="display: flex; gap: 8px; align-items: center;">
          <!-- TTS button desktop -->
          <button class="tts-btn-desktop" :class="{ 'tts-btn-desktop--active': ttsState !== 'idle' }" :disabled="ttsState === 'loading'" @click="toggleTTS">
            <svg v-if="ttsState === 'loading'" class="tts-spin" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M12 3a9 9 0 0 1 9 9" stroke-linecap="round"/></svg>
            <svg v-else-if="ttsState === 'playing'" width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/></svg>
            <svg v-else width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5.14v14l11-7-11-7z"/></svg>
            {{ ttsState === 'loading' ? 'Loading…' : ttsState === 'playing' ? 'Pause' : ttsState === 'paused' ? 'Resume' : 'Listen' }}
          </button>
          <button v-if="notification.is_read" @click="markUnread" class="btn-hover" style="height: 34px; padding: 0 14px; border-radius: 8px; font-size: 13px; font-weight: 500; cursor: pointer; border: 1px solid #1e2030; background: #111318; color: #b8c0d4; transition: all 0.2s;">Mark unread</button>
          <button @click="deleteNotif" class="btn-hover btn-danger" style="height: 34px; padding: 0 14px; border-radius: 8px; font-size: 13px; font-weight: 500; cursor: pointer; border: 1px solid #1e2030; background: #111318; color: #b8c0d4; transition: all 0.2s;">Delete</button>
        </div>
      </div>

      <!-- Metadata Cards -->
      <div style="display: flex; gap: 16px;">
        <div v-if="meta.agent" style="flex: 1; background: #111318; border: 1px solid #1e2030; border-radius: 12px; padding: 14px 16px; display: flex; flex-direction: column; gap: 4px;" class="metric-card">
          <span style="font-size: 11px; font-weight: 600; color: #64748b; text-transform: uppercase; letter-spacing: 0.5px;">Agent</span>
          <span style="font-size: 15px; font-weight: 600; color: #f3f6fc;">{{ meta.agent }}</span>
        </div>
        <div v-if="meta.exec_mode" style="flex: 1; background: #111318; border: 1px solid #1e2030; border-radius: 12px; padding: 14px 16px; display: flex; flex-direction: column; gap: 4px;" class="metric-card">
          <span style="font-size: 11px; font-weight: 600; color: #64748b; text-transform: uppercase; letter-spacing: 0.5px;">Mode</span>
          <span style="font-size: 15px; font-weight: 600; color: #f3f6fc;">{{ meta.exec_mode }}</span>
        </div>
        <div v-if="meta.duration_ms" style="flex: 1; background: #111318; border: 1px solid #1e2030; border-radius: 12px; padding: 14px 16px; display: flex; flex-direction: column; gap: 4px;" class="metric-card">
          <span style="font-size: 11px; font-weight: 600; color: #64748b; text-transform: uppercase; letter-spacing: 0.5px;">Duration</span>
          <span style="font-size: 15px; font-weight: 600; color: #f3f6fc;">{{ formatDuration(meta.duration_ms) }}</span>
        </div>
        <div v-if="meta.status" style="flex: 1; background: #111318; border: 1px solid #1e2030; border-radius: 12px; padding: 14px 16px; display: flex; flex-direction: column; gap: 4px;" class="metric-card">
          <span style="font-size: 11px; font-weight: 600; color: #64748b; text-transform: uppercase; letter-spacing: 0.5px;">Status</span>
          <span style="font-size: 15px; font-weight: 600;" :style="{ color: statusColor(meta.status) }">● {{ meta.status }}</span>
        </div>
        <div style="flex: 1; background: #111318; border: 1px solid #1e2030; border-radius: 12px; padding: 14px 16px; display: flex; flex-direction: column; gap: 4px;" class="metric-card">
          <span style="font-size: 11px; font-weight: 600; color: #64748b; text-transform: uppercase; letter-spacing: 0.5px;">Time</span>
          <span style="font-size: 15px; font-weight: 600; color: #f3f6fc;">{{ formatTime(notification.created_at) }}</span>
        </div>
      </div>

      <!-- Content -->
      <div style="background: #111318; border: 1px solid #1e2030; border-radius: 12px; overflow: hidden;">
        <div style="padding: 14px 20px; border-bottom: 1px solid #1e2030; display: flex; align-items: center;">
          <span style="font-size: 14px; font-weight: 600; color: #f3f6fc;">Content</span>
          <span style="margin-left: auto; font-size: 11px; color: #8b8fa3;">{{ notification.content_type }}</span>
        </div>
        <div style="padding: 24px 20px;">
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
/* ═══ Mobile detail ═══ */
.notif-detail-mobile {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 52px - 60px); /* minus mobile header and bottom nav */
  overflow: hidden;
}

.notif-detail-mobile__state {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 40px 20px;
}
.notif-detail-mobile__state-text {
  font-size: 13px;
  color: #555872;
}
/* Back button inside hero (always visible) */
.notif-detail-mobile__top-back {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 32px;
  padding: 0 12px;
  border-radius: 16px;
  font-size: 13px;
  font-weight: 500;
  font-family: 'Inter', sans-serif;
  color: #8b8fa3;
  background: rgba(255,255,255,0.04);
  border: 1px solid #1e2030;
  cursor: pointer;
  transition: all 0.2s ease;
  align-self: flex-start;
  margin-bottom: 4px;
}
.notif-detail-mobile__top-back:active {
  background: rgba(59, 130, 246, 0.08);
  color: #3b82f6;
  border-color: #3b82f6;
}

/* Scrollable content area */
.notif-detail-mobile__scroll {
  flex: 1;
  overflow-y: auto;
  -webkit-overflow-scrolling: touch;
}

/* Hero */
.notif-detail-mobile__hero {
  padding: 20px 16px 16px;
  border-bottom: 1px solid #1a1d2e;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.notif-detail-mobile__type-badge {
  display: inline-flex;
  align-items: center;
  height: 22px;
  padding: 0 10px;
  border-radius: 11px;
  font-size: 11px;
  font-weight: 600;
  font-family: 'Inter', sans-serif;
  align-self: flex-start;
}
.notif-detail-mobile__title {
  font-size: 18px;
  font-weight: 700;
  color: #f0f2f5;
  line-height: 1.3;
  margin: 0;
}
.notif-detail-mobile__time {
  font-size: 11px;
  color: #555872;
}

/* Time + TTS button row inside hero */
.notif-detail-mobile__hero-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 2px;
}

/* TTS button in hero (compact pill style) */
.tts-hero-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 28px;
  padding: 0 12px;
  border-radius: 14px;
  font-size: 12px;
  font-weight: 600;
  font-family: 'Inter', sans-serif;
  cursor: pointer;
  border: 1px solid rgba(0, 212, 170, 0.35);
  background: rgba(0, 212, 170, 0.08);
  color: #00d4aa;
  transition: all 0.18s ease;
  flex-shrink: 0;
}
.tts-hero-btn:hover {
  background: rgba(0, 212, 170, 0.15);
  border-color: #00d4aa;
}
.tts-hero-btn--active {
  background: rgba(0, 212, 170, 0.14);
  border-color: rgba(0, 212, 170, 0.6);
}
.tts-hero-btn--blocked {
  background: rgba(245, 158, 11, 0.1);
  border-color: rgba(245, 158, 11, 0.4);
  color: #f59e0b;
  cursor: default;
}
.tts-hero-btn:disabled {
  opacity: 0.55;
  pointer-events: none;
}

/* Meta pills row */
.notif-detail-mobile__meta {
  display: flex;
  gap: 8px;
  padding: 12px 16px;
  overflow-x: auto;
  scrollbar-width: none;
  border-bottom: 1px solid #1a1d2e;
}
.notif-detail-mobile__meta::-webkit-scrollbar { display: none; }

.meta-pill {
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
  background: #111318;
  border: 1px solid #1e2030;
  border-radius: 10px;
  padding: 8px 12px;
}
.meta-pill__label {
  font-size: 9px;
  font-weight: 600;
  color: #555872;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.meta-pill__value {
  font-size: 13px;
  font-weight: 600;
  color: #f0f2f5;
  white-space: nowrap;
}

/* Body */
.notif-detail-mobile__body {
  padding: 16px;
  font-size: 14px;
  line-height: 1.6;
  color: #c4c8d4;
}

/* Sticky bottom action bar */
.notif-detail-mobile__actions {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
  background: #111318;
  border-top: 1px solid #1e2030;
  flex-shrink: 0;
}
.notif-detail-mobile__action-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 1;
  justify-content: center;
  height: 40px;
  font-size: 13px;
  font-weight: 500;
  font-family: 'Inter', sans-serif;
  color: #c4c8d4;
  background: #1e2233;
  border: 1px solid #1e2030;
  border-radius: 10px;
  cursor: pointer;
  transition: all 0.2s ease;
}
.notif-detail-mobile__action-btn:active {
  background: #232840;
}
.notif-detail-mobile__action-btn--danger {
  color: #ef4444;
  border-color: rgba(239, 68, 68, 0.2);
}
.notif-detail-mobile__action-btn--danger:active {
  background: rgba(239, 68, 68, 0.08);
}

/* ═══ Desktop ═══ */
.metric-card:hover { border-color: #2a3556 !important; }
.btn-hover:hover { border-color: #2a3556 !important; color: #f3f6fc !important; }
.btn-danger:hover { color: #ef4444 !important; border-color: rgba(239, 68, 68, 0.4) !important; }

/* ── TTS button desktop ── */
.tts-btn-desktop {
  display: inline-flex; align-items: center; gap: 6px;
  height: 34px; padding: 0 14px; border-radius: 8px;
  font-size: 13px; font-weight: 500; font-family: 'Inter', sans-serif;
  cursor: pointer; border: 1px solid rgba(0,212,170,0.3);
  background: rgba(0,212,170,0.06); color: #00d4aa;
  transition: all 0.2s;
}
.tts-btn-desktop:hover { background: rgba(0,212,170,0.12); border-color: #00d4aa; }
.tts-btn-desktop--active { background: rgba(0,212,170,0.1); border-color: rgba(0,212,170,0.5); }
.tts-btn-desktop:disabled { opacity: 0.55; cursor: not-allowed; }

/* ── TTS button mobile ── */
.notif-detail-mobile__action-btn--tts {
  color: #00d4aa; border-color: rgba(0,212,170,0.25);
}
.notif-detail-mobile__action-btn--tts.tts-active,
.notif-detail-mobile__action-btn--tts:active {
  background: rgba(0,212,170,0.08); border-color: rgba(0,212,170,0.5);
}
.notif-detail-mobile__action-btn--tts:disabled { opacity: 0.55; pointer-events: none; }

/* Spinner */
.tts-spin { animation: tts-rotate 0.9s linear infinite; transform-origin: center; }
@keyframes tts-rotate { to { transform: rotate(360deg); } }
</style>

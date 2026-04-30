<script setup>
/**
 * NotificationList — timeline list with filters, badges, pagination.
 * SSE subscription for realtime updates via scheduler stream.
 * Mobile: compact chip filter bar with unread toggle pill.
 */
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { useBreakpoint } from '../composables/useBreakpoint'
import { apiFetch, buildSSEUrl } from '../api'

const router = useRouter()
const { isMobile } = useBreakpoint()

// State
const notifications = ref([])
const total = ref(0)
const loading = ref(true)
const loadingMore = ref(false)
const offset = ref(0)
const limit = 20

// Filters
const typeFilter = ref('all')
const unreadOnly = ref(false)

const typeChips = [
  { value: 'all',          label: 'All' },
  { value: 'reminder',     label: 'Reminders' },
  { value: 'agent_result', label: 'Agent' },
  { value: 'error',        label: 'Failed' },
]

const hasMore = computed(() => offset.value + limit < total.value)
const unreadCount = computed(() => notifications.value.filter((n) => !n.is_read).length)

// ─── Fetch ───
async function fetchNotifications(reset = false) {
  if (reset) { offset.value = 0; loading.value = true }
  else { loadingMore.value = true }

  try {
    const params = new URLSearchParams()
    if (typeFilter.value !== 'all') params.set('type', typeFilter.value)
    if (unreadOnly.value) params.set('is_read', '0')
    params.set('limit', limit)
    params.set('offset', offset.value)

    const data = await apiFetch(`/api/notifications?${params.toString()}`)
    if (reset) { notifications.value = data.items }
    else { notifications.value.push(...data.items) }
    total.value = data.total
  } catch (e) {
    console.error('Failed to fetch notifications:', e)
  } finally {
    loading.value = false
    loadingMore.value = false
  }
}

function loadMore() { offset.value += limit; fetchNotifications(false) }

// ─── Actions ───
async function markAllRead() {
  try {
    await apiFetch('/api/notifications/mark-all-read', { method: 'POST' })
    notifications.value.forEach((n) => { n.is_read = true })
    window.dispatchEvent(new Event('notification-badge-update'))
  } catch (e) { console.error('Failed to mark all read:', e) }
}

async function deleteNotification(id, event) {
  event.stopPropagation()
  try {
    await apiFetch(`/api/notifications/${id}`, { method: 'DELETE' })
    notifications.value = notifications.value.filter((n) => n.id !== id)
    total.value = Math.max(0, total.value - 1)
    window.dispatchEvent(new Event('notification-badge-update'))
  } catch (e) { console.error('Failed to delete notification:', e) }
}

function openDetail(n) { router.push(`/notifications/${n.id}`) }

function setTypeFilter(val) { typeFilter.value = val; fetchNotifications(true) }
function toggleUnreadOnly() { unreadOnly.value = !unreadOnly.value; fetchNotifications(true) }

// ─── Helpers ───
function relativeTime(ts) {
  if (!ts) return ''
  const seconds = Math.floor(Date.now() / 1000 - ts)
  if (seconds < 60) return 'vừa xong'
  if (seconds < 3600) return `${Math.floor(seconds / 60)} phút trước`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} giờ trước`
  const d = new Date(ts * 1000)
  return d.toLocaleDateString('vi-VN', { day: '2-digit', month: '2-digit' })
}

function typeIcon(type) {
  if (type === 'reminder') return 'bell'
  if (type === 'agent_result') return 'cpu'
  if (type === 'error') return 'alert-circle'
  return 'inbox'
}

function typeColor(type) {
  if (type === 'reminder') return '#FFB547'
  if (type === 'agent_result') return '#3B82F6'
  if (type === 'error') return '#EF4444'
  return '#8B8FA3'
}

// ─── SSE ───
let eventSource = null

function connectSSE() {
  const url = buildSSEUrl('/api/scheduler/stream')
  eventSource = new EventSource(url)
  eventSource.onmessage = (ev) => {
    try {
      const data = JSON.parse(ev.data)
      if (data.type === 'new_notification') {
        notifications.value.unshift({
          id: data.id,
          type: data.notif_type || data.type,
          title: data.title,
          preview: data.preview,
          is_read: false,
          created_at: data.created_at,
        })
        total.value += 1
      }
    } catch (_) {}
  }
  eventSource.onerror = () => {
    eventSource?.close()
    setTimeout(connectSSE, 5000)
  }
}

onMounted(() => { fetchNotifications(true); connectSSE() })
onUnmounted(() => { eventSource?.close() })
</script>

<template>
  <!-- ═══════════════════════════════════════════════════════ -->
  <!-- MOBILE layout                                          -->
  <!-- ═══════════════════════════════════════════════════════ -->
  <div v-if="isMobile" class="notif-mobile">

    <!-- Filter bar: compact chip row -->
    <div class="notif-mobile__filters">
      <div class="notif-mobile__chips">
        <button
          v-for="chip in typeChips"
          :key="chip.value"
          class="notif-chip"
          :class="{ 'notif-chip--active': typeFilter === chip.value }"
          @click="setTypeFilter(chip.value)"
        >
          {{ chip.label }}
        </button>
      </div>

      <!-- Unread toggle pill -->
      <button
        class="notif-unread-pill"
        :class="{ 'notif-unread-pill--active': unreadOnly }"
        @click="toggleUnreadOnly"
      >
        <span class="notif-unread-pill__dot" />
        {{ unreadCount > 0 ? unreadCount : '0' }} unread
      </button>
    </div>

    <!-- List -->
    <div class="notif-mobile__list">
      <!-- Loading -->
      <div v-if="loading" class="notif-mobile__state">
        <span class="notif-mobile__state-text">Loading...</span>
      </div>

      <!-- Empty -->
      <div v-else-if="notifications.length === 0" class="notif-mobile__state">
        <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#555872" stroke-width="1.5">
          <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/>
          <path d="M13.73 21a2 2 0 0 1-3.46 0"/>
        </svg>
        <span class="notif-mobile__state-text" style="margin-top: 8px;">Không có thông báo</span>
      </div>

      <!-- Rows -->
      <TransitionGroup v-else name="notif-list" tag="div">
        <div
          v-for="n in notifications"
          :key="n.id"
          class="notif-mobile__row"
          :class="{ 'notif-mobile__row--unread': !n.is_read }"
          @click="openDetail(n)"
        >
          <!-- Unread bar -->
          <div class="notif-mobile__unread-bar" :style="{ background: !n.is_read ? '#3B82F6' : 'transparent' }" />

          <!-- Type icon -->
          <div class="notif-mobile__icon" :style="{ background: typeColor(n.type) + '1a' }">
            <!-- Bell -->
            <svg v-if="typeIcon(n.type) === 'bell'" width="18" height="18" viewBox="0 0 24 24" fill="none" :stroke="typeColor(n.type)" stroke-width="2" stroke-linecap="round">
              <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/>
            </svg>
            <!-- CPU -->
            <svg v-else-if="typeIcon(n.type) === 'cpu'" width="18" height="18" viewBox="0 0 24 24" fill="none" :stroke="typeColor(n.type)" stroke-width="2" stroke-linecap="round">
              <rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/>
              <path d="M15 2v2M9 2v2M2 15h2M2 9h2M15 20v2M9 20v2M20 15h2M20 9h2"/>
            </svg>
            <!-- Alert circle -->
            <svg v-else-if="typeIcon(n.type) === 'alert-circle'" width="18" height="18" viewBox="0 0 24 24" fill="none" :stroke="typeColor(n.type)" stroke-width="2" stroke-linecap="round">
              <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>
            </svg>
            <!-- Inbox fallback -->
            <svg v-else width="18" height="18" viewBox="0 0 24 24" fill="none" :stroke="typeColor(n.type)" stroke-width="2" stroke-linecap="round">
              <polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/>
              <path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>
            </svg>
          </div>

          <!-- Content -->
          <div class="notif-mobile__content">
            <div class="notif-mobile__title-row">
              <span class="notif-mobile__title" :class="{ 'notif-mobile__title--unread': !n.is_read }">
                {{ n.title }}
              </span>
              <span class="notif-mobile__time">{{ relativeTime(n.created_at) }}</span>
            </div>
            <span class="notif-mobile__preview">{{ n.preview || 'No content' }}</span>
          </div>

          <!-- Actions -->
          <div class="notif-mobile__actions">
            <span v-if="!n.is_read" class="notif-mobile__dot" />
            <span v-else style="width: 8px; height: 8px;" />
            <button class="notif-mobile__delete" @click.stop="deleteNotification(n.id, $event)" title="Delete">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <polyline points="3 6 5 6 21 6"/>
                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
              </svg>
            </button>
          </div>
        </div>
      </TransitionGroup>

      <!-- Load more -->
      <div v-if="hasMore && !loading" class="notif-mobile__load-more">
        <button class="notif-mobile__load-btn" @click="loadMore" :disabled="loadingMore">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <polyline points="7 13 12 18 17 13"/><polyline points="7 6 12 11 17 6"/>
          </svg>
          {{ loadingMore ? 'Loading...' : 'Load more' }}
        </button>
      </div>
    </div>
  </div>

  <!-- ═══════════════════════════════════════════════════════ -->
  <!-- DESKTOP layout (unchanged)                             -->
  <!-- ═══════════════════════════════════════════════════════ -->
  <div v-else style="display: flex; flex-direction: column; gap: 24px; max-width: 1148px;">

    <!-- Header -->
    <div style="display: flex; justify-content: space-between; align-items: flex-start;">
      <div style="display: flex; flex-direction: column; gap: 4px;">
        <h1 style="font-size: 24px; font-weight: 700; color: #f3f6fc; line-height: 29px; margin: 0;">
          Notifications
        </h1>
        <p style="font-size: 13px; font-weight: 400; color: #b8c0d4; line-height: 16px; margin: 0;">
          Review results from scheduler jobs, reminders, and agent tasks.
        </p>
      </div>

      <div style="display: flex; align-items: center; gap: 12px;">
        <span v-if="unreadCount > 0" style="font-size: 12px; font-weight: 600; padding: 4px 12px; background: rgba(59,130,246,0.12); color: #3b82f6; border-radius: 12px;">
          {{ unreadCount }} unread
        </span>
        <button
          @click="markAllRead"
          :disabled="unreadCount === 0"
          :style="{
            height: '34px', padding: '0 16px', borderRadius: '8px',
            fontSize: '13px', fontWeight: '500', fontFamily: 'Inter, sans-serif',
            cursor: unreadCount > 0 ? 'pointer' : 'default',
            border: '1px solid #1e2030', background: '#111318',
            color: unreadCount > 0 ? '#b8c0d4' : '#555872', transition: 'all 0.2s ease',
            whiteSpace: 'nowrap',
          }"
          class="btn-hover"
        >
          Mark all as read
        </button>
      </div>
    </div>

    <!-- Filters -->
    <div style="display: flex; gap: 12px; align-items: center;">
      <div style="display: flex; gap: 4px; background: #111318; border: 1px solid #1e2030; border-radius: 10px; padding: 3px;">
        <button
          v-for="chip in typeChips"
          :key="chip.value"
          @click="setTypeFilter(chip.value)"
          :style="{
            height: '30px', padding: '0 14px', borderRadius: '8px',
            fontSize: '12px', fontWeight: '600', fontFamily: 'Inter, sans-serif',
            cursor: 'pointer', border: 'none',
            background: typeFilter === chip.value ? '#3b82f6' : 'transparent',
            color: typeFilter === chip.value ? '#ffffff' : '#8b8fa3',
            transition: 'all 0.2s ease', whiteSpace: 'nowrap',
          }"
        >{{ chip.label }}</button>
      </div>

      <div style="width: 1px; height: 24px; background: #1e2030;" />

      <div style="display: flex; gap: 4px; background: #111318; border: 1px solid #1e2030; border-radius: 10px; padding: 3px;">
        <button
          v-for="opt in [{ value: false, label: 'All' }, { value: true, label: 'Unread' }]"
          :key="String(opt.value)"
          @click="unreadOnly = opt.value; fetchNotifications(true)"
          :style="{
            height: '30px', padding: '0 14px', borderRadius: '8px',
            fontSize: '12px', fontWeight: '600', fontFamily: 'Inter, sans-serif',
            cursor: 'pointer', border: 'none',
            background: unreadOnly === opt.value ? '#3b82f6' : 'transparent',
            color: unreadOnly === opt.value ? '#ffffff' : '#8b8fa3',
            transition: 'all 0.2s ease',
          }"
        >{{ opt.label }}</button>
      </div>

      <span style="margin-left: auto; font-size: 11px; color: #8b8fa3;">
        {{ total }} notification{{ total !== 1 ? 's' : '' }}
      </span>
    </div>

    <!-- List card -->
    <div style="background: #111318; border: 1px solid #1e2030; border-radius: 12px; overflow: hidden;">
      <div v-if="loading" style="padding: 60px 20px; text-align: center; color: #64748b; font-size: 13px;">
        Loading notifications...
      </div>

      <div v-else-if="notifications.length === 0" style="display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 60px 0; color: #64748b;">
        <span style="font-size: 32px; margin-bottom: 8px;">📭</span>
        <span style="font-size: 13px;">No notifications yet</span>
        <span style="font-size: 11px; color: #555872; margin-top: 4px;">Notifications will appear when scheduler jobs complete.</span>
      </div>

      <TransitionGroup v-else name="notif-list" tag="div">
        <div
          v-for="n in notifications"
          :key="n.id"
          class="notif-row"
          :style="{
            display: 'flex', alignItems: 'center', gap: '12px',
            padding: '14px 20px', borderBottom: '1px solid rgba(26, 29, 46, 0.5)',
            cursor: 'pointer', transition: 'background 0.15s ease',
            borderLeft: !n.is_read ? '3px solid #3b82f6' : '3px solid transparent',
          }"
          @click="openDetail(n)"
        >
          <span style="font-size: 18px; flex-shrink: 0; width: 28px; text-align: center;">
            {{ n.type === 'reminder' ? '🔔' : n.type === 'agent_result' ? '🤖' : n.type === 'error' ? '❌' : '📩' }}
          </span>

          <div style="flex: 1; min-width: 0;">
            <div style="display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 3px;">
              <span style="font-size: 13px; font-weight: 600; color: #f3f6fc; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">
                {{ n.title }}
              </span>
              <span style="font-size: 11px; color: #64748b; flex-shrink: 0;">
                {{ relativeTime(n.created_at) }}
              </span>
            </div>
            <span style="font-size: 12px; color: #8b8fa3; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; display: block;">
              {{ n.preview || 'No content' }}
            </span>
          </div>

          <div style="display: flex; align-items: center; gap: 8px; flex-shrink: 0;">
            <div v-if="!n.is_read" style="width: 8px; height: 8px; background: #3b82f6; border-radius: 50%;" />
            <button
              class="delete-btn"
              @click.stop="deleteNotification(n.id, $event)"
              style="padding: 4px; color: #555872; background: transparent; border: none; border-radius: 4px; cursor: pointer; display: flex; transition: color 0.2s;"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <polyline points="3 6 5 6 21 6"/>
                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
              </svg>
            </button>
          </div>
        </div>
      </TransitionGroup>

      <div v-if="hasMore && !loading" style="padding: 16px; text-align: center; border-top: 1px solid #1e2030;">
        <button
          @click="loadMore"
          :disabled="loadingMore"
          :style="{
            padding: '8px 28px', fontSize: '12px', fontWeight: '600',
            color: '#b8c0d4', background: 'transparent',
            border: '1px solid #1e2030', borderRadius: '8px',
            cursor: 'pointer', transition: 'all 0.2s ease',
          }"
          class="btn-hover"
        >
          {{ loadingMore ? 'Loading...' : 'Load more' }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* ═══ Mobile styles ═══ */
.notif-mobile {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
}

/* Filter bar */
.notif-mobile__filters {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 12px;
  background: #111318;
  border-bottom: 1px solid #1e2030;
  flex-shrink: 0;
}

.notif-mobile__chips {
  display: flex;
  gap: 6px;
  flex: 1;
  overflow-x: auto;
  scrollbar-width: none;
}
.notif-mobile__chips::-webkit-scrollbar { display: none; }

.notif-chip {
  flex-shrink: 0;
  height: 30px;
  padding: 0 14px;
  border-radius: 15px;
  font-size: 12px;
  font-weight: 500;
  font-family: 'Inter', sans-serif;
  cursor: pointer;
  border: 1px solid #1e2030;
  background: #0a0d14;
  color: #8b8fa3;
  transition: all 0.2s ease;
  white-space: nowrap;
}
.notif-chip--active {
  background: #3b82f6;
  border-color: #3b82f6;
  color: #ffffff;
  font-weight: 600;
}

/* Unread toggle pill */
.notif-unread-pill {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 5px;
  height: 30px;
  padding: 0 12px;
  border-radius: 15px;
  font-size: 11px;
  font-weight: 600;
  font-family: 'Inter', sans-serif;
  cursor: pointer;
  border: none;
  background: rgba(59, 130, 246, 0.1);
  color: #8b8fa3;
  transition: all 0.2s ease;
}
.notif-unread-pill--active {
  background: rgba(59, 130, 246, 0.15);
  color: #3b82f6;
}
.notif-unread-pill__dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #8b8fa3;
  flex-shrink: 0;
  transition: background 0.2s;
}
.notif-unread-pill--active .notif-unread-pill__dot {
  background: #3b82f6;
}

/* List area */
.notif-mobile__list {
  flex: 1;
  overflow-y: auto;
  -webkit-overflow-scrolling: touch;
}

/* State (empty / loading) */
.notif-mobile__state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 60px 20px;
}
.notif-mobile__state-text {
  font-size: 13px;
  color: #555872;
}

/* Row */
.notif-mobile__row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 14px 12px 0;
  border-bottom: 1px solid #1a1d2e;
  cursor: pointer;
  transition: background 0.15s ease;
  position: relative;
}
.notif-mobile__row:active {
  background: rgba(59, 130, 246, 0.04);
}
.notif-mobile__row--unread {
  background: rgba(13, 17, 23, 0.8);
}

/* Unread left bar (3px) */
.notif-mobile__unread-bar {
  width: 3px;
  height: 48px;
  border-radius: 0 2px 2px 0;
  flex-shrink: 0;
}

/* Icon circle */
.notif-mobile__icon {
  width: 36px;
  height: 36px;
  border-radius: 18px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

/* Content */
.notif-mobile__content {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.notif-mobile__title-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
}
.notif-mobile__title {
  font-size: 13px;
  font-weight: 500;
  color: #c4c8d4;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
}
.notif-mobile__title--unread {
  font-weight: 600;
  color: #f0f2f5;
}
.notif-mobile__time {
  font-size: 10px;
  color: #555872;
  flex-shrink: 0;
  white-space: nowrap;
}
.notif-mobile__preview {
  font-size: 12px;
  color: #8b8fa3;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  display: block;
}

/* Actions col */
.notif-mobile__actions {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: space-between;
  height: 48px;
  flex-shrink: 0;
  padding-right: 2px;
}
.notif-mobile__dot {
  width: 8px;
  height: 8px;
  background: #3b82f6;
  border-radius: 50%;
}
.notif-mobile__delete {
  padding: 4px;
  color: #555872;
  background: transparent;
  border: none;
  border-radius: 4px;
  cursor: pointer;
  display: flex;
  align-items: center;
  transition: color 0.2s;
}
.notif-mobile__delete:active { color: #ef4444; }

/* Load more */
.notif-mobile__load-more {
  padding: 16px;
  display: flex;
  justify-content: center;
}
.notif-mobile__load-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 24px;
  font-size: 12px;
  font-weight: 500;
  color: #c4c8d4;
  background: transparent;
  border: 1px solid #1e2030;
  border-radius: 8px;
  cursor: pointer;
  font-family: 'Inter', sans-serif;
  transition: all 0.2s ease;
}
.notif-mobile__load-btn:active {
  border-color: #2a3556;
  color: #f0f2f5;
}

/* ═══ Desktop styles ═══ */
.notif-row:hover { background: rgba(59, 130, 246, 0.04); }
.delete-btn:hover { color: #ef4444 !important; }
.btn-hover:hover:not(:disabled) { border-color: #2a3556 !important; color: #f3f6fc !important; }

/* Transitions */
.notif-list-enter-active { transition: all 0.3s ease; }
.notif-list-enter-from { transform: translateY(-10px); opacity: 0; }
</style>

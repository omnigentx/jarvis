<script setup>
/**
 * NotificationList — timeline + filters with realtime SSE updates.
 *
 * Logic preserved verbatim (REST paging, mark-all-read, SSE merge).
 * Visual layout restyled to match the new design.
 */
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useBreakpoint } from '../composables/useBreakpoint'
import { apiFetch, buildSSEUrl } from '../api'
import { useSSEConnection } from '../composables/useSSEConnection.js'

const router = useRouter()
const { isMobile } = useBreakpoint()

const notifications = ref([])
const total = ref(0)
const loading = ref(true)
const loadingMore = ref(false)
const offset = ref(0)
const limit = 20

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
const actionableCount = computed(() => notifications.value.filter(n => (n.metadata?.actionable) || n.type === 'error').length)

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

function relativeTime(ts) {
  if (!ts) return ''
  const seconds = Math.floor(Date.now() / 1000 - ts)
  if (seconds < 60) return 'just now'
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  const d = new Date(ts * 1000)
  return d.toLocaleDateString([], { day: '2-digit', month: '2-digit' })
}

function typeColor(type) {
  if (type === 'reminder') return 'var(--warning)'
  if (type === 'agent_result') return 'var(--primary-hover)'
  if (type === 'error') return 'var(--danger)'
  return 'var(--text-muted)'
}

function typeIconName(type) {
  if (type === 'reminder') return 'bell'
  if (type === 'agent_result') return 'cpu'
  if (type === 'error') return 'alert'
  return 'inbox'
}

useSSEConnection(buildSSEUrl('/api/scheduler/stream'), {
  onMessage(ev) {
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
  },
})

onMounted(() => { fetchNotifications(true) })
</script>

<template>
  <div class="notif jv">
    <!-- ─── Mobile chips bar ─── -->
    <div v-if="isMobile" class="notif-mobile">
      <div class="notif-mobile__filters">
        <div class="notif-mobile__chips">
          <button
            v-for="chip in typeChips"
            :key="chip.value"
            class="notif-mobile__chip"
            :class="{ 'notif-mobile__chip--active': typeFilter === chip.value }"
            @click="setTypeFilter(chip.value)"
          >{{ chip.label }}</button>
        </div>
        <button
          class="notif-mobile__unread"
          :class="{ 'notif-mobile__unread--active': unreadOnly }"
          @click="toggleUnreadOnly"
        >
          <span class="notif-mobile__unread-dot"></span>
          {{ unreadCount > 0 ? unreadCount : '0' }} unread
        </button>
      </div>

      <div class="notif-mobile__list">
        <div v-if="loading" class="notif-mobile__state">Loading…</div>
        <div v-else-if="notifications.length === 0" class="notif-mobile__state">No notifications</div>

        <TransitionGroup v-else name="notif-list" tag="div">
          <div
            v-for="n in notifications"
            :key="n.id"
            class="notif-mobile__row"
            :class="{ 'notif-mobile__row--unread': !n.is_read }"
            @click="openDetail(n)"
          >
            <div class="notif-mobile__icon" :style="{ background: 'color-mix(in srgb, ' + typeColor(n.type) + ' 18%, transparent)', color: typeColor(n.type) }">
              <span v-if="typeIconName(n.type) === 'bell'">🔔</span>
              <span v-else-if="typeIconName(n.type) === 'cpu'">🤖</span>
              <span v-else-if="typeIconName(n.type) === 'alert'">⚠</span>
              <span v-else>📩</span>
            </div>
            <div class="notif-mobile__content">
              <div class="notif-mobile__title-row">
                <span class="notif-mobile__title" :class="{ 'notif-mobile__title--unread': !n.is_read }">{{ n.title }}</span>
                <span class="notif-mobile__time">{{ relativeTime(n.created_at) }}</span>
              </div>
              <span class="notif-mobile__preview">{{ n.preview || 'No content' }}</span>
            </div>
            <div class="notif-mobile__actions">
              <span v-if="!n.is_read" class="notif-mobile__dot"></span>
              <button class="btn btn-icon btn-ghost notif-mobile__delete" @click.stop="deleteNotification(n.id, $event)" title="Delete">×</button>
            </div>
          </div>
        </TransitionGroup>

        <div v-if="hasMore && !loading" class="notif-mobile__load-more">
          <button class="btn btn-secondary" @click="loadMore" :disabled="loadingMore">
            {{ loadingMore ? 'Loading…' : 'Load more' }}
          </button>
        </div>
      </div>
    </div>

    <!-- ─── Desktop layout ─── -->
    <div v-else class="notif-desktop">
      <!-- Filter rail -->
      <aside class="notif-desktop__rail">
        <div class="mono-label" style="padding: 4px 10px 10px;">FILTERS</div>
        <button
          class="notif-desktop__filter"
          :class="{ 'notif-desktop__filter--active': !unreadOnly && typeFilter === 'all' }"
          @click="unreadOnly = false; typeFilter = 'all'; fetchNotifications(true)"
        >
          <span>All</span>
          <span class="notif-desktop__filter-count">{{ total }}</span>
        </button>
        <button
          class="notif-desktop__filter"
          :class="{ 'notif-desktop__filter--active': unreadOnly }"
          @click="unreadOnly = true; fetchNotifications(true)"
        >
          <span><span class="notif-desktop__filter-dot" style="background: var(--primary);"></span> Unread</span>
          <span class="notif-desktop__filter-count">{{ unreadCount }}</span>
        </button>
        <button
          class="notif-desktop__filter"
          :class="{ 'notif-desktop__filter--active': typeFilter === 'error' }"
          @click="typeFilter = 'error'; fetchNotifications(true)"
        >
          <span><span class="notif-desktop__filter-dot" style="background: var(--danger);"></span> Errors</span>
          <span class="notif-desktop__filter-count">{{ actionableCount }}</span>
        </button>

        <div class="mono-label" style="padding: 16px 10px 10px;">BY TYPE</div>
        <button
          v-for="chip in typeChips.filter(c => c.value !== 'all')"
          :key="chip.value"
          class="notif-desktop__filter"
          :class="{ 'notif-desktop__filter--active': typeFilter === chip.value && !unreadOnly }"
          @click="unreadOnly = false; setTypeFilter(chip.value)"
        >
          <span>{{ chip.label }}</span>
        </button>
      </aside>

      <!-- List -->
      <section class="notif-desktop__list-wrap">
        <div class="notif-desktop__list-head">
          <h2 class="notif-desktop__title">
            Notifications
            <span v-if="unreadCount > 0" class="notif-desktop__unread-pill">{{ unreadCount }} unread</span>
          </h2>
          <button class="btn btn-ghost" @click="markAllRead" :disabled="unreadCount === 0">Mark all read</button>
        </div>

        <div class="notif-desktop__list">
          <div v-if="loading" class="notif-desktop__empty">Loading notifications…</div>

          <div v-else-if="notifications.length === 0" class="notif-desktop__empty">
            <span style="font-size: 28px;">📭</span>
            <span>No notifications yet</span>
            <span class="notif-desktop__empty-sub">They appear when scheduler jobs complete.</span>
          </div>

          <TransitionGroup v-else name="notif-list" tag="div">
            <div
              v-for="n in notifications"
              :key="n.id"
              class="notif-desktop__row"
              :class="{ 'notif-desktop__row--unread': !n.is_read }"
              :style="{ borderLeftColor: !n.is_read ? typeColor(n.type) : 'transparent' }"
              @click="openDetail(n)"
            >
              <span
                class="notif-desktop__icon"
                :style="{ background: 'color-mix(in srgb, ' + typeColor(n.type) + ' 16%, transparent)', color: typeColor(n.type) }"
              >
                <span v-if="typeIconName(n.type) === 'bell'">🔔</span>
                <span v-else-if="typeIconName(n.type) === 'cpu'">🤖</span>
                <span v-else-if="typeIconName(n.type) === 'alert'">⚠</span>
                <span v-else>📩</span>
              </span>

              <div class="notif-desktop__content">
                <div class="notif-desktop__title-row">
                  <span class="notif-desktop__row-title" :class="{ 'notif-desktop__row-title--unread': !n.is_read }">
                    {{ n.title }}
                  </span>
                  <span class="notif-desktop__time">{{ relativeTime(n.created_at) }}</span>
                </div>
                <span class="notif-desktop__preview">{{ n.preview || 'No content' }}</span>
              </div>

              <div class="notif-desktop__actions">
                <span v-if="!n.is_read" class="notif-desktop__dot"></span>
                <button class="btn btn-icon btn-ghost" @click.stop="deleteNotification(n.id, $event)" title="Delete">
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <polyline points="3 6 5 6 21 6"/>
                    <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
                  </svg>
                </button>
              </div>
            </div>
          </TransitionGroup>

          <div v-if="hasMore && !loading" class="notif-desktop__load-more">
            <button class="btn btn-secondary" @click="loadMore" :disabled="loadingMore">
              {{ loadingMore ? 'Loading…' : 'Load more' }}
            </button>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.notif {
  color: var(--text);
  height: 100%;
  min-height: 0;
}

/* ─── Mobile ─── */
.notif-mobile {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
}
.notif-mobile__filters {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 12px;
  background: var(--bg-1);
  border-bottom: 1px solid var(--border);
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
.notif-mobile__chip {
  flex-shrink: 0;
  height: 28px;
  padding: 0 12px;
  border-radius: 999px;
  background: var(--bg-2);
  border: 1px solid var(--border-strong);
  color: var(--text-dim);
  font-family: var(--font-mono);
  font-size: 11px;
  letter-spacing: 0.04em;
  cursor: pointer;
  white-space: nowrap;
}
.notif-mobile__chip--active {
  background: var(--primary);
  border-color: var(--primary);
  color: white;
}
.notif-mobile__unread {
  display: flex;
  align-items: center;
  gap: 5px;
  height: 28px;
  padding: 0 10px;
  border-radius: 999px;
  background: var(--primary-bg);
  border: 0;
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: 10.5px;
  cursor: pointer;
  flex-shrink: 0;
}
.notif-mobile__unread--active { background: var(--primary-bg-strong); color: var(--primary-hover); }
.notif-mobile__unread-dot {
  width: 6px; height: 6px; border-radius: 50%; background: var(--text-muted);
}
.notif-mobile__unread--active .notif-mobile__unread-dot { background: var(--primary); }

.notif-mobile__list {
  flex: 1;
  overflow-y: auto;
}
.notif-mobile__state {
  padding: 50px 20px;
  text-align: center;
  color: var(--text-muted);
  font-size: 13px;
}

.notif-mobile__row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 14px;
  border-bottom: 1px solid var(--border);
  cursor: pointer;
}
.notif-mobile__row:active { background: var(--primary-bg); }
.notif-mobile__row--unread { background: var(--bg-2); }
.notif-mobile__icon {
  width: 32px;
  height: 32px;
  border-radius: var(--r-md);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  flex-shrink: 0;
}
.notif-mobile__content { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 3px; }
.notif-mobile__title-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
}
.notif-mobile__title {
  font-size: 13px;
  color: var(--text-dim);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  flex: 1;
}
.notif-mobile__title--unread { color: var(--text); font-weight: 500; }
.notif-mobile__time {
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-subtle);
  flex-shrink: 0;
}
.notif-mobile__preview {
  font-size: 11.5px;
  color: var(--text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.notif-mobile__actions {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}
.notif-mobile__dot { width: 8px; height: 8px; border-radius: 50%; background: var(--primary); }
.notif-mobile__delete:active { color: var(--danger); }
.notif-mobile__load-more { padding: 16px; display: flex; justify-content: center; }

/* ─── Desktop ─── */
.notif-desktop {
  display: grid;
  grid-template-columns: 220px 1fr;
  gap: 14px;
  height: 100%;
  min-height: 0;
}
.notif-desktop__rail {
  background: var(--bg-1);
  border: 1px solid var(--border);
  border-radius: var(--r-lg);
  padding: 12px;
  overflow-y: auto;
}
.notif-desktop__filter {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding: 8px 10px;
  margin: 1px 0;
  background: transparent;
  border: 0;
  border-left: 2px solid transparent;
  border-radius: var(--r-md);
  color: var(--text-dim);
  font-size: 13px;
  cursor: pointer;
  text-align: left;
}
.notif-desktop__filter:hover { background: var(--bg-2); }
.notif-desktop__filter--active {
  background: var(--primary-bg-strong);
  border-left-color: var(--primary);
  color: var(--text);
  font-weight: 500;
}
.notif-desktop__filter-dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  margin-right: 6px;
  vertical-align: middle;
}
.notif-desktop__filter-count {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-muted);
}

.notif-desktop__list-wrap {
  background: var(--bg-1);
  border: 1px solid var(--border);
  border-radius: var(--r-lg);
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
}
.notif-desktop__list-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px 18px;
  border-bottom: 1px solid var(--border);
}
.notif-desktop__title {
  font-size: 16px;
  font-weight: 600;
  margin: 0;
  display: flex;
  align-items: center;
  gap: 8px;
}
.notif-desktop__unread-pill {
  padding: 2px 10px;
  border-radius: 999px;
  background: var(--primary);
  color: white;
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}

.notif-desktop__list { flex: 1; overflow-y: auto; }

.notif-desktop__empty {
  padding: 60px 20px;
  text-align: center;
  color: var(--text-muted);
  font-size: 13px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  align-items: center;
}
.notif-desktop__empty-sub { font-size: 11px; color: var(--text-subtle); }

.notif-desktop__row {
  display: grid;
  grid-template-columns: 32px 1fr auto;
  align-items: center;
  gap: 12px;
  padding: 12px 18px;
  border-bottom: 1px solid var(--border);
  border-left: 3px solid transparent;
  cursor: pointer;
  transition: background 0.12s var(--ease-out);
}
.notif-desktop__row:hover { background: var(--bg-2); }
.notif-desktop__row--unread { background: rgba(99, 102, 241, 0.04); }

.notif-desktop__icon {
  width: 32px;
  height: 32px;
  border-radius: var(--r-md);
  display: flex;
  align-items: center;
  justify-content: center;
}
.notif-desktop__content { min-width: 0; }
.notif-desktop__title-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 2px;
}
.notif-desktop__row-title {
  font-size: 13px;
  color: var(--text-dim);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.notif-desktop__row-title--unread { color: var(--text); font-weight: 500; }
.notif-desktop__time {
  font-family: var(--font-mono);
  font-size: 10.5px;
  color: var(--text-subtle);
  flex-shrink: 0;
}
.notif-desktop__preview {
  font-size: 12px;
  color: var(--text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  display: block;
}

.notif-desktop__actions { display: flex; align-items: center; gap: 8px; }
.notif-desktop__dot { width: 8px; height: 8px; border-radius: 50%; background: var(--primary); }
.notif-desktop__load-more {
  padding: 14px;
  display: flex;
  justify-content: center;
  border-top: 1px solid var(--border);
}

/* Transitions */
.notif-list-enter-active { transition: all 0.3s var(--ease-out); }
.notif-list-enter-from { transform: translateY(-10px); opacity: 0; }
</style>

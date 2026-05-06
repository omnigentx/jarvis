<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRoute, useRouter, RouterLink } from 'vue-router'
import { useBreakpoint } from '../composables/useBreakpoint'
import { useRealtimeStream } from '../composables/useRealtimeStream'
import { useAgentsStore } from '../stores/agents'
import { useApprovalsStore } from '../stores/approvals'
import { useAudioPlayerStore } from '../stores/audioPlayer'
import { useAudioPlayer } from '../composables/useAudioPlayer'
import { apiFetch, buildSSEUrl } from '../api'
import ConnectionBanner from './ConnectionBanner.vue'
import MiniAudioPlayer from './stories/MiniAudioPlayer.vue'
import FullAudioPlayer from './stories/FullAudioPlayer.vue'

const approvalsStore = useApprovalsStore()
const audioPlayerStore = useAudioPlayerStore()

// Initialize audio engine (singleton — persists across route changes)
useAudioPlayer()

const isAudioPlaying = computed(() => audioPlayerStore.isMiniPlayerVisible)

const store = useAgentsStore()
const { status, reconnect } = useRealtimeStream()
const route = useRoute()
const router = useRouter()
const { isMobile } = useBreakpoint()

// ─── Mobile sidebar ───
const isSidebarOpen = ref(false)
function toggleSidebar() {
  isSidebarOpen.value = !isSidebarOpen.value
}
function closeSidebar() {
  isSidebarOpen.value = false
}
// Auto-close sidebar on route change (mobile)
watch(() => route.path, () => {
  if (isMobile.value) closeSidebar()
})

// ─── Unread notification badge ───
const unreadCount = ref(0)
let notifEventSource = null

async function fetchUnreadCount() {
  try {
    const data = await apiFetch('/api/notifications/unread-count')
    unreadCount.value = data.unread_count || 0
  } catch (_) {}
}

function connectNotifSSE() {
  const url = buildSSEUrl('/api/scheduler/stream')
  notifEventSource = new EventSource(url)
  notifEventSource.onmessage = (ev) => {
    try {
      const data = JSON.parse(ev.data)
      if (data.type === 'new_notification') {
        unreadCount.value += 1
        // Update browser tab title
        document.title = unreadCount.value > 0
          ? `(${unreadCount.value}) ${route.meta.title || 'Dashboard'} — My Jarvis`
          : `${route.meta.title || 'Dashboard'} — My Jarvis`
      }
    } catch (_) {}
  }
  notifEventSource.onerror = () => {
    notifEventSource?.close()
    setTimeout(connectNotifSSE, 5000)
  }
}

// Figma sidebar nav — exact items from node tree
const mainNav = [
  { path: '/overview', label: 'Overview', comingSoon: true },
  { path: '/agents', label: 'Agents' },
  { path: '/skills', label: 'Skills' },
  { path: '/monitor', label: 'Team Monitor' },
  { path: '/chat', label: 'Chat' },
  { path: '/runs', label: 'Runs', comingSoon: true },
  { path: '/scheduler', label: 'Scheduler' },
  { path: '/token-usage', label: 'Token Usage' },
  { path: '/stories', label: 'Stories' },
  { path: '/notifications', label: 'Notifications', hasBadge: true },
  { path: '/telemetry', label: 'Telemetry', comingSoon: true },
]

const secondaryNav = [
  { path: '/alerts', label: 'Alerts', comingSoon: true },
  { path: '/approvals', label: 'Approvals', hasApprovalBadge: true },
  { path: '/audit', label: 'Audit', comingSoon: true },
  { path: '/settings', label: 'Settings' },
]

const visibleMainNav = computed(() => mainNav.filter(i => !i.comingSoon))
const visibleSecondaryNav = computed(() => secondaryNav.filter(i => !i.comingSoon))

// Mobile bottom nav — 5 key tabs
const mobileNav = [
  { path: '/overview',       label: 'Home',     icon: 'home' },
  { path: '/agents',         label: 'Agents',   icon: 'bot' },
  { path: '/chat',           label: 'Chat',     icon: 'message-circle' },
  { path: '/scheduler',      label: 'Schedule', icon: 'calendar' },
  { path: '/notifications',  label: 'Notifs',   icon: 'bell', hasBadge: true },
]

function isActive(item) {
  return route.path.startsWith(item.path) && !item.comingSoon
}

function handleReconnect() {
  // Re-fetch agents then reconnect SSE
  store.fetchAgents()
  reconnect()
}

// Refresh badge when other components signal a change
function onBadgeUpdate() {
  fetchUnreadCount()
}

// Re-fetch on route change (e.g. navigating away from detail page)
watch(() => route.path, () => {
  fetchUnreadCount()
})

// Mobile header back navigation
const isDetailPage = computed(() => !!route.meta?.back)
function goBack() {
  const back = route.meta?.back
  if (back && typeof back === 'string') {
    router.push(back)
  } else {
    router.back()
  }
}

onMounted(() => {
  fetchUnreadCount()
  connectNotifSSE()
  window.addEventListener('notification-badge-update', onBadgeUpdate)
})

onUnmounted(() => {
  notifEventSource?.close()
  window.removeEventListener('notification-badge-update', onBadgeUpdate)
})
</script>

<template>
  <div class="app-layout">
    <!-- Mobile header bar -->
    <header v-if="isMobile" class="mobile-header">
      <!-- Back button (detail pages) -->
      <button v-if="isDetailPage" class="mobile-header__back" @click="goBack">
        <svg viewBox="0 0 24 24" width="20" height="20" fill="none">
          <path d="M19 12H5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
          <polyline points="12 19 5 12 12 5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
        </svg>
      </button>
      <!-- Hamburger (top-level pages) -->
      <button v-else class="mobile-header__hamburger" @click="toggleSidebar">
        <svg viewBox="0 0 24 24" width="22" height="22" fill="none">
          <path d="M3 6h18M3 12h18M3 18h18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
        </svg>
      </button>
      <span class="mobile-header__title">{{ route.meta.title || 'Jarvis' }}</span>
    </header>

    <!-- Sidebar overlay (mobile) -->
    <div v-if="isMobile && isSidebarOpen" class="sidebar-overlay" @click="closeSidebar"></div>

    <!-- Sidebar -->
    <aside
      class="sidebar"
      :class="{ 'sidebar--open': isSidebarOpen, 'sidebar--mobile': isMobile }"
      :style="{
        background: 'var(--bg-sidebar)',
        borderRight: '1px solid var(--border-sidebar)',
      }"
    >
      <!-- Header: y=20, paddingLeft=24, gap=2 -->
      <div style="padding: 20px 0 0 24px;">
        <div style="font-size: 16px; font-weight: 700; color: var(--text-heading); line-height: 19px;">
          Jarvis Ops
        </div>
        <div style="font-size: 11px; font-weight: 400; color: var(--text-sub); margin-top: 2px; line-height: 13px;">
          v1.1.0 · realtime
        </div>
      </div>

      <!-- Divider -->
      <div style="margin: 8px 16px; height: 1px; background: var(--border-sidebar);"></div>

      <!-- Main Nav -->
      <nav class="flex-1" style="padding: 0;">
        <RouterLink
          v-for="item in visibleMainNav"
          :key="item.label"
          :to="item.comingSoon ? '#' : item.path"
          class="flex items-center transition-colors relative"
          :style="{
            height: '38px',
            paddingLeft: '24px',
            paddingRight: '12px',
            fontSize: '13px',
            fontWeight: '400',
            color: isActive(item) ? 'var(--text-heading)' : 'var(--text-nav)',
            background: isActive(item) ? 'rgba(59,130,246,0.08)' : 'transparent',
            cursor: item.comingSoon ? 'default' : 'pointer',
            justifyContent: 'space-between',
          }"
          @click.prevent="item.comingSoon ? null : undefined"
        >
          <!-- Active: left accent bar -->
          <div
            v-if="isActive(item)"
            style="position: absolute; left: 0; top: 0; width: 3px; height: 38px; background: var(--accent-blue); border-radius: 0 2px 2px 0;"
          ></div>
          <span>{{ item.label }}</span>
          <!-- Unread badge for Notifications -->
          <span
            v-if="item.hasBadge && unreadCount > 0"
            class="sidebar-badge"
          >
            {{ unreadCount > 99 ? '99+' : unreadCount }}
          </span>
        </RouterLink>

        <!-- Divider -->
        <div style="margin: 4px 16px; height: 1px; background: var(--border-sidebar);"></div>

        <RouterLink
          v-for="item in visibleSecondaryNav"
          :key="item.label"
          :to="item.comingSoon ? '#' : item.path"
          class="flex items-center transition-colors relative"
          :style="{
            height: '38px',
            paddingLeft: '24px',
            paddingRight: '12px',
            fontSize: '13px',
            fontWeight: '400',
            color: isActive(item) ? 'var(--text-heading)' : 'var(--text-nav)',
            background: isActive(item) ? 'rgba(59,130,246,0.08)' : 'transparent',
            cursor: item.comingSoon ? 'default' : 'pointer',
            justifyContent: 'space-between',
          }"
          @click.prevent="item.comingSoon ? null : undefined"
        >
          <div
            v-if="isActive(item)"
            style="position: absolute; left: 0; top: 0; width: 3px; height: 38px; background: var(--accent-blue); border-radius: 0 2px 2px 0;"
          ></div>
          <span>{{ item.label }}</span>
          <span
            v-if="item.hasApprovalBadge && approvalsStore.pendingCount > 0"
            class="sidebar-badge" style="background: #f59e0b;"
          >
            {{ approvalsStore.pendingCount > 99 ? '99+' : approvalsStore.pendingCount }}
          </span>
        </RouterLink>
      </nav>
    </aside>

    <!-- Main Content -->
    <main class="app-main" :class="{ 'app-main--mobile': isMobile }" :style="{ background: 'var(--bg-base)' }">
      <ConnectionBanner :status="status" @reconnect="handleReconnect" />
      <div
        class="app-main__content"
        :style="{ paddingBottom: isAudioPlaying ? '84px' : (isMobile ? '16px' : '24px') }"
      >
        <RouterView />
      </div>
      <MiniAudioPlayer v-if="isAudioPlaying" />
    </main>

  </div>
  <FullAudioPlayer />
</template>

<style scoped>
/* Layout */
.app-layout {
  display: flex;
  height: 100vh;
  overflow: hidden;
}

/* Sidebar */
.sidebar {
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  overflow-y: auto;
  width: 180px;
}
.sidebar--mobile {
  position: fixed;
  top: 0;
  left: 0;
  bottom: 0;
  z-index: 300;
  width: 220px;
  transform: translateX(-100%);
  transition: transform 0.25s ease;
}
.sidebar--mobile.sidebar--open {
  transform: translateX(0);
}
.sidebar-overlay {
  position: fixed;
  inset: 0;
  z-index: 250;
  background: rgba(0,0,0,0.5);
}

/* Mobile header */
.mobile-header {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  z-index: 200;
  display: flex;
  align-items: center;
  gap: 12px;
  height: 52px;
  padding: 0 16px;
  background: var(--bg-sidebar, #0c0e15);
  border-bottom: 1px solid var(--border-primary, #1a1d2e);
}
.mobile-header__hamburger {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  border: none;
  background: transparent;
  color: var(--text-secondary, #c4c8d4);
  border-radius: 8px;
  cursor: pointer;
}
.mobile-header__hamburger:active {
  background: rgba(255,255,255,0.06);
}
/* Back button — same size as hamburger, shown on detail pages */
.mobile-header__back {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  border: none;
  background: transparent;
  color: var(--text-secondary, #c4c8d4);
  border-radius: 8px;
  cursor: pointer;
}
.mobile-header__back:active {
  background: rgba(255,255,255,0.06);
  color: var(--accent-blue, #3b82f6);
}
.mobile-header__title {
  font-size: 15px;
  font-weight: 600;
  color: var(--text-heading, #f0f2f5);
}

/* Main content */
.app-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.app-main--mobile {
  padding-top: 52px; /* space for mobile header */
}
.app-main__content {
  flex: 1;
  overflow-y: auto;
  padding: 24px 36px;
}
.app-main--mobile .app-main__content {
  padding: 16px;
}

/* Badge */
.sidebar-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  font-size: 10px;
  font-weight: 700;
  color: #fff;
  background: #ef4444;
  border-radius: 9px;
  line-height: 1;
}
</style>

<script setup>
/**
 * AppLayout — redesigned chrome (sidebar 232px + topbar + Cmd+K).
 *
 * Logic preserved verbatim from the previous AppLayout:
 *   • realtime SSE status + reconnect
 *   • audio player singleton (useAudioPlayer)
 *   • notification unread count via SSE
 *   • MCP warn-toast forwarder
 *   • mobile drawer + bottom nav
 *
 * What changed:
 *   • Sidebar widened to 232px and grouped into four sections
 *     (WORKSPACE / OPERATIONS / LIBRARY / SYSTEM) per redesign/chrome.jsx.
 *   • Top bar (56px) with lang/theme toggles + Cmd+K trigger; replaces
 *     the bare per-page header.
 *   • Persisted prefs in localStorage:
 *       - `jarvis_lang`  : 'vi' | 'en'
 *       - `jarvis_theme` : 'dark' | 'light' (mirrored to <html data-theme>)
 *   • CmdK palette mounted here so any route can pop it via ⌘K.
 */
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRoute, useRouter, RouterLink } from 'vue-router'
import { useBreakpoint } from '../composables/useBreakpoint'
import { useFabVisibility } from '../composables/useFabVisibility'
import { useLang } from '../composables/useLang'
import { useRealtimeStream } from '../composables/useRealtimeStream'
import { useSSEConnection } from '../composables/useSSEConnection.js'
import { useAgentsStore } from '../stores/agents'
import { useApprovalsStore } from '../stores/approvals'
import { useAudioPlayerStore } from '../stores/audioPlayer'
import { useAudioPlayer } from '../composables/useAudioPlayer'
import { apiFetch, buildSSEUrl } from '../api'
import { useToast } from '../composables/useToast'
import ConnectionBanner from './ConnectionBanner.vue'
import MiniAudioPlayer from './stories/MiniAudioPlayer.vue'
import FullAudioPlayer from './stories/FullAudioPlayer.vue'
import GlobalVoiceIndicator from './global/GlobalVoiceIndicator.vue'
import FloatingChatDock from './global/FloatingChatDock.vue'
import CmdK from './CmdK.vue'
import VoiceFAB from './VoiceFAB.vue'

const approvalsStore = useApprovalsStore()
const audioPlayerStore = useAudioPlayerStore()

// Initialize audio engine (singleton — persists across route changes)
useAudioPlayer()

const isAudioPlaying = computed(() => audioPlayerStore.isMiniPlayerVisible)

const store = useAgentsStore()
const { status, reconnect } = useRealtimeStream({
  // Context-compaction lifecycle → non-blocking toasts. The store case
  // in agents.js owns the per-agent state; this only surfaces visible
  // feedback so the user knows compaction happened (and saved tokens)
  // without being interrupted mid-chat.
  onEvent(event) {
    if (event.event_type === 'context_compaction_completed') {
      const saved = event.data?.saved_tokens || 0
      const pct = Math.round((event.data?.reduction_ratio || 0) * 100)
      toast.success(
        lang.value === 'vi'
          ? `Đã nén ngữ cảnh: ${event.agent_name}`
          : `Context compacted: ${event.agent_name}`,
        {
          description: lang.value === 'vi'
            ? `Tiết kiệm ~${saved.toLocaleString()} tokens (${pct}%)`
            : `Saved ~${saved.toLocaleString()} tokens (${pct}%)`,
        },
      )
    } else if (event.event_type === 'context_compaction_failed') {
      toast.warning(
        lang.value === 'vi'
          ? `Nén ngữ cảnh thất bại: ${event.agent_name}`
          : `Context compaction failed: ${event.agent_name}`,
        {
          description: lang.value === 'vi'
            ? 'Agent tiếp tục với ngữ cảnh gốc. Xem tab Context Versions để biết chi tiết.'
            : 'Agent continues on the raw context. See the Context Versions tab for details.',
        },
      )
    }
  },
})
const route = useRoute()
const router = useRouter()
const { isMobile } = useBreakpoint()
// Quick hide/show for the floating chat + voice FABs (mobile). manualHidden is
// sticky (persisted) and wins over scroll-based auto-hide — see useFabVisibility.
const { manualHidden: fabsManualHidden, toggleManual: toggleFabs } = useFabVisibility()
// Only show the grip where a FAB actually renders. The FABs are hidden on /chat
// (inline composer already there) and on bare layouts (/login, /setup) — a grip
// there would toggle nothing. Mirror that so it never becomes an orphan control.
const showFabGrip = computed(() =>
  isMobile.value && route.name !== 'Chat' && route.meta?.layout !== 'bare',
)

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

async function fetchUnreadCount() {
  try {
    const data = await apiFetch('/api/notifications/unread-count')
    unreadCount.value = data.unread_count || 0
  } catch (_) {}
}

useSSEConnection(buildSSEUrl('/api/scheduler/stream'), {
  onMessage(ev) {
    try {
      const data = JSON.parse(ev.data)
      if (data.type === 'new_notification') {
        unreadCount.value += 1
        document.title = unreadCount.value > 0
          ? `(${unreadCount.value}) ${route.meta.title || 'Dashboard'} — Jarvis`
          : `${route.meta.title || 'Dashboard'} — Jarvis`
      }
    } catch (_) {}
  },
})

// ─── MCP warning toasts ───
const toast = useToast()
useSSEConnection(buildSSEUrl('/api/mcp/events/stream'), {
  onMessage(ev) {
    try {
      const data = JSON.parse(ev.data)
      if (data.type !== 'mcp' || data.action !== 'warn') return
      const detail = data.detail || {}
      const category = detail.category || 'warn'
      const summary = (detail.hits || [])
        .map((h) => h.why)
        .filter(Boolean)
        .join('; ') || category
      toast.warning(`MCP warning — ${data.server || 'unknown'}`, {
        description: summary,
        duration: 8000,
      })
    } catch (_) {}
  },
})

// ─── Nav (grouped per redesign/chrome.jsx) ───
// Each item resolves to a router-link path declared in router.js.
// `title`/`label` hold i18n KEYS resolved with t() in the template, so the
// nav re-renders reactively on a language toggle (a const baked with t()
// values at setup time would not).
const navSections = [
  {
    title: 'nav.secWorkspace',
    items: [
      { path: '/chat',         label: 'nav.chat',         icon: 'chat' },
      { path: '/agents',       label: 'nav.agents',       icon: 'agents' },
      { path: '/monitor',      label: 'nav.monitor',      icon: 'monitor' },
      { path: '/meetings',     label: 'nav.meeting',      icon: 'meeting' },
    ],
  },
  {
    title: 'nav.secOperations',
    items: [
      { path: '/scheduler',    label: 'nav.scheduler',    icon: 'cron' },
      { path: '/approvals',    label: 'nav.approvals',    icon: 'approval', hasApprovalBadge: true },
      { path: '/notifications', label: 'nav.notifications', icon: 'bell', hasBadge: true },
      { path: '/token-usage',  label: 'nav.tokenUsage',   icon: 'usage' },
    ],
  },
  {
    title: 'nav.secLibrary',
    items: [
      { path: '/stories',      label: 'nav.stories',      icon: 'book' },
      { path: '/skills',       label: 'nav.skills',       icon: 'spark' },
      { path: '/mcp-servers',  label: 'nav.mcpServers',   icon: 'plug' },
    ],
  },
  {
    title: 'nav.secSystem',
    items: [
      { path: '/settings',     label: 'nav.settings',     icon: 'settings' },
    ],
  },
]

// SVG path map mirrored from redesign/shared.jsx Icon set so the chrome
// matches without pulling in lucide-vue (no new top-level dependency).
const ICON_PATHS = {
  chat:     'M21 12a8 8 0 0 1-11.6 7.2L4 21l1.8-5.4A8 8 0 1 1 21 12z',
  agents:   'M4 21v-2a4 4 0 0 1 4-4h2M14 15h2a4 4 0 0 1 4 4v2M9 7a3 3 0 1 0 6 0 3 3 0 0 0-6 0M3 14a2.5 2.5 0 1 0 5 0 2.5 2.5 0 0 0-5 0M16 14a2.5 2.5 0 1 0 5 0 2.5 2.5 0 0 0-5 0',
  monitor:  'M4 5h16v11H4zM4 16l4 4h8l4-4M9 9h6M9 12h4',
  meeting:  'M7 8a5 5 0 1 1 10 0 5 5 0 0 1-10 0zM3 21v-1a5 5 0 0 1 5-5h8a5 5 0 0 1 5 5v1',
  cron:     'M12 7v5l3 2M12 21a9 9 0 1 1 0-18 9 9 0 0 1 0 18z',
  approval: 'M5 12l5 5L20 7',
  bell:     'M6 8a6 6 0 1 1 12 0v5l2 3H4l2-3V8zM9 19a3 3 0 0 0 6 0',
  usage:    'M3 20h18M6 16V8M11 16V4M16 16v-7M21 16v-3',
  book:     'M4 19V4l8 2 8-2v15l-8 2-8-2zM12 6v15',
  spark:    'M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3z',
  plug:     'M9 7V3M15 7V3M6 7h12v3a6 6 0 1 1-12 0V7zM12 19v2',
  settings: 'M12 9.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5zM19.5 12l1.6-1-1-2.7-2 .4a7 7 0 0 0-1.5-.9L16 6h-3l-.6 1.8a7 7 0 0 0-1.5.9l-2-.4-1 2.7 1.6 1-1.6 1 1 2.7 2-.4q.7.6 1.5.9L13 18h3l.6-1.8q.8-.3 1.5-.9l2 .4 1-2.7-1.6-1z',
  search:   'M11 4a7 7 0 1 1 0 14 7 7 0 0 1 0-14zM21 21l-5-5',
  globe:    'M3 12a9 9 0 1 0 18 0 9 9 0 0 0-18 0zM3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18',
  sun:      'M12 4v2M12 18v2M4 12H2M22 12h-2M5.6 5.6L4.2 4.2M19.8 19.8l-1.4-1.4M5.6 18.4l-1.4 1.4M19.8 4.2l-1.4 1.4M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8z',
  moon:     'M21 13a9 9 0 1 1-10-10 7 7 0 0 0 10 10z',
  menu:     'M4 7h16M4 12h16M4 17h16',
  arrowLeft: 'M19 12H5M12 19l-7-7 7-7',
}

// Mobile bottom tab bar — 5 primary tabs per Mobile.html design.
// "More" opens the drawer (overflow nav: Scheduler / Approvals / Stories /
// MCP / Token usage / Notifications). Drawer stays as overflow surface
// so the bottom bar only carries the high-frequency destinations.
const mobileNav = [
  { path: '/chat',     label: 'nav.chat',         icon: 'chat' },
  { path: '/agents',   label: 'nav.agents',       icon: 'agents' },
  { path: '/monitor',  label: 'nav.monitorShort', icon: 'monitor' },
  { path: '/settings', label: 'nav.settings',     icon: 'settings' },
  { kind: 'more',      label: 'nav.more',         icon: 'menu' },
]

function onMobileTabClick(item) {
  if (item.kind === 'more') {
    toggleSidebar()
    return
  }
  if (route.path.startsWith(item.path)) return
  router.push(item.path)
}

function isMobileTabActive(item) {
  if (item.kind === 'more') return isSidebarOpen.value
  return route.path.startsWith(item.path)
}

function isActive(item) {
  return route.path.startsWith(item.path)
}

function handleReconnect() {
  store.fetchAgents()
  reconnect()
}

function onBadgeUpdate() {
  fetchUnreadCount()
}

watch(() => route.path, () => {
  fetchUnreadCount()
})

// Mobile header back nav
const isDetailPage = computed(() => !!route.meta?.back)
function goBack() {
  const back = route.meta?.back
  if (back && typeof back === 'string') {
    router.push(back)
  } else {
    router.back()
  }
}

// Routes whose own view supplies a header — Chat has ChatHeader,
// Monitor has its own team-header. Stacking the global mobile-header
// on top of those duplicates the search/title/menu and steals ~50px
// of vertical chrome above the actual content.
const ROUTES_WITHOUT_MOBILE_HEADER = ['/chat']
const showMobileHeader = computed(() => {
  if (!isMobile.value) return false
  return !ROUTES_WITHOUT_MOBILE_HEADER.some((p) => route.path.startsWith(p))
})

// ─── Lang / theme persisted prefs ───
// lang lives in the shared useLang composable so bilingual copy elsewhere
// (ChatView crawl banner, …) reacts to the topbar toggle live.
const { lang, toggleLang, t } = useLang()
const theme = ref(localStorage.getItem('jarvis_theme') || 'dark')

function applyTheme(t) {
  document.documentElement.setAttribute('data-theme', t)
}
applyTheme(theme.value)
function toggleTheme() {
  theme.value = theme.value === 'dark' ? 'light' : 'dark'
  localStorage.setItem('jarvis_theme', theme.value)
  applyTheme(theme.value)
}

// ─── Cmd+K palette ───
const cmdkOpen = ref(false)
function openCmdK() { cmdkOpen.value = true }
function closeCmdK() { cmdkOpen.value = false }

function onGlobalKeydown(e) {
  // ⌘K / Ctrl+K — toggle palette. Honour the same browser-shortcut
  // override the design calls out: only fire when no text-input is
  // focused with a modifier? No — ⌘K with modifier is unambiguous,
  // let it through regardless of focus.
  if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
    e.preventDefault()
    cmdkOpen.value = !cmdkOpen.value
  }
}

// Lock body scroll while the mobile drawer is open. Without this the
// page underneath keeps scrolling, dragging the drawer offset off-screen
// on long pages (and on iOS Safari the URL bar collapses/expands during
// the drag, which jitters the drawer).
watch(isSidebarOpen, (open) => {
  document.body.classList.toggle('no-scroll', !!open && isMobile.value)
})

// Surface the mini-player height to CSS so sticky composers / FABs /
// the bottom tab bar can offset upward when audio is playing. Single
// source of truth — every consumer reads var(--mini-player-h).
watch(isAudioPlaying, (playing) => {
  document.documentElement.style.setProperty('--mini-player-h', playing ? '64px' : '0px')
})

onMounted(() => {
  fetchUnreadCount()
  window.addEventListener('notification-badge-update', onBadgeUpdate)
  window.addEventListener('keydown', onGlobalKeydown)
})

onUnmounted(() => {
  window.removeEventListener('notification-badge-update', onBadgeUpdate)
  window.removeEventListener('keydown', onGlobalKeydown)
  document.body.classList.remove('no-scroll')
})

// Breadcrumb label for the topbar — derived from the active leaf.
const currentSection = computed(() => {
  for (const sec of navSections) {
    for (const it of sec.items) {
      if (route.path.startsWith(it.path)) return { section: sec.title, label: it.label }
    }
  }
  return { section: 'nav.secWorkspace', label: route.meta?.title || 'Dashboard' }
})

const searchPlaceholder = computed(() => t('nav.searchPlaceholder'))
</script>

<template>
  <div class="app-layout jv">
    <!-- Mobile header bar.
         The bottom tab "More" already opens the drawer, so a second
         hamburger up here is redundant. Only render the back arrow on
         detail pages; otherwise the title sits flush-left and Search
         stays on the right.
         Hidden on routes that supply their own header (e.g. /chat ships
         ChatHeader with hamburger + agent + actions — stacking the
         global bar on top duplicates ~50px of vertical chrome). -->
    <header v-if="showMobileHeader" class="mobile-header">
      <button v-if="isDetailPage" class="mobile-header__btn" @click="goBack" aria-label="Back">
        <svg viewBox="0 0 24 24" width="20" height="20" fill="none">
          <path :d="ICON_PATHS.arrowLeft" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </button>
      <span class="mobile-header__title">{{ route.meta.title || 'Jarvis' }}</span>
      <button class="mobile-header__btn" @click="openCmdK" aria-label="Search">
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none">
          <path :d="ICON_PATHS.search" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </button>
    </header>

    <!-- Sidebar overlay (mobile) -->
    <div v-if="isMobile && isSidebarOpen" class="sidebar-overlay" @click="closeSidebar"></div>

    <!-- Sidebar -->
    <aside
      class="sidebar"
      :class="{ 'sidebar--open': isSidebarOpen, 'sidebar--mobile': isMobile }"
    >
      <!-- Brand -->
      <div class="brand">
        <div class="brand__mark">J</div>
        <div class="brand__text">
          <div class="brand__name">Jarvis</div>
          <div class="brand__sub mono-label">OMNIGENTX · v0.9</div>
        </div>
      </div>

      <!-- Nav sections -->
      <nav class="nav">
        <div v-for="sec in navSections" :key="sec.title" class="nav-section">
          <div class="nav-section__title mono-label">{{ t(sec.title) }}</div>
          <RouterLink
            v-for="item in sec.items"
            :key="item.label"
            :to="item.path"
            class="nav-item"
            :class="{ 'nav-item--active': isActive(item) }"
          >
            <svg class="nav-item__icon" viewBox="0 0 24 24" width="16" height="16" fill="none">
              <path :d="ICON_PATHS[item.icon]" stroke="currentColor"
                :stroke-width="isActive(item) ? 1.8 : 1.6"
                stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
            <span class="nav-item__label">{{ t(item.label) }}</span>
            <span
              v-if="item.hasBadge && unreadCount > 0"
              class="nav-item__badge"
            >{{ unreadCount > 99 ? '99+' : unreadCount }}</span>
            <span
              v-else-if="item.hasApprovalBadge && approvalsStore.pendingCount > 0"
              class="nav-item__badge nav-item__badge--warn"
            >{{ approvalsStore.pendingCount > 99 ? '99+' : approvalsStore.pendingCount }}</span>
          </RouterLink>
        </div>
      </nav>

      <!-- Drawer footer (mobile only) — lang/theme toggles relocated
           here since the desktop topbar is hidden on mobile. -->
      <div v-if="isMobile" class="drawer-footer">
        <button class="drawer-footer__btn" @click="toggleLang">
          <span class="topbar__lang">{{ lang === 'vi' ? 'Vi' : 'En' }}</span>
          <span class="drawer-footer__label">{{ t('nav.language') }}</span>
        </button>
        <button class="drawer-footer__btn" @click="toggleTheme">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none">
            <path :d="theme === 'dark' ? ICON_PATHS.moon : ICON_PATHS.sun"
              stroke="currentColor" stroke-width="1.6"
              stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
          <span class="drawer-footer__label">{{ theme === 'dark' ? t('nav.themeDark') : t('nav.themeLight') }}</span>
        </button>
      </div>
    </aside>

    <!-- Main Content -->
    <main
      class="app-main"
      :class="{
        'app-main--mobile': isMobile,
        'app-main--no-header': isMobile && !showMobileHeader,
      }"
    >
      <!-- Top bar (desktop only) -->
      <header v-if="!isMobile" class="topbar">
        <div class="topbar__crumb">
          <span class="mono-label topbar__crumb-section">{{ t(currentSection.section) }}</span>
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" class="topbar__crumb-sep">
            <path d="M9 6l6 6-6 6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
          <span class="topbar__crumb-leaf">{{ t(currentSection.label) }}</span>
        </div>

        <button class="topbar__search" @click="openCmdK">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none">
            <path :d="ICON_PATHS.search" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
          <span class="topbar__search-text">{{ searchPlaceholder }}</span>
          <kbd class="topbar__kbd">⌘K</kbd>
        </button>

        <div class="topbar__actions">
          <button class="topbar__icon-btn" :title="`Language: ${lang.toUpperCase()}`" @click="toggleLang">
            <span class="topbar__lang">{{ lang === 'vi' ? 'Vi' : 'En' }}</span>
          </button>
          <button class="topbar__icon-btn" :title="`Theme: ${theme}`" @click="toggleTheme">
            <svg viewBox="0 0 24 24" width="15" height="15" fill="none">
              <path :d="theme === 'dark' ? ICON_PATHS.moon : ICON_PATHS.sun"
                stroke="currentColor" stroke-width="1.6"
                stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </button>
        </div>
      </header>

      <ConnectionBanner :status="status" @reconnect="handleReconnect" />

      <div class="app-main__content">
        <!--
          ``<keep-alive>`` caches view component instances across route nav.
          Without it, switching from /chat to /monitor and back unmounts
          ChatView (and VoiceBar inside it) — losing in-flight conversation
          state, killing the active SSE stream, and stopping hands-free
          voice mid-turn.
        -->
        <RouterView v-slot="{ Component }">
          <keep-alive :include="['Chat', 'TeamMonitor']">
            <component :is="Component" />
          </keep-alive>
        </RouterView>
      </div>
      <MiniAudioPlayer v-if="isAudioPlaying" />
    </main>

    <!-- Bottom tab bar (mobile only) — high-frequency destinations.
         Drawer (More tab) carries overflow nav. -->
    <nav v-if="isMobile" class="mobile-tabbar" aria-label="Primary">
      <button
        v-for="item in mobileNav"
        :key="item.label"
        class="mobile-tab"
        :class="{ 'mobile-tab--active': isMobileTabActive(item) }"
        @click="onMobileTabClick(item)"
      >
        <span class="mobile-tab__indicator" />
        <svg class="mobile-tab__icon" viewBox="0 0 24 24" width="20" height="20" fill="none">
          <path :d="ICON_PATHS[item.icon]" stroke="currentColor"
            :stroke-width="isMobileTabActive(item) ? 2 : 1.6"
            stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        <span class="mobile-tab__label">{{ t(item.label) }}</span>
      </button>
    </nav>

  </div>
  <FullAudioPlayer />

  <!-- Global overlays (live outside the layout flex). -->
  <GlobalVoiceIndicator />
  <FloatingChatDock />

  <!-- Cmd+K palette (global) -->
  <CmdK :open="cmdkOpen" @close="closeCmdK" />

  <!-- Voice FAB (mobile-only, self-gates by route) -->
  <VoiceFAB />

  <!-- FAB grip (mobile-only): one-tap hide/show for the chat + voice FABs.
       Always visible so it works even on screens with no scroll, and is the
       only way back once the user has manually hidden them. -->
  <button
    v-if="showFabGrip"
    class="fab-grip"
    type="button"
    @click="toggleFabs"
    :aria-label="fabsManualHidden ? 'Show chat and voice buttons' : 'Hide chat and voice buttons'"
    :title="fabsManualHidden ? 'Show chat / voice' : 'Hide chat / voice'"
  >
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path :d="fabsManualHidden ? 'M6 15l6-6 6 6' : 'M6 9l6 6 6-6'"
            stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />
    </svg>
  </button>
</template>

<style scoped>
/* Layout */
.app-layout {
  display: flex;
  /* 100dvh excludes iOS Safari's collapsing URL bar — content stays
     reachable as the bar shows/hides. Fallback to 100vh on browsers
     without dvh support. */
  height: 100vh;
  height: 100dvh;
  overflow: hidden;
  background: var(--bg-0);
  color: var(--text);
}

/* ─── Sidebar ─── */
.sidebar {
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  overflow-y: auto;
  width: 232px;
  background: var(--bg-1);
  border-right: 1px solid var(--border);
}
.sidebar--mobile {
  position: fixed;
  top: 0;
  left: 0;
  bottom: 0;
  z-index: 300;
  width: 240px;
  /* Make drawer itself scrollable on short viewports — otherwise the
     bottom of the nav (and the new drawer-footer) is unreachable. */
  overflow-y: auto;
  padding-top: max(0px, var(--safe-top));
  padding-bottom: max(12px, var(--safe-bottom));
  transform: translateX(-100%);
  transition: transform 0.25s var(--ease-out);
}
.sidebar--mobile.sidebar--open {
  transform: translateX(0);
}
.sidebar-overlay {
  position: fixed;
  inset: 0;
  z-index: 250;
  background: rgba(0,0,0,0.5);
  /* Fade the overlay alongside the drawer slide so they read as one
     gesture. Without this the dim pops in/out abruptly. */
  animation: overlayFade 0.25s var(--ease-out);
}
@keyframes overlayFade {
  from { opacity: 0; }
  to   { opacity: 1; }
}

/* Drawer footer (mobile-only) — surfaces lang/theme toggles that
   the desktop topbar normally carries (topbar is hidden on mobile). */
.drawer-footer {
  margin-top: auto;
  padding: 12px 12px;
  border-top: 1px solid var(--border);
  display: flex;
  gap: 8px;
}
.drawer-footer__btn {
  flex: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  height: 40px;
  border-radius: var(--r-md);
  background: var(--bg-2);
  border: 1px solid var(--border-strong);
  color: var(--text-dim);
  font-size: 12.5px;
}
.drawer-footer__btn:active { background: var(--bg-3); color: var(--text); }
.drawer-footer__label { font-weight: 500; }

.brand {
  height: 56px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 16px;
  border-bottom: 1px solid var(--border);
}
.brand__mark {
  width: 28px;
  height: 28px;
  border-radius: 8px;
  background: linear-gradient(135deg, var(--primary), var(--accent));
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-family: var(--font-display);
  font-weight: 700;
  font-size: 14px;
}
.brand__name {
  font-family: var(--font-display);
  font-size: 15px;
  font-weight: 600;
  letter-spacing: -0.01em;
  color: var(--text);
}
.brand__sub {
  font-size: 9px;
  color: var(--text-subtle);
  margin-top: 1px;
}

.nav {
  flex: 1;
  overflow-y: auto;
  padding: 14px 8px 12px;
}
.nav-section { margin-bottom: 14px; }
.nav-section__title {
  padding: 6px 10px 4px;
  font-size: 9.5px;
  letter-spacing: 0.16em;
  color: var(--text-subtle);
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  margin: 1px 0;
  border-radius: var(--r-md);
  color: var(--text-dim);
  background: transparent;
  border-left: 2px solid transparent;
  font-size: 13px;
  font-weight: 400;
  text-decoration: none;
  transition: background 0.15s var(--ease-out), color 0.15s var(--ease-out);
  position: relative;
}
.nav-item:hover {
  color: var(--text);
  background: var(--bg-2);
}
.nav-item--active {
  color: var(--text);
  background: var(--primary-bg-strong);
  border-left-color: var(--primary);
  font-weight: 500;
}
.nav-item__icon { flex-shrink: 0; }
.nav-item__label { flex: 1; }
.nav-item__badge {
  font-family: var(--font-mono);
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 999px;
  background: var(--danger);
  color: #fff;
  line-height: 1.4;
  font-weight: 600;
}
.nav-item__badge--warn { background: var(--warning); color: #1a1300; }

/* ─── Mobile header ─── */
.mobile-header {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  z-index: 200;
  display: flex;
  align-items: center;
  gap: 12px;
  /* Add safe-area-top so the bar clears the iOS notch / dynamic
     island. Height accounts for both the 52px content area and the
     extra inset on devices that have it (no inset = +0). */
  height: calc(var(--mobile-header-h) + var(--safe-top));
  padding: var(--safe-top) 12px 0;
  background: var(--bg-1);
  border-bottom: 1px solid var(--border);
}
.mobile-header__btn {
  display: flex;
  align-items: center;
  justify-content: center;
  /* iOS HIG / WCAG touch target: 40px minimum, 44px ideal. */
  width: 40px;
  height: 40px;
  border: none;
  background: transparent;
  color: var(--text-dim);
  border-radius: var(--r-md);
}
.mobile-header__btn:active { background: var(--bg-3); color: var(--text); }

/* FAB grip — slim handle centered just above the tab bar (mobile). Tap to
   hide/show the chat + voice FABs. Small + translucent so it barely covers
   anything, yet always reachable (incl. on screens with no scroll). */
.fab-grip {
  position: fixed;
  left: 50%;
  transform: translateX(-50%);
  bottom: calc(var(--mobile-tabbar-h) + var(--safe-bottom) + var(--mini-player-h, 0px) + 6px);
  z-index: 196; /* above the FABs (195) */
  width: 46px;
  height: 20px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--border-strong, rgba(120,120,140,0.25));
  border-radius: 10px;
  background: color-mix(in srgb, var(--bg-2) 70%, transparent);
  -webkit-backdrop-filter: blur(8px);
  backdrop-filter: blur(8px);
  color: var(--text-muted);
  cursor: pointer;
  box-shadow: 0 2px 8px rgba(0,0,0,0.12);
  transition: color 0.15s var(--ease-out), background 0.15s var(--ease-out);
}
.fab-grip:active { color: var(--text); background: var(--bg-3); }
.mobile-header__title {
  flex: 1;
  font-size: 15px;
  font-weight: 600;
  color: var(--text);
  font-family: var(--font-display);
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ─── Mobile bottom tab bar ─── */
.mobile-tabbar {
  position: fixed;
  left: 0;
  right: 0;
  /* Sit above the mini audio player when it's visible. --mini-player-h
     is set to 64px by the script when audio is playing, 0 otherwise. */
  bottom: var(--mini-player-h, 0px);
  z-index: 180;
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  height: calc(var(--mobile-tabbar-h) + var(--safe-bottom));
  padding-bottom: var(--safe-bottom);
  background: var(--bg-1);
  border-top: 1px solid var(--border);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
}
.mobile-tab {
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 3px;
  height: 100%;
  background: transparent;
  border: 0;
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.04em;
  transition: color 0.15s var(--ease-out);
}
.mobile-tab:active { background: var(--bg-2); }
.mobile-tab--active { color: var(--primary-hover); }
.mobile-tab__indicator {
  position: absolute;
  top: 0;
  left: 30%;
  right: 30%;
  height: 2px;
  background: var(--primary);
  border-radius: 0 0 2px 2px;
  opacity: 0;
  transition: opacity 0.15s var(--ease-out);
}
.mobile-tab--active .mobile-tab__indicator { opacity: 1; }
.mobile-tab__icon { flex-shrink: 0; }
.mobile-tab__label { line-height: 1; }

/* ─── Top bar ─── */
.app-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg-0);
  min-width: 0;
}
.app-main--mobile {
  /* Header height already includes safe-area-top, so this offset
     keeps mobile content below it. */
  padding-top: calc(var(--mobile-header-h) + var(--safe-top));
}
/* When the global mobile-header is hidden (e.g. /chat where ChatHeader
   is the real header), only reserve safe-area-top so content sits
   below the iOS notch but doesn't get pushed down by a phantom 52px. */
.app-main--no-header {
  padding-top: var(--safe-top);
}

.topbar {
  height: 56px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 0 20px;
  background: var(--bg-0);
  border-bottom: 1px solid var(--border);
}
.topbar__crumb {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.topbar__crumb-section { font-size: 10px; }
.topbar__crumb-sep { color: var(--text-muted); }
.topbar__crumb-leaf { font-size: 13px; font-weight: 500; color: var(--text); }

.topbar__search {
  flex: 1;
  max-width: 420px;
  margin-left: auto;
  height: 32px;
  padding: 0 12px;
  background: var(--bg-2);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--text-muted);
  font-size: 12.5px;
  cursor: pointer;
  transition: border-color 0.15s var(--ease-out), background 0.15s var(--ease-out);
}
.topbar__search:hover { border-color: var(--border-bright); background: var(--bg-3); }
.topbar__search-text { flex: 1; text-align: left; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.topbar__kbd {
  margin-left: auto;
  padding: 2px 6px;
  border-radius: 5px;
  background: var(--bg-3);
  border: 1px solid var(--border-strong);
  font-family: var(--font-mono);
  font-size: 10.5px;
  color: var(--text-dim);
}

.topbar__actions { display: flex; align-items: center; gap: 6px; }
.topbar__icon-btn {
  width: 32px;
  height: 32px;
  border-radius: var(--r-md);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--text-dim);
  transition: color 0.15s var(--ease-out), background 0.15s var(--ease-out);
}
.topbar__icon-btn:hover { color: var(--text); background: var(--bg-3); }
.topbar__lang {
  font-family: var(--font-mono);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.04em;
}

/* ─── Main content ─── */
.app-main__content {
  flex: 1;
  overflow-y: auto;
  /* Inline style on the template adds dynamic padding-bottom for the
     mini audio player. CSS owns the static padding here. */
  padding: 24px 36px;
  padding-bottom: 24px;
}
.app-main--mobile .app-main__content {
  /* Bottom padding clears: tabbar (56px) + safe-area + mini player + the
     FLOATING FAB band (≈52px button + 12px offset) that sits just above the
     tab bar. Without the +64 the last content (e.g. a short page's actions)
     stayed underneath the chat/voice FABs with no way to reach it. */
  padding: 16px;
  padding-bottom: calc(var(--mobile-tabbar-h) + var(--mini-player-h, 0px) + max(16px, var(--safe-bottom)) + var(--mobile-fab-band));
}
</style>

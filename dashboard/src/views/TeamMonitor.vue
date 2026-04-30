<script setup>
import { ref, computed, onMounted, onUnmounted, onBeforeUpdate, onUpdated } from 'vue'
import { useRouter } from 'vue-router'
import { useActivityStream, formatTimestamp } from '../composables/useActivityStream'
import { apiFetch } from '../api'
import { useAgentsStore } from '../stores/agents'
import ConfirmModal from '../components/ConfirmModal.vue'
import StatusBadge from '../components/StatusBadge.vue'
import MeetingsTab from '../components/meetings/MeetingsTab.vue'
import { useToast } from '../composables/useToast'

const toast = useToast()

// Sub-navigation: activity | meetings
const subTab = ref('activity')

const router = useRouter()
const store = useAgentsStore()
const { filteredAgents, filter, selectedAgents, sortLocked, allAgentNames, getEvents, getCurrentAction, toggleAgent, selectAll, toggleSortLock } = useActivityStream()

// Dropdown state
const showAgentDropdown = ref(false)

function toggleDropdown() {
  showAgentDropdown.value = !showAgentDropdown.value
}

function closeDropdown(e) {
  // Close if clicking outside the dropdown
  if (!e.target.closest('.agent-dropdown')) {
    showAgentDropdown.value = false
  }
}

// Click-outside handler
function handleClickOutside(e) {
  if (showAgentDropdown.value && !e.target.closest('.agent-dropdown')) {
    showAgentDropdown.value = false
  }
  if (showDisbandMenu.value && !e.target.closest('.disband-dropdown')) {
    showDisbandMenu.value = false
  }
}
onMounted(() => document.addEventListener('click', handleClickOutside))
onUnmounted(() => document.removeEventListener('click', handleClickOutside))

// Label for the dropdown button
const dropdownLabel = computed(() => {
  if (selectedAgents.value.size === 0) return 'All Agents'
  if (selectedAgents.value.has('__none__')) return 'None Selected'
  const count = selectedAgents.value.size
  return `${count} agent${count > 1 ? 's' : ''} selected`
})

// Expanded event tracking
const expandedEvents = ref(new Set())

function toggleEvent(agentName, idx) {
  const key = `${agentName}_${idx}`
  if (expandedEvents.value.has(key)) {
    expandedEvents.value.delete(key)
  } else {
    expandedEvents.value.add(key)
  }
  expandedEvents.value = new Set(expandedEvents.value)
}

// ── Scroll-lock: preserve user scroll position across event updates ──
// Plain (non-reactive) Map — we don't need Vue to track DOM element refs;
// callback refs handle registration. Using a plain object avoids the
// "Cannot set properties of undefined" TypeError that occurs when the reactive
// wrapper is torn down before the template callback fires.
const eventListRefs = {}
const _savedScrolls = {}

onBeforeUpdate(() => {
  for (const [name, el] of Object.entries(eventListRefs)) {
    if (el) _savedScrolls[name] = el.scrollTop
  }
})

onUpdated(() => {
  for (const [name, el] of Object.entries(eventListRefs)) {
    if (el && _savedScrolls[name] !== undefined) {
      el.scrollTop = _savedScrolls[name]
    }
  }
})

function isExpanded(agentName, idx) {
  return expandedEvents.value.has(`${agentName}_${idx}`)
}

// Status helpers
function statusColor(status) {
  const map = { running: '#f59e0b', paused: '#8b5cf6', idle: '#10b981', error: '#ef4444', completed: '#3b82f6' }
  return map[status] || '#555872'
}

function statusLabel(status) {
  const map = { running: 'Running', paused: 'Paused', idle: 'Idle', error: 'Error', completed: 'Completed' }
  return map[status] || 'Unknown'
}

// Pause/Resume per-agent
const pauseLoading = ref(new Set())

async function handlePauseToggle(agentName, currentStatus) {
  if (pauseLoading.value.has(agentName)) return
  pauseLoading.value.add(agentName)
  pauseLoading.value = new Set(pauseLoading.value)
  try {
    if (currentStatus === 'paused') {
      await store.resumeAgent(agentName)
    } else {
      await store.pauseAgent(agentName)
    }
  } catch (e) {
    console.error('[TeamMonitor] Pause/resume failed:', e)
  } finally {
    pauseLoading.value.delete(agentName)
    pauseLoading.value = new Set(pauseLoading.value)
  }
}

// ── Disband Team ──
const teamNames = computed(() => {
  const names = new Set()
  for (const a of store.agentsList) {
    if (a.team_name) names.add(a.team_name)
  }
  return [...names]
})

// Team color: consistent hash-based color per team name
const TEAM_COLORS = ['#6366f1', '#8b5cf6', '#ec4899', '#14b8a6', '#f97316', '#06b6d4', '#84cc16', '#e879f9']
function teamColor(teamName) {
  if (!teamName) return null
  let hash = 0
  for (let i = 0; i < teamName.length; i++) hash = ((hash << 5) - hash + teamName.charCodeAt(i)) | 0
  return TEAM_COLORS[Math.abs(hash) % TEAM_COLORS.length]
}

// Agents grouped by team for dropdown
const agentsGrouped = computed(() => {
  const groups = []
  const teamMap = new Map()
  const noTeam = []
  for (const a of store.agentsList) {
    if (a.team_name) {
      if (!teamMap.has(a.team_name)) teamMap.set(a.team_name, [])
      teamMap.get(a.team_name).push(a)
    } else {
      noTeam.push(a)
    }
  }
  // Teams first, then solo agents
  for (const [name, agents] of teamMap) {
    groups.push({ type: 'team', name, agents, color: teamColor(name) })
  }
  if (noTeam.length) {
    groups.push({ type: 'solo', name: 'Individual Agents', agents: noTeam, color: null })
  }
  return groups
})

function selectTeam(teamName) {
  const teamAgents = store.agentsList.filter(a => a.team_name === teamName)
  const s = new Set(selectedAgents.value)
  const allSelected = teamAgents.every(a => s.has(a.name))
  if (allSelected) {
    teamAgents.forEach(a => s.delete(a.name))
  } else {
    teamAgents.forEach(a => s.add(a.name))
  }
  selectedAgents.value = s.size === store.agentsList.length ? new Set() : s
}

const disbandModal = ref({ visible: false, teamName: '', loading: false, error: '' })
const showDisbandMenu = ref(false)

function requestDisband(teamName) {
  disbandModal.value = { visible: true, teamName, loading: false, error: '' }
}

function cancelDisband() {
  disbandModal.value = { visible: false, teamName: '', loading: false, error: '' }
}

async function confirmDisband() {
  const name = disbandModal.value.teamName
  disbandModal.value.loading = true
  disbandModal.value.error = ''
  try {
    const result = await apiFetch(`/api/agents/teams/${encodeURIComponent(name)}`, { method: 'DELETE' })
    const removedAgents = result.removed_agents || []
    const removedCount = removedAgents.length

    // Reset filter if currently filtering by this team
    if (filter.value === `team:${name}`) {
      filter.value = 'all'
    }
    disbandModal.value = { visible: false, teamName: '', loading: false, error: '' }
    showDisbandMenu.value = false

    // Note: agents are removed from the store reactively via SSE 'agent_removed' events
    // broadcast by the backend. No manual store mutation needed.
    toast.success(`Team "${name}" disbanded successfully`, {
      description: `${removedCount} agent${removedCount !== 1 ? 's' : ''} removed: ${removedAgents.join(', ')}`,
      duration: 5000,
    })
  } catch (err) {
    disbandModal.value.loading = false
    disbandModal.value.error = err.message || 'Failed to disband team'
    toast.error(`Failed to disband team "${name}"`, {
      description: err.message,
      duration: 5000,
    })
  }
}

function eventTypeIcon(type) {
  const map = {
    started: '▶',
    thinking: '💭',
    tool_call: '🔧',
    tool_result: '📦',
    result: '✅',
    response: '💬',
    error: '❌',
    idle: '⏸',
    resumed: '🔄',
    inject: '⚡',
  }
  return map[type] || '•'
}

function eventTypeColor(type) {
  const map = {
    started: '#00d4aa',
    thinking: '#3b82f6',
    tool_call: '#f59e0b',
    tool_result: '#10b981',
    result: '#3b82f6',
    response: '#c4c8d4',
    error: '#ef4444',
    idle: '#555872',
    resumed: '#00d4aa',
    inject: '#6366f1',
  }
  return map[type] || '#8b8fa3'
}

function formatTime(ts) {
  return formatTimestamp(ts)
}

function truncate(text, max = 80) {
  if (!text) return ''
  return text.length > max ? text.slice(0, max) + '…' : text
}

function navigateToAgent(name) {
  router.push(`/agents/${name}`)
}

// Agent stats
function agentEventCount(name) {
  return getEvents(name).length
}

// Inject prompt — per-agent inline state
const injectState = ref({}) // Map<agentName, { text, loading, result, files }>

function getInjectState(name) {
  if (!injectState.value[name]) {
    injectState.value[name] = { text: '', loading: false, result: null, files: [] }
  }
  return injectState.value[name]
}

function handleFileAttach(agentName, mediaType = 'image') {
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = mediaType === 'audio' ? 'audio/*' : 'image/*'
  input.multiple = true
  input.onchange = (e) => {
    const st = getInjectState(agentName)
    st.files = [...st.files, ...Array.from(e.target.files)]
    injectState.value = { ...injectState.value }
  }
  input.click()
}

function removeFile(agentName, idx) {
  const st = getInjectState(agentName)
  st.files.splice(idx, 1)
  injectState.value = { ...injectState.value }
}

async function submitInject(agentName) {
  const st = getInjectState(agentName)
  if (!st.text.trim() && !st.files.length) return
  st.loading = true
  st.result = null
  injectState.value = { ...injectState.value }

  try {
    let data
    if (st.files.length > 0) {
      // Multipart upload with files
      const formData = new FormData()
      formData.append('message', st.text.trim())
      for (const file of st.files) {
        formData.append('files', file)
      }
      data = await apiFetch(`/api/agents/${agentName}/inject`, {
        method: 'POST',
        body: formData,
      })
    } else {
      // Simple JSON
      data = await apiFetch(`/api/agents/${agentName}/inject`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: st.text.trim() }),
      })
    }
    st.result = data
    st.text = ''
    st.files = []
    // Auto-clear result after 8s
    setTimeout(() => {
      if (injectState.value[agentName]?.result === data) {
        injectState.value[agentName].result = null
        injectState.value = { ...injectState.value }
      }
    }, 8000)
  } catch (err) {
    st.result = { status: 'error', response: err.message }
  } finally {
    st.loading = false
    injectState.value = { ...injectState.value }
  }
}

// Delete agent — modal-based confirmation
const deleteModal = ref({ visible: false, agentName: '', loading: false, error: '' })

function requestDelete(name) {
  deleteModal.value = { visible: true, agentName: name, loading: false, error: '' }
}

function cancelDelete() {
  deleteModal.value = { visible: false, agentName: '', loading: false, error: '' }
}

async function confirmDelete() {
  const name = deleteModal.value.agentName
  deleteModal.value.loading = true
  deleteModal.value.error = ''
  try {
    await apiFetch(`/api/agents/${name}`, { method: 'DELETE' })
    store.agentsList = store.agentsList.filter(a => a.name !== name)
    deleteModal.value = { visible: false, agentName: '', loading: false, error: '' }
    toast.success(`Agent "${name}" deleted`)
  } catch (err) {
    deleteModal.value.loading = false
    deleteModal.value.error = err.message || 'Failed to delete agent'
  }
}

function isDeletableAgent(agent) {
  // All non-builtin agents can be deleted (card, spawn, team)
  return agent.type !== 'builtin'
}

// Ensure agents are loaded
onMounted(() => {
  if (!store.agentsList.length) {
    store.fetchAgents()
  }
})
</script>

<template>
  <div class="team-monitor">
    <!-- Header -->
    <div class="monitor-header">
      <div>
        <h1 class="monitor-title">Team Monitor</h1>
        <p class="monitor-subtitle">Real-time multi-agent activity stream</p>
      </div>
      <div style="display: flex; align-items: center; gap: 12px;">
        <div class="monitor-stats">
          <span class="stat-pill stat-total">{{ store.stats.total }} agents</span>
          <span class="stat-pill stat-running">{{ store.stats.running }} running</span>
          <span class="stat-pill stat-idle">{{ store.stats.idle }} idle</span>
          <span v-if="store.stats.error" class="stat-pill stat-error">{{ store.stats.error }} error</span>
        </div>
        <!-- Disband Team dropdown -->
        <div v-if="teamNames.length" class="disband-dropdown" ref="disbandDropdownRef">
          <button class="disband-team-btn" @click="showDisbandMenu = !showDisbandMenu">
            🗑 Disband Team
            <span class="dropdown-caret" :class="{ open: showDisbandMenu }">▾</span>
          </button>
          <Transition name="dropdown-fade">
            <div v-if="showDisbandMenu" class="disband-menu">
              <button
                v-for="tn in teamNames"
                :key="tn"
                class="disband-menu-item"
                @click="requestDisband(tn); showDisbandMenu = false"
              >
                <span class="disband-menu-icon">🗑</span>
                <span>{{ tn }}</span>
                <span class="disband-menu-count">{{ store.agentsList.filter(a => a.team_name === tn).length }} agents</span>
              </button>
            </div>
          </Transition>
        </div>
      </div>
    </div>

    <!-- Sub-navigation tabs -->
    <div class="sub-nav">
      <button
        class="sub-nav-tab"
        :class="{ active: subTab === 'activity' }"
        @click="subTab = 'activity'"
      >
        <span class="sub-nav-icon">📡</span>
        Activity Stream
      </button>
      <button
        class="sub-nav-tab"
        :class="{ active: subTab === 'meetings' }"
        @click="subTab = 'meetings'"
      >
        <span class="sub-nav-icon">📋</span>
        Meetings
      </button>
    </div>

    <!-- ═══ ACTIVITY TAB ═══ -->
    <template v-if="subTab === 'activity'">
    <!-- Filter Bar -->
    <div class="filter-bar">
      <!-- Row 1: filter pills (always scrollable on mobile) -->
      <div class="filter-pills-row">
        <button
          class="filter-btn"
          :class="{ active: filter === 'all' }"
          @click="filter = 'all'"
        >All</button>
        <button
          class="filter-btn"
          :class="{ active: filter === 'active' }"
          @click="filter = 'active'"
        >Active</button>

        <!-- Team filter pills -->
        <button
          v-for="tn in teamNames"
          :key="'tf-' + tn"
          class="filter-btn team-filter-pill"
          :class="{ active: filter === 'team:' + tn }"
          @click="filter = filter === 'team:' + tn ? 'all' : 'team:' + tn"
          :style="{ '--team-accent': teamColor(tn) }"
        >
          <span class="team-dot" :style="{ background: teamColor(tn) }"></span>
          <span class="team-pill-label">{{ tn }}</span>
        </button>

        <!-- Sort Lock Toggle -->
        <button
          class="filter-btn sort-lock-btn"
          :class="{ active: sortLocked }"
          @click="toggleSortLock"
          :title="sortLocked ? 'Unlock sort' : 'Lock sort'"
        >
          {{ sortLocked ? '🔒' : '🔓' }}
        </button>
      </div>

      <!-- Row 2 (desktop: same row as pills via flex; mobile: own row) -->
      <!-- Agent Dropdown Multi-Select -->
      <div class="agent-dropdown" :class="{ 'dropdown-open': showAgentDropdown }">
        <button class="dropdown-trigger" @click="toggleDropdown">
          <span class="dropdown-icon">🔍</span>
          <span>{{ dropdownLabel }}</span>
          <span class="dropdown-caret" :class="{ open: showAgentDropdown }">▾</span>
        </button>
        <Transition name="dropdown-fade">
          <div v-if="showAgentDropdown" class="dropdown-panel">
            <div class="dropdown-actions">
              <button class="dropdown-action" @click="selectAll">Select All</button>
              <span class="dropdown-divider">|</span>
              <button class="dropdown-action" @click="selectedAgents = new Set(['__none__'])">Clear</button>
            </div>
            <div class="dropdown-list">
              <template v-for="group in agentsGrouped" :key="group.name">
                <!-- Team header -->
                <div class="dropdown-team-header" @click="group.type === 'team' && selectTeam(group.name)">
                  <span v-if="group.color" class="team-dot" :style="{ background: group.color }"></span>
                  <span>{{ group.name }}</span>
                  <span class="dropdown-team-count">{{ group.agents.length }}</span>
                </div>
                <label
                  v-for="a in group.agents"
                  :key="a.name"
                  class="dropdown-item"
                  :class="{ checked: selectedAgents.size === 0 || selectedAgents.has(a.name) }"
                >
                  <input
                    type="checkbox"
                    :checked="selectedAgents.size === 0 || selectedAgents.has(a.name)"
                    @change="toggleAgent(a.name)"
                  />
                  <span class="dropdown-item-dot" :style="{ background: statusColor(a.status) }"></span>
                  <span class="dropdown-item-name">{{ a.name }}</span>
                </label>
              </template>
            </div>
          </div>
        </Transition>
      </div>
    </div>

    <!-- Empty state -->
    <div v-if="!filteredAgents.length" class="empty-state">
      <p>{{ filter === 'active' ? 'No active agents at the moment' : 'No agents found' }}</p>
    </div>

    <!-- Agent Grid -->
    <div class="agent-grid" v-else>
      <div
        v-for="agent in filteredAgents"
        :key="agent.name"
        class="agent-panel"
        :class="{ 'panel-running': agent.status === 'running', 'panel-paused': agent.status === 'paused', 'panel-error': agent.status === 'error' }"
        :style="{ borderLeft: `3px solid ${statusColor(agent.status)}` }"
      >
        <!-- Panel Header -->
        <div class="panel-header">
          <div class="panel-agent-info" @click="navigateToAgent(agent.name)">
            <!-- No dot — status expressed via left border of the panel -->
            <div class="panel-agent-meta">
              <span class="panel-agent-name">{{ agent.name }}</span>
              <span
                v-if="agent.team_name"
                class="panel-team-badge"
                :style="{ background: teamColor(agent.team_name) + '22', color: teamColor(agent.team_name), borderColor: teamColor(agent.team_name) + '44' }"
                :title="agent.team_name"
              >{{ agent.team_name }}</span>
              <span class="panel-agent-model">{{ agent.model || '—' }}</span>
            </div>
          </div>
          <div class="panel-header-right">
            <div class="panel-status-badge" :style="{ color: statusColor(agent.status) }">
              {{ statusLabel(agent.status) }}
            </div>
            <!-- Pause/Resume toggle -->
            <button
              v-if="agent.status === 'running' || agent.status === 'paused'"
              class="panel-pause-btn"
              :class="{ 'is-paused': agent.status === 'paused' }"
              :disabled="pauseLoading.has(agent.name)"
              :title="agent.status === 'paused' ? 'Resume agent' : 'Pause agent'"
              @click.stop="handlePauseToggle(agent.name, agent.status)"
            >
              {{ agent.status === 'paused' ? '▶' : '⏸' }}
            </button>
            <button
              v-if="isDeletableAgent(agent)"
              class="panel-delete-btn"
              title="Remove this agent"
              @click.stop="requestDelete(agent.name)"
            >
              🗑
            </button>
          </div>
        </div>

        <!-- Stats Bar -->
        <div class="panel-stats-bar">
          <span class="panel-stat">
            <span class="stat-label">Events</span>
            <span class="stat-value">{{ agentEventCount(agent.name) }}</span>
          </span>
          <span class="panel-stat" v-if="agent.type">
            <span class="stat-label">Type</span>
            <span class="stat-value">{{ agent.type }}</span>
          </span>
        </div>

        <!-- Current Action -->
        <div class="panel-current-action" v-if="getCurrentAction(agent.name)">
          <div class="action-label">Current Action</div>
          <div class="action-message">
            {{ truncate(getCurrentAction(agent.name).message, 120) }}
          </div>
        </div>

        <!-- Event Log -->
        <div class="panel-event-log">
          <div class="event-log-title">
            Recent Events
            <span class="event-log-count" v-if="getEvents(agent.name).length">
              {{ getEvents(agent.name).length }} this hour
            </span>
          </div>
          <!-- Scroll-locked list: ref keyed per-agent -->
          <div
            class="event-list"
            :ref="el => { if (el) eventListRefs[agent.name] = el; else delete eventListRefs[agent.name] }"
          >
            <div
              v-for="(evt, idx) in getEvents(agent.name)"
              :key="`${evt.timestamp}_${evt.event_type}_${idx}`"
              class="event-item"
              :class="{ expanded: isExpanded(agent.name, idx) }"
              @click="toggleEvent(agent.name, idx)"
            >
              <div class="event-summary">
                <span class="event-icon">{{ eventTypeIcon(evt.event_type) }}</span>
                <span class="event-type" :style="{ color: eventTypeColor(evt.event_type) }">{{ evt.event_type }}</span>
                <!-- 2-line clamp preview — show full via expand -->
                <span class="event-msg">{{ evt.message || '' }}</span>
                <span class="event-time">{{ formatTime(evt.timestamp) }}</span>
              </div>
              <!-- Expanded Detail -->
              <div v-if="isExpanded(agent.name, idx)" class="event-detail">
                <div v-if="evt.full_message || evt.message" class="detail-row">
                  <span class="detail-label">Message</span>
                  <span class="detail-value detail-full-text">{{ evt.full_message || evt.message }}</span>
                </div>
                <div v-if="evt.data" class="detail-row">
                  <span class="detail-label">Data</span>
                  <pre class="detail-pre">{{ JSON.stringify(evt.data, null, 2) }}</pre>
                </div>
                <div v-if="evt.timestamp" class="detail-row">
                  <span class="detail-label">Timestamp</span>
                  <span class="detail-value">{{ formatTimestamp(evt.timestamp, { timeOnly: false }) }}</span>
                </div>
              </div>
            </div>

            <div v-if="!getEvents(agent.name).length" class="no-events">
              No events in the last hour
            </div>
          </div>
        </div>

        <!-- Inline Inject Bar -->
        <div class="panel-inject-bar">
          <!-- Attached files preview -->
          <div v-if="getInjectState(agent.name).files.length" class="inject-files">
            <span v-for="(file, fi) in getInjectState(agent.name).files" :key="fi" class="inject-file-chip">
              {{ file.type.startsWith('image') ? '🖼' : '🎤' }} {{ file.name.slice(0, 20) }}
              <button class="file-remove" @click.stop="removeFile(agent.name, fi)">×</button>
            </span>
          </div>
          <div class="inject-input-row">
            <!-- Image attach -->
            <button class="inject-media-btn" @click="handleFileAttach(agent.name, 'image')" title="Attach image">
              <svg width="16" height="16" viewBox="0 0 16 14" fill="none"><rect x="1" y="1" width="14" height="12" rx="2" stroke="#8b8fa3" stroke-width="1.3"/><circle cx="5" cy="5" r="1.5" stroke="#8b8fa3" stroke-width="1.2"/><path d="M1 11L5 7L8 10L10 8L15 11" stroke="#8b8fa3" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/></svg>
            </button>
            <!-- Audio attach -->
            <button class="inject-media-btn" @click="handleFileAttach(agent.name, 'audio')" title="Attach audio">
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><rect x="5.5" y="1" width="5" height="9" rx="2.5" stroke="#8b8fa3" stroke-width="1.3"/><path d="M3 8C3 11 5.5 13 8 13C10.5 13 13 11 13 8" stroke="#8b8fa3" stroke-width="1.3" stroke-linecap="round"/><line x1="8" y1="13" x2="8" y2="15" stroke="#8b8fa3" stroke-width="1.3" stroke-linecap="round"/></svg>
            </button>
            <!-- Text input -->
            <div class="inject-input-wrap">
              <input
                type="text"
                class="inject-input"
                :placeholder="`Type a message...`"
                :value="getInjectState(agent.name).text"
                :disabled="getInjectState(agent.name).loading"
                @input="e => getInjectState(agent.name).text = e.target.value"
                @keydown.enter="submitInject(agent.name)"
              />
            </div>
            <!-- Send button -->
            <button
              class="inject-send-btn"
              :disabled="(!getInjectState(agent.name).text.trim() && !getInjectState(agent.name).files.length) || getInjectState(agent.name).loading"
              @click="submitInject(agent.name)"
            >
              <svg v-if="!getInjectState(agent.name).loading" width="16" height="16" viewBox="0 0 18 18" fill="none"><path d="M2 9L16 2L9 16L8 10L2 9Z" fill="white" stroke="white" stroke-width="1.2" stroke-linejoin="round"/></svg>
              <span v-else>⏳</span>
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- Delete Confirmation Modal -->
    <ConfirmModal
      :visible="deleteModal.visible"
      title="Remove Agent"
      confirm-text="Remove Agent"
      variant="danger"
      :loading="deleteModal.loading"
      :error="deleteModal.error"
      @confirm="confirmDelete"
      @cancel="cancelDelete"
    >
      Are you sure you want to remove <strong>{{ deleteModal.agentName }}</strong>?
      This will delete its card file, registry entries, and activity history.
    </ConfirmModal>

    <!-- Disband Team Confirmation Modal -->
    <ConfirmModal
      :visible="disbandModal.visible"
      title="Disband Team"
      confirm-text="Disband Team"
      variant="danger"
      :loading="disbandModal.loading"
      :error="disbandModal.error"
      @confirm="confirmDisband"
      @cancel="cancelDisband"
    >
      Are you sure you want to disband team <strong>{{ disbandModal.teamName }}</strong>?
      This will remove all agents, sessions, workspaces, and activity data for this team.
    </ConfirmModal>
    </template>

    <!-- ═══ MEETINGS TAB ═══ -->
    <MeetingsTab v-if="subTab === 'meetings'" />
  </div>
</template>

<style scoped>
.team-monitor {
  max-width: 1600px;
}

/* ─── Sub-navigation ─── */
.sub-nav {
  display: flex;
  gap: 3px;
  margin-bottom: 12px;
  background: #0c0e15;
  border: 1px solid #1a1d2e;
  border-radius: 9px;
  padding: 3px;
  width: fit-content;
}

.sub-nav-tab {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 6px 14px;
  font-size: 12px;
  font-weight: 500;
  color: #8b8fa3;
  background: transparent;
  border: none;
  border-radius: 7px;
  cursor: pointer;
  transition: all 0.15s ease;
  white-space: nowrap;
}

.sub-nav-tab:hover {
  color: #c4c8d4;
  background: #111318;
}

.sub-nav-tab.active {
  background: #1e2233;
  color: #f0f2f5;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.2);
}

.sub-nav-icon {
  font-size: 13px;
}

/* ─── Header ─── */
.monitor-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 14px;
}

.monitor-title {
  font-size: 18px;
  font-weight: 700;
  color: var(--text-heading, #f0f2f5);
  margin: 0;
}

.monitor-subtitle {
  font-size: 12px;
  color: var(--text-sub, #8b8fa3);
  margin: 3px 0 0;
}

.monitor-stats {
  display: flex;
  gap: 8px;
}

.stat-pill {
  padding: 4px 12px;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 500;
}

.stat-total { background: #111830; color: #c4c8d4; }
.stat-running { background: rgba(245, 158, 11, 0.12); color: #f59e0b; }
.stat-idle { background: rgba(16, 185, 129, 0.12); color: #10b981; }
.stat-error { background: rgba(239, 68, 68, 0.12); color: #ef4444; }

/* ─── Filter Bar ─── */
.filter-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 20px;
}

/* Row of pills: All / Active / team filters / sort lock */
.filter-pills-row {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 1;
  flex-wrap: wrap;
  min-width: 0;
}

.filter-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 6px 14px;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 500;
  background: #111318;
  color: #8b8fa3;
  border: 1px solid #1a1d2e;
  cursor: pointer;
  transition: all 0.2s;
  white-space: nowrap;
  flex-shrink: 0;
}

.filter-btn:hover {
  background: #1e2233;
  color: #c4c8d4;
}

.filter-btn.active {
  background: #1e2233;
  color: #f0f2f5;
  border-color: #2a3556;
}

/* Team pill accent on active */
.team-filter-pill.active {
  border-color: var(--team-accent);
  color: var(--team-accent);
  background: color-mix(in srgb, var(--team-accent) 10%, #111318);
}

.team-pill-label {
  max-width: 100px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sort-lock-btn {
  font-size: 13px;
  padding: 6px 10px;
}

/* ─── Agent Dropdown ─── */
.agent-dropdown {
  position: relative;
  flex-shrink: 0;
}

.dropdown-trigger {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 14px;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 500;
  background: #111318;
  color: #c4c8d4;
  border: 1px solid #1a1d2e;
  cursor: pointer;
  transition: all 0.2s;
  white-space: nowrap;
}

.dropdown-trigger:hover {
  background: #1e2233;
  border-color: #2a3556;
  color: #f0f2f5;
}

.dropdown-icon {
  font-size: 12px;
}

.dropdown-caret {
  font-size: 11px;
  transition: transform 0.2s;
  color: #555872;
}

.dropdown-caret.open {
  transform: rotate(180deg);
}

.dropdown-panel {
  position: absolute;
  top: calc(100% + 6px);
  right: 0;
  width: 240px;
  background: #0c0e15;
  border: 1px solid #1a1d2e;
  border-radius: 10px;
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.5);
  z-index: 100;
  overflow: hidden;
}

.dropdown-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-bottom: 1px solid #1a1d2e;
}

.dropdown-action {
  font-size: 11px;
  color: #3b82f6;
  background: none;
  border: none;
  cursor: pointer;
  padding: 0;
  font-weight: 500;
}

.dropdown-action:hover {
  color: #60a5fa;
}

.dropdown-divider {
  color: #2a3556;
  font-size: 11px;
}

.dropdown-list {
  max-height: 260px;
  overflow-y: auto;
  padding: 4px 0;
}

.dropdown-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 12px;
  cursor: pointer;
  transition: background 0.15s;
  font-size: 13px;
  color: #8b8fa3;
}

.dropdown-item:hover {
  background: rgba(30, 34, 51, 0.6);
}

.dropdown-item.checked {
  color: #f0f2f5;
}

.dropdown-item input[type="checkbox"] {
  appearance: none;
  width: 14px;
  height: 14px;
  border-radius: 3px;
  border: 1.5px solid #2a3556;
  background: #111318;
  cursor: pointer;
  position: relative;
  flex-shrink: 0;
}

.dropdown-item input[type="checkbox"]:checked {
  background: #3b82f6;
  border-color: #3b82f6;
}

.dropdown-item input[type="checkbox"]:checked::after {
  content: '✓';
  position: absolute;
  top: -1px;
  left: 1px;
  font-size: 10px;
  color: #fff;
  font-weight: 700;
}

.dropdown-item-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex-shrink: 0;
}

.dropdown-item-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.dropdown-list::-webkit-scrollbar {
  width: 4px;
}

.dropdown-list::-webkit-scrollbar-thumb {
  background: #1a1d2e;
  border-radius: 2px;
}

/* Dropdown team header */
.dropdown-team-header {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px 4px;
  font-size: 10px;
  font-weight: 700;
  color: #555872;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  cursor: pointer;
  user-select: none;
}

.dropdown-team-header:hover {
  color: #8b8fa3;
}

.dropdown-team-count {
  margin-left: auto;
  font-size: 10px;
  font-weight: 500;
  color: #555872;
  background: #1a1d2e;
  border-radius: 4px;
  padding: 1px 5px;
}

/* Dropdown fade transition */
.dropdown-fade-enter-active,
.dropdown-fade-leave-active {
  transition: opacity 0.15s, transform 0.15s;
}

.dropdown-fade-enter-from,
.dropdown-fade-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}

.empty-state {
  display: flex;
  justify-content: center;
  align-items: center;
  height: 300px;
  color: #555872;
  font-size: 14px;
}

/* ─── Agent Grid ─── */
.agent-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 16px;
}

@media (max-width: 1200px) {
  .agent-grid {
    grid-template-columns: 1fr;
  }
}

/* ─── Mobile overrides ─── */
@media (max-width: 767px) {
  .team-monitor {
    max-width: 100%;
  }

  /* Header: stack title + stats vertically */
  .monitor-header {
    flex-direction: column;
    align-items: flex-start;
    gap: 8px;
    margin-bottom: 0;
    padding: 10px 14px;
    border-bottom: 1px solid #1a1d2e;
    background: #0c0e15;
  }

  /* Stats: horizontal scrollable row */
  .monitor-stats {
    display: flex;
    flex-wrap: nowrap;
    gap: 6px;
    overflow-x: auto;
    scrollbar-width: none;
    padding-bottom: 2px;
    width: 100%;
  }

  .monitor-stats::-webkit-scrollbar { display: none; }

  .stat-pill {
    font-size: 11px;
    padding: 3px 10px;
    white-space: nowrap;
    flex-shrink: 0;
  }

  /* Sub-nav: full width, equal tabs */
  .sub-nav {
    width: 100%;
    justify-content: stretch;
  }

  .sub-nav-tab {
    flex: 1;
    justify-content: center;
    padding: 6px 10px;
    font-size: 12px;
  }

  /* ── Filter bar: 2-row stacked layout on mobile ── */
  .filter-bar {
    flex-direction: column;
    align-items: stretch;
    gap: 0;
    margin-bottom: 0;
    padding: 0;
    background: #0c0e15;
    border-bottom: 1px solid #1a1d2e;
  }

  /* Row 1: scrollable pills */
  .filter-pills-row {
    display: flex;
    flex-wrap: nowrap;
    gap: 6px;
    padding: 8px 12px;
    overflow-x: auto;
    scrollbar-width: none;
    border-bottom: 1px solid #111318;
    flex: unset;
  }

  .filter-pills-row::-webkit-scrollbar { display: none; }

  .filter-btn {
    white-space: nowrap;
    flex-shrink: 0;
    font-size: 12px;
    padding: 5px 12px;
  }

  /* Team pill: limit label width on mobile */
  .team-pill-label {
    max-width: 72px;
  }

  /* Row 2: agent dropdown — full width */
  .agent-dropdown {
    width: 100%;
    padding: 8px 12px;
    box-sizing: border-box;
  }

  .dropdown-trigger {
    width: 100%;
    justify-content: space-between;
    font-size: 13px;
    padding: 9px 14px;
    border-radius: 10px;
  }

  /* Dropdown panel: full width, anchored to agent-dropdown container */
  .dropdown-panel {
    width: calc(100vw - 24px);
    left: 0;
    right: 0;
    max-height: 60vh;
    overflow-y: auto;
  }

  .dropdown-list {
    max-height: none;
  }

  /* Agent grid: single column */
  .agent-grid {
    grid-template-columns: 1fr;
    gap: 8px;
  }

  /* Agent panel: flush full-width on mobile */
  .agent-panel {
    border-radius: 8px;
    margin: 0;
  }

  /* Panel header: tighter padding */
  .panel-header {
    padding: 10px 12px 8px;
  }

  .panel-agent-name {
    font-size: 13px;
  }

  /* Hide model label on small screens */
  .panel-agent-model {
    display: none;
  }

  /* Event log: shorter max-height on mobile */
  .panel-event-log {
    max-height: 200px;
  }

  /* Event rows: tighter on mobile */
  .event-row {
    padding: 6px 12px;
    gap: 6px;
  }

  .event-time {
    font-size: 10px;
    min-width: 48px;
  }

  .event-type-badge {
    font-size: 9px;
    padding: 1px 5px;
  }

  .event-msg {
    font-size: 11px;
  }

  /* Inject bar */
  .inject-bar {
    padding: 8px 10px;
    gap: 6px;
  }

  .inject-input {
    font-size: 13px;
    min-height: 36px;
  }

  .inject-btn {
    width: 36px;
    height: 36px;
    flex-shrink: 0;
  }

  /* Disband button: full width on mobile */
  .disband-team-btn {
    width: 100%;
    justify-content: center;
  }
}

/* ─── Agent Panel ─── */
.agent-panel {
  background: #0c0e15;
  border: 1px solid #1a1d2e;
  border-radius: 12px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  transition: border-color 0.3s;
}

.agent-panel.panel-running {
  border-color: rgba(245, 158, 11, 0.3);
}

.agent-panel.panel-error {
  border-color: rgba(239, 68, 68, 0.3);
}

/* Panel Header */
.panel-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 8px;
  padding: 12px 16px 10px;
  border-bottom: 1px solid #1a1d2e;
  min-width: 0;
}

.panel-agent-info {
  display: flex;
  align-items: flex-start;  /* dot aligns with name, not vertical center when there's a team */
  gap: 8px;
  cursor: pointer;
  min-width: 0;
  flex: 1;
}

.panel-agent-info:hover .panel-agent-name {
  color: #3b82f6;
}

/* Column wrapper: name on row 1, team badge on row 2, model below */
.panel-agent-meta {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
  flex: 1;
}

.panel-running .status-dot {
  animation: pulse-dot 2s infinite;
}

@keyframes pulse-dot {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}

.panel-agent-name {
  font-size: 14px;
  font-weight: 600;
  color: #f0f2f5;
  transition: color 0.2s;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.panel-agent-model {
  font-size: 11px;
  color: #555872;
}

.panel-status-badge {
  font-size: 11px;
  font-weight: 500;
}

.panel-header-right {
  display: flex;
  align-items: center;
  gap: 8px;
}

.panel-delete-btn {
  background: none;
  border: 1px solid transparent;
  color: #555872;
  font-size: 14px;
  cursor: pointer;
  padding: 2px 4px;
  border-radius: 4px;
  transition: all 0.2s;
  line-height: 1;
}

.panel-delete-btn:hover {
  color: #ef4444;
}

/* Pause/Resume button */
.panel-pause-btn {
  background: rgba(245, 158, 11, 0.15);
  border: 1px solid rgba(245, 158, 11, 0.3);
  color: #f59e0b;
  font-size: 13px;
  cursor: pointer;
  padding: 2px 6px;
  border-radius: 4px;
  transition: all 0.2s;
  line-height: 1;
}

.panel-pause-btn:hover {
  background: rgba(245, 158, 11, 0.25);
}

.panel-pause-btn.is-paused {
  background: rgba(34, 197, 94, 0.15);
  border-color: rgba(34, 197, 94, 0.3);
  color: #22c55e;
}

.panel-pause-btn.is-paused:hover {
  background: rgba(34, 197, 94, 0.25);
}

.panel-pause-btn:disabled {
  opacity: 0.5;
  cursor: wait;
}

/* Paused panel border */
.panel-paused {
  border-color: rgba(245, 158, 11, 0.3) !important;
}

/* Disband Team button */
.disband-team-btn {
  background: rgba(239, 68, 68, 0.1);
  border: 1px solid rgba(239, 68, 68, 0.3);
  color: #ef4444;
  font-size: 12px;
  font-weight: 500;
  padding: 6px 12px;
  border-radius: 8px;
  cursor: pointer;
  transition: all 0.2s;
  white-space: nowrap;
}

.disband-team-btn:hover {
  background: rgba(239, 68, 68, 0.2);
  border-color: rgba(239, 68, 68, 0.5);
}

/* Disband dropdown */
.disband-dropdown {
  position: relative;
}

.disband-menu {
  position: absolute;
  top: calc(100% + 4px);
  right: 0;
  min-width: 200px;
  background: #111318;
  border: 1px solid #1e2030;
  border-radius: 8px;
  padding: 4px;
  z-index: 100;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
}

.disband-menu-item {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 8px 12px;
  background: none;
  border: none;
  border-radius: 6px;
  color: #c4c8d4;
  font-size: 13px;
  cursor: pointer;
  transition: all 0.15s;
  text-align: left;
}

.disband-menu-item:hover {
  background: rgba(239, 68, 68, 0.1);
  color: #ef4444;
}

.disband-menu-icon {
  font-size: 14px;
}

.disband-menu-count {
  margin-left: auto;
  font-size: 11px;
  color: #555872;
}

/* Team visual helpers */
.team-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}

.team-filter-pill.active {
  border-color: var(--team-accent) !important;
  background: color-mix(in srgb, var(--team-accent) 15%, transparent) !important;
  color: var(--team-accent) !important;
}

.dropdown-team-header {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  margin-top: 4px;
  font-size: 11px;
  font-weight: 600;
  color: #8b8fa3;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  cursor: pointer;
  border-radius: 4px;
  transition: background 0.15s;
}

.dropdown-team-header:first-child {
  margin-top: 0;
}

.dropdown-team-header:hover {
  background: rgba(99, 102, 241, 0.08);
}

.dropdown-team-count {
  margin-left: auto;
  font-size: 10px;
  font-weight: 400;
  color: #555872;
  background: rgba(85, 88, 114, 0.15);
  padding: 1px 6px;
  border-radius: 8px;
}

.panel-team-badge {
  font-size: 10px;
  font-weight: 600;
  padding: 1px 8px;
  border-radius: 10px;
  border: 1px solid;
  white-space: nowrap;
  letter-spacing: 0.3px;
  /* Truncate long team names */
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
  display: inline-block;
}

/* Stats Bar */
.panel-stats-bar {
  display: flex;
  gap: 20px;
  padding: 8px 16px;
  background: rgba(17, 19, 24, 0.5);
}

.panel-stat {
  display: flex;
  gap: 6px;
  font-size: 11px;
}

.stat-label { color: #555872; }
.stat-value { color: #c4c8d4; font-weight: 500; }

/* Current Action */
.panel-current-action {
  padding: 10px 16px;
  border-bottom: 1px solid #1a1d2e;
}

.action-label {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: #555872;
  margin-bottom: 4px;
}

.action-message {
  font-size: 12px;
  color: #c4c8d4;
  line-height: 1.4;
}

/* Event Log */
.panel-event-log {
  flex: 1;
  padding: 10px 16px 8px;
  min-height: 120px;
  max-height: 300px;
  overflow-y: auto;
}

.event-log-title {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: #555872;
  margin-bottom: 8px;
}

.event-log-count {
  font-size: 10px;
  color: #3b82f6;
  text-transform: none;
  letter-spacing: 0;
  font-weight: 500;
  background: rgba(59, 130, 246, 0.08);
  padding: 1px 6px;
  border-radius: 8px;
}

.event-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: 240px;
  overflow-y: auto;
}

.event-item {
  border-radius: 6px;
  cursor: pointer;
  transition: background 0.15s;
}

.event-item:hover {
  background: rgba(30, 34, 51, 0.5);
}

.event-summary {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 8px;
  font-size: 12px;
}

.event-icon {
  font-size: 11px;
  flex-shrink: 0;
  width: 16px;
  text-align: center;
}

.event-type {
  font-weight: 500;
  font-size: 11px;
  min-width: 72px;
}

.event-msg {
  color: #8b8fa3;
  font-size: 11px;
  flex: 1;
  overflow: hidden;
  /* 2-line clamp: shows preview, tap event-item to expand full text */
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  word-break: break-word;
  white-space: normal;
  line-height: 1.35;
}

/* When event is expanded, show message without clamp in the event-summary too */
.event-item.expanded .event-msg {
  -webkit-line-clamp: unset;
}

.event-time {
  color: #555872;
  font-size: 10px;
  flex-shrink: 0;
}

/* Expanded Event Detail */
.event-detail {
  padding: 6px 8px 8px 30px;
  background: rgba(17, 24, 48, 0.3);
  border-radius: 0 0 6px 6px;
  animation: slide-in 0.15s ease-out;
}

@keyframes slide-in {
  from { opacity: 0; transform: translateY(-4px); }
  to { opacity: 1; transform: translateY(0); }
}

.detail-row {
  display: flex;
  gap: 8px;
  margin-bottom: 4px;
  font-size: 11px;
}

.detail-label {
  color: #555872;
  min-width: 70px;
  flex-shrink: 0;
}

.detail-value {
  color: #c4c8d4;
  word-break: break-word;
}

.detail-full-text {
  max-height: 200px;
  overflow-y: auto;
  white-space: pre-wrap;
  line-height: 1.5;
}

.detail-pre {
  color: #8b8fa3;
  font-size: 10px;
  background: rgba(10, 13, 20, 0.5);
  padding: 6px 8px;
  border-radius: 4px;
  overflow-x: auto;
  max-height: 120px;
  margin: 0;
}

.no-events {
  text-align: center;
  padding: 16px;
  color: #555872;
  font-size: 12px;
}

/* ─── Inline Inject Bar (Chat-style) ─── */
.panel-inject-bar {
  border-top: 1px solid #1a1d2e;
  padding: 8px 12px 10px;
  background: #0c0e15;
}

.inject-input-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.inject-media-btn {
  width: 32px;
  height: 32px;
  background: #1e2233;
  border-radius: 8px;
  border: none;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  transition: background 0.2s;
}

.inject-media-btn:hover {
  background: #2a3556;
}

.inject-media-btn:hover svg rect,
.inject-media-btn:hover svg circle,
.inject-media-btn:hover svg path,
.inject-media-btn:hover svg line {
  stroke: #c4c8d4;
}

.inject-input-wrap {
  flex: 1;
  height: 36px;
  background: #111318;
  border: 1px solid #1e2030;
  border-radius: 10px;
  display: flex;
  align-items: center;
  padding: 0 12px;
  transition: border-color 0.2s;
}

.inject-input-wrap:focus-within {
  border-color: #2a3556;
}

.inject-input {
  flex: 1;
  background: transparent;
  border: none;
  color: #f0f2f5;
  font-size: 12px;
  font-family: 'Inter', sans-serif;
  outline: none;
}

.inject-input::placeholder {
  color: #555872;
  font-weight: 400;
}

.inject-send-btn {
  width: 36px;
  height: 36px;
  border-radius: 10px;
  background: #3b82f6;
  color: white;
  border: none;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: opacity 0.2s;
  flex-shrink: 0;
}

.inject-send-btn:disabled {
  opacity: 0.3;
  cursor: not-allowed;
  background: #1e2233;
}

.inject-send-btn:not(:disabled):hover {
  opacity: 0.85;
}

/* File chips */
.inject-files {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-bottom: 6px;
}

.inject-file-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  background: #1e2233;
  border-radius: 12px;
  font-size: 11px;
  color: #c4c8d4;
}

.file-remove {
  background: none;
  border: none;
  color: #8b8fa3;
  font-size: 14px;
  cursor: pointer;
  padding: 0 2px;
  line-height: 1;
}

.file-remove:hover {
  color: #ef4444;
}

/* Inline result */
.inject-inline-result {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  padding: 6px 8px;
  margin-bottom: 6px;
  background: rgba(16, 185, 129, 0.06);
  border: 1px solid rgba(16, 185, 129, 0.15);
  border-radius: 8px;
  font-size: 11px;
  color: #10b981;
  animation: slide-in 0.15s ease-out;
}

.inject-inline-result.result-error {
  background: rgba(239, 68, 68, 0.06);
  border-color: rgba(239, 68, 68, 0.15);
  color: #ef4444;
}

.result-icon {
  flex-shrink: 0;
}

.result-text {
  color: #c4c8d4;
  word-break: break-word;
  line-height: 1.4;
}

/* Scrollbar styling */
.panel-event-log::-webkit-scrollbar {
  width: 4px;
}

.panel-event-log::-webkit-scrollbar-track {
  background: transparent;
}

.panel-event-log::-webkit-scrollbar-thumb {
  background: #1a1d2e;
  border-radius: 2px;
}
</style>




<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useActivityStream } from '../composables/useActivityStream'
import { useAgentTurns } from '../composables/useAgentTurns'
import { apiFetch } from '../api'
import { useAgentsStore } from '../stores/agents'
import ConfirmModal from '../components/ConfirmModal.vue'
import MeetingsTab from '../components/meetings/MeetingsTab.vue'
import AgentTerminal from '../components/monitor/AgentTerminal.vue'
import { useToast } from '../composables/useToast'

// Explicit name so AppLayout's `<keep-alive include="['Chat', 'TeamMonitor']">`
// preserves activity-stream / meeting-stream subscriptions across nav.
defineOptions({ name: 'TeamMonitor' })

const toast = useToast()

// Sub-navigation: activity | meetings
const subTab = ref('activity')

const store = useAgentsStore()
const { filteredAgents, filter, selectedAgents, sortLocked, toggleAgent, selectAll, toggleSortLock } = useActivityStream()

// Terminal-style monitor grid (the only UI now — v1 card grid was
// removed). Per-agent turn buffer keeps history available as new
// agents come online via activity stream.
const agentTurns = useAgentTurns({ maxPerAgent: 200 })

// Fetch each agent's initial history once when it appears in the
// roster. Skipping fetchInitial calls past the first per-agent is
// handled inside ``useAgentTurns.fetchInitial``.
watch(
  () => filteredAgents.value.map(a => a.name),
  (names) => {
    for (const n of names) agentTurns.fetchInitial(n)
  },
  { immediate: true },
)

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

// Status helpers (used by dropdown item dots and other shared UI)
function statusColor(status) {
  const map = { running: '#f59e0b', paused: '#8b5cf6', idle: '#10b981', error: '#ef4444', completed: '#3b82f6' }
  return map[status] || '#555872'
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

/**
 * Send a prompt injection to ``agentName``. Used by the AgentTerminal
 * inject footer. Returns the API response so the caller can display
 * feedback.
 */
async function injectToAgent(agentName, { text = '', files = [] } = {}) {
  if (!text.trim() && !files.length) return null
  if (files.length > 0) {
    const formData = new FormData()
    formData.append('message', text.trim())
    for (const file of files) formData.append('files', file)
    return apiFetch(`/api/agents/${agentName}/inject`, { method: 'POST', body: formData })
  }
  return apiFetch(`/api/agents/${agentName}/inject`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message: text.trim() }),
  })
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

// Ensure the full roster is loaded. We can't gate on agentsList.length —
// SSE events may have already added a partial set of agents to the store
// (the activity-stream subscription opens at app boot, before this
// component mounts), and that would mask the need for the REST roster
// fetch and leave the page showing only the SSE-known subset. Calling
// fetchAgents() unconditionally is idempotent and a single GET.
onMounted(() => {
  store.fetchAgents()
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
      <span class="sub-nav-spacer" />
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

    <!-- ═══ Agent grid — terminal-style, message_history-driven ═══ -->
    <div v-else class="agent-grid agent-grid-v2">
      <AgentTerminal
        v-for="agent in filteredAgents"
        :key="agent.name"
        :agent="agent"
        :turns="agentTurns.getTurns(agent.name)"
        :loading="!agentTurns.fetched.value.has(agent.name)"
        :on-fetch-full="(turnIdx) => agentTurns.fetchTurnFull(agent.name, turnIdx)"
        :on-pause-toggle="(agent.status === 'running' || agent.status === 'paused')
          ? () => handlePauseToggle(agent.name, agent.status)
          : null"
        :on-delete="isDeletableAgent(agent) ? () => requestDelete(agent.name) : null"
        :on-inject="(payload) => injectToAgent(agent.name, payload)"
      />
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
  align-items: center;
  gap: 3px;
  margin-bottom: 12px;
  background: #0c0e15;
  border: 1px solid #1a1d2e;
  border-radius: 9px;
  padding: 3px;
  /* width: fit-content was here — opening up to full width so the v2 toggle
     can align to the right edge via .sub-nav-spacer. */
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

.sub-nav-spacer { flex: 1; }

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

  /* Sub-nav: full width, equal tabs. The version toggle wraps to a
     second row on mobile so it doesn't squeeze the activity/meetings
     tabs into a tiny strip. */
  .sub-nav {
    width: 100%;
    flex-wrap: wrap;
    justify-content: stretch;
  }

  .sub-nav-tab {
    flex: 1;
    justify-content: center;
    padding: 6px 10px;
    font-size: 12px;
  }

  .sub-nav-spacer { display: none; }

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

  /* Disband button: full width on mobile */
  .disband-team-btn {
    width: 100%;
    justify-content: center;
  }
}

</style>




<script setup>
import { ref, computed, onMounted, onUnmounted, watch, nextTick } from 'vue'
import { useActivityStream } from '../composables/useActivityStream'
import { useAgentTurns } from '../composables/useAgentTurns'
import { apiFetch } from '../api'
import { useAgentsStore } from '../stores/agents'
import { useApprovalsStore } from '../stores/approvals'
import ConfirmModal from '../components/ConfirmModal.vue'
import AgentTerminal from '../components/monitor/AgentTerminal.vue'
import LifecycleBar from '../components/monitor/LifecycleBar.vue'
import BulkInjectBar from '../components/monitor/BulkInjectBar.vue'
import { useToast } from '../composables/useToast'
import { statusColor } from '../components/agent/agentMeta.js'
import { useLang } from '../composables/useLang'

const { t } = useLang()

// Explicit name so AppLayout's `<keep-alive include="['Chat', 'TeamMonitor']">`
// preserves activity-stream / meeting-stream subscriptions across nav.
defineOptions({ name: 'TeamMonitor' })

const toast = useToast()

// Sub-navigation: activity | meetings

const store = useAgentsStore()
const approvalsStore = useApprovalsStore()
const { filteredAgents, filter, selectedAgents, toggleAgent, selectAll } = useActivityStream()

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
const dropdownSearchInput = ref(null)

function toggleDropdown() {
  showAgentDropdown.value = !showAgentDropdown.value
  if (showAgentDropdown.value) {
    // Autofocus the search input so users can type immediately.
    nextTick(() => dropdownSearchInput.value?.focus())
  } else {
    // Clear search when closing so reopening starts clean.
    dropdownSearch.value = ''
  }
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
    dropdownSearch.value = ''
  }
}
onMounted(() => document.addEventListener('click', handleClickOutside))
onUnmounted(() => document.removeEventListener('click', handleClickOutside))

// Label for the dropdown button. Includes a "· K deletable" suffix
// whenever the selection contains built-in agents (which view fine but
// are skipped by bulk-delete). Without it, users see "16 selected" next
// to a "Delete (11)" badge and can't tell where the 5-agent gap came
// from — same confusion that prompted this whole thread.
const dropdownLabel = computed(() => {
  if (selectedAgents.value.size === 0) return t('teamMonitor.allAgents')   // implicit-all (initial)
  if (selectedAgents.value.has('__none__')) return t('teamMonitor.noneSelected')
  const count = selectedAgents.value.size
  const total = store.agentsList.length
  const deletable = deletableSelectedNames.value.length
  let main = (total > 0 && count === total)
    ? t('teamMonitor.allCountSelected', { n: total })
    : t('teamMonitor.countSelected', { n: count })
  if (deletable < count) main += ' · ' + t('teamMonitor.deletableSuffix', { n: deletable })
  return main
})

// Status color comes from the canonical `agentMeta::statusColor` import above
// (was a local map with running=amber/idle=green; that contradicted both the
// design tokens and every other view).

// Pause/Resume per-agent
const pauseLoading = ref(new Set())

async function handlePauseToggle(agentName, currentStatus) {
  if (pauseLoading.value.has(agentName)) return
  // Ignore clicks during transitional states — the previous request
  // hasn't completed yet. Double-firing causes pause/resume churn
  // (controller will no-op but UI flickers).
  if (currentStatus === 'pausing' || currentStatus === 'resuming') return
  pauseLoading.value.add(agentName)
  pauseLoading.value = new Set(pauseLoading.value)
  try {
    if (currentStatus === 'paused') {
      await store.resumeAgent(agentName)
    } else {
      // Pause only running agents — idle agents have nothing in
      // flight to interrupt and never show the toggle (the v-if
      // guard above keeps them out).
      await store.pauseAgent(agentName)
    }
  } catch (e) {
    if (e?.code === 'approval_pause_lock') {
      toast?.show?.(
        t('teamMonitor.pausedByApproval', { id: e.approvalId }),
        { kind: 'warn' },
      )
    } else {
      console.error('[TeamMonitor] Pause/resume failed:', e)
    }
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
    groups.push({ type: 'solo', name: t('teamMonitor.individualAgents'), agents: noTeam, color: null })
  }
  return groups
})

// Search-by-name filter for the dropdown. Case-insensitive substring match
// against agent.name AND team header (so typing a team name still surfaces
// its members). Empty query → returns all groups untouched.
const dropdownSearch = ref('')
const agentsGroupedFiltered = computed(() => {
  const q = dropdownSearch.value.trim().toLowerCase()
  if (!q) return agentsGrouped.value
  const out = []
  for (const group of agentsGrouped.value) {
    const headerHit = group.name.toLowerCase().includes(q)
    const agents = headerHit
      ? group.agents  // team name matched — keep the whole roster
      : group.agents.filter(a => a.name.toLowerCase().includes(q))
    if (agents.length) out.push({ ...group, agents })
  }
  return out
})

/**
 * Toggle select-all for the agents currently shown in a dropdown group.
 *
 * Works for BOTH team groups and the synthetic "Individual Agents" group.
 * Crucially, operates on ``group.agents`` (which is already narrowed by
 * the search filter via ``agentsGroupedFiltered``) so users can search
 * for "agent-0", click the header, and select exactly those matches —
 * not the entire roster.
 */
function toggleGroup(group) {
  const agents = group.agents || []
  if (!agents.length) return
  let s = new Set(selectedAgents.value)
  // ── Order matters: check size BEFORE stripping the ``__none__``
  // sentinel — same contract as toggleAgent. Three starting states:
  //   {__none__}   user explicitly cleared → start blank (size=1, skip
  //                expand). After delete sentinel, s = {} and we
  //                proceed to add the clicked group's agents.
  //   {} (empty)   implicit-all / pre-roster-load → expand to all so
  //                toggling a group means "subtract from all".
  //   {names...}   explicit selection → use as-is.
  // The previous order (delete first, then size check) collapsed
  // {__none__} into {} → triggered the expand-to-all path → flipped
  // a "Clear → click team" into "select all-except-team". That's
  // exactly the 29-selected bug user reported.
  if (s.size === 0) {
    s = new Set(store.agentsList.map(a => a.name))
  }
  s.delete('__none__')
  const allSelected = agents.every(a => s.has(a.name))
  if (allSelected) {
    agents.forEach(a => s.delete(a.name))
  } else {
    agents.forEach(a => s.add(a.name))
  }
  // No auto-collapse to empty when full: the new contract requires
  // explicit Set membership so destructive actions (bulk delete) know
  // exactly which agents the user consented to.
  selectedAgents.value = s
}

// ── Bulk delete: remove every agent currently in ``selectedAgents`` ──
//
// Replaces the old "Disband Team" button: instead of deleting a whole
// team in one shot, the user picks exactly which agents (via the multi-
// select dropdown + search) and confirms a single bulk delete.

const bulkDeleteModal = ref({ visible: false, names: [], protected: [], loading: false, error: '' })

// ── Two views of the same selection ────────────────────────────────
//
// The dropdown is shared by two concerns:
//   1. View filter — pick which agents to show in the activity stream.
//   2. Bulk delete — pick which agents to remove.
//
// (1) needs to include built-in agents (Jarvis, PersonalAgent, etc.)
// because users legitimately want to monitor their conversations.
// (2) MUST exclude them — they're not deletable by design (backend
// would reject anyway). So derive two lists from one selection:
//   - selectedAgentNames     → for any general "what did the user pick"
//   - deletableSelectedNames → for the bulk-delete action specifically
// Plus a protected list so the confirm modal can tell the user
// explicitly which built-ins are being kept, instead of silently
// dropping them.

function _isBuiltin(agent) {
  return agent?.type === 'builtin'
}

const selectedAgentNames = computed(() => {
  const known = new Set(store.agentsList.map(a => a.name))
  return [...selectedAgents.value].filter(n => n !== '__none__' && known.has(n))
})

const deletableSelectedNames = computed(() => {
  const byName = new Map(store.agentsList.map(a => [a.name, a]))
  return selectedAgentNames.value.filter(n => !_isBuiltin(byName.get(n)))
})

// Built-ins inside the current selection — surfaced in the confirm
// modal as "kept" so the user understands why the delete count is
// smaller than their visible tick count.
const protectedSelectedNames = computed(() => {
  const byName = new Map(store.agentsList.map(a => [a.name, a]))
  return selectedAgentNames.value.filter(n => _isBuiltin(byName.get(n)))
})

const canBulkDelete = computed(() => deletableSelectedNames.value.length > 0)

// Tooltip text reflects all three cases the user might be in: nothing
// to delete, some deletable, or selection contains only built-ins
// (visible but protected).
const bulkDeleteTooltip = computed(() => {
  const n = deletableSelectedNames.value.length
  const skipped = protectedSelectedNames.value.length
  if (n === 0 && skipped > 0) {
    return t('teamMonitor.tooltipOnlyBuiltin')
  }
  if (n === 0) return t('teamMonitor.tooltipSelectFirst')
  return skipped > 0
    ? t('teamMonitor.tooltipDeleteKept', { n, skipped })
    : t('teamMonitor.tooltipDelete', { n })
})

function requestBulkDelete() {
  if (!canBulkDelete.value) return
  bulkDeleteModal.value = {
    visible: true,
    names: deletableSelectedNames.value.slice(),
    protected: protectedSelectedNames.value.slice(),
    loading: false,
    error: '',
  }
}

function cancelBulkDelete() {
  bulkDeleteModal.value = { visible: false, names: [], protected: [], loading: false, error: '' }
}

async function confirmBulkDelete() {
  const names = bulkDeleteModal.value.names
  if (!names.length) return
  bulkDeleteModal.value.loading = true
  bulkDeleteModal.value.error = ''
  // Fire DELETEs in parallel — independent records, safe to overlap.
  // Use allSettled so a partial failure surfaces every error rather than
  // hiding all but the first.
  const results = await Promise.allSettled(
    names.map(n => apiFetch(`/api/agents/${encodeURIComponent(n)}`, { method: 'DELETE' })),
  )
  const ok = []
  const fail = []
  results.forEach((r, i) => {
    if (r.status === 'fulfilled') ok.push(names[i])
    else fail.push({ name: names[i], err: r.reason?.message || String(r.reason) })
  })

  // Clear selection of the agents we actually removed. Backend broadcasts
  // ``agent_removed`` SSE so the store drops them reactively — we just
  // need to keep selectedAgents in sync.
  const s = new Set(selectedAgents.value)
  ok.forEach(n => s.delete(n))
  selectedAgents.value = s

  bulkDeleteModal.value.loading = false
  if (fail.length) {
    bulkDeleteModal.value.error =
      t('teamMonitor.bulkFailedHeading', { fail: fail.length, total: names.length }) + '\n' +
      fail.map(f => `• ${f.name}: ${f.err}`).join('\n')
    toast.error(t('teamMonitor.bulkFailedToast', { fail: fail.length, total: names.length }), {
      description: fail.map(f => `${f.name}: ${f.err}`).join('\n'),
      duration: 6000,
    })
    // Keep modal open so the user can read the per-agent errors.
    return
  }

  bulkDeleteModal.value = { visible: false, names: [], protected: [], loading: false, error: '' }
  toast.success(t('teamMonitor.bulkDeletedToast', { n: ok.length }), {
    description: ok.join(', '),
    duration: 5000,
  })
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
    toast.success(t('teamMonitor.agentDeletedToast', { name }))
  } catch (err) {
    deleteModal.value.loading = false
    deleteModal.value.error = err.message || t('teamMonitor.deleteFailed')
  }
}

function isDeletableAgent(agent) {
  // All non-builtin agents can be deleted (card, spawn, team)
  return agent.type !== 'builtin'
}

/**
 * Bulk inject — fan out the inject payload to every selected agent.
 * If the user has the implicit-all selection (size 0), we use the
 * filtered visible roster so they don't accidentally broadcast to
 * paused/idle agents off-screen.
 */
async function bulkInject({ text, files }) {
  // Resolve target agents from explicit selection or visible filteredAgents.
  let targets
  if (selectedAgents.value.size === 0) {
    targets = filteredAgents.value.map(a => a.name)
  } else {
    targets = selectedAgentNames.value
  }
  if (!targets.length) {
    toast.warning?.(t('teamMonitor.noAgentsForInject'), { duration: 3000 })
    return []
  }
  // POST to each agent in parallel; allSettled so a partial failure
  // doesn't silently drop the rest of the broadcast.
  return Promise.allSettled(
    targets.map(name => injectToAgent(name, { text, files })),
  )
}

// Names list passed to the bulk inject bar so its counter is honest.
// Falls back to filteredAgents (visible) when selection is implicit-all.
const bulkInjectTargets = computed(() => {
  if (selectedAgents.value.size === 0) return filteredAgents.value.map(a => a.name)
  return selectedAgentNames.value
})

// Ensure the full roster is loaded. We can't gate on agentsList.length —
// SSE events may have already added a partial set of agents to the store
// (the activity-stream subscription opens at app boot, before this
// component mounts), and that would mask the need for the REST roster
// fetch and leave the page showing only the SSE-known subset. Calling
// fetchAgents() unconditionally is idempotent and a single GET.
onMounted(() => {
  store.fetchAgents()
  // Seed the approvals store with pending rows so the per-agent
  // pause-by-approval gate (AgentCard "Awaiting approval" pill) shows
  // immediately on first paint — without this, a tab opened fresh
  // would render a Resume button on an approval-locked agent until
  // the next SSE event lands.
  approvalsStore.fetchApprovals('pending')
})
</script>

<template>
  <div class="team-monitor">
    <!-- Header -->
    <div class="monitor-header">
      <div>
        <h1 class="monitor-title">{{ t('teamMonitor.title') }}</h1>
        <p class="monitor-subtitle">{{ t('teamMonitor.subtitle') }}</p>
      </div>
      <div style="display: flex; align-items: center; gap: 12px;">
        <div class="monitor-stats">
          <span class="stat-pill stat-total">{{ t('teamMonitor.statAgents', { n: store.stats.total }) }}</span>
          <span class="stat-pill stat-running">{{ t('teamMonitor.statRunning', { n: store.stats.running }) }}</span>
          <span class="stat-pill stat-idle">{{ t('teamMonitor.statIdle', { n: store.stats.idle }) }}</span>
          <span v-if="store.stats.error" class="stat-pill stat-error">{{ t('teamMonitor.statError', { n: store.stats.error }) }}</span>
        </div>
        <!-- Bulk delete selected (deletable) agents -->
        <button
          class="bulk-delete-btn"
          :disabled="!canBulkDelete"
          :title="bulkDeleteTooltip"
          @click="requestBulkDelete"
        >
          🗑 {{ t('teamMonitor.deleteSelected') }}
          <span v-if="canBulkDelete" class="bulk-delete-count">{{ deletableSelectedNames.length }}</span>
        </button>
      </div>
    </div>


    <!-- Activity stream (Meetings moved to dedicated /meetings route) -->
    <!-- Filter Bar -->
    <div class="filter-bar">
      <!-- Row 1: filter pills (always scrollable on mobile) -->
      <div class="filter-pills-row">
        <button
          class="filter-btn"
          :class="{ active: filter === 'all' }"
          @click="filter = 'all'"
        >{{ t('teamMonitor.filterAll') }}</button>
        <button
          class="filter-btn"
          :class="{ active: filter === 'active' }"
          @click="filter = 'active'"
        >{{ t('teamMonitor.filterActive') }}</button>

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
              <button class="dropdown-action" @click="selectAll">{{ t('teamMonitor.selectAll') }}</button>
              <span class="dropdown-divider">|</span>
              <button class="dropdown-action" @click="selectedAgents = new Set(['__none__'])">{{ t('teamMonitor.clear') }}</button>
            </div>
            <div class="dropdown-search">
              <input
                ref="dropdownSearchInput"
                v-model="dropdownSearch"
                class="dropdown-search-input"
                type="text"
                :placeholder="t('teamMonitor.searchByName')"
                @keydown.escape.stop="dropdownSearch = ''"
              />
              <button
                v-if="dropdownSearch"
                class="dropdown-search-clear"
                :title="t('teamMonitor.clearSearch')"
                @click="dropdownSearch = ''"
              >×</button>
            </div>
            <div v-if="!agentsGroupedFiltered.length" class="dropdown-empty">
              {{ t('teamMonitor.noAgentsMatch', { q: dropdownSearch }) }}
            </div>
            <div v-else class="dropdown-list">
              <template v-for="group in agentsGroupedFiltered" :key="group.name">
                <!-- Team header -->
                <div class="dropdown-team-header" @click="toggleGroup(group)">
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
                  <!-- Built-in agents can be selected for VIEW filter,
                       but the bulk-delete action skips them. The lock
                       icon plus title tooltip make that clear up front
                       so users don't expect a Delete-Selected click to
                       remove Jarvis et al. -->
                  <span
                    v-if="a.type === 'builtin'"
                    class="dropdown-item-builtin"
                    :title="t('teamMonitor.builtinProtected')"
                  >🔒</span>
                </label>
              </template>
            </div>
          </div>
        </Transition>
      </div>
    </div>

    <!-- Live lifecycle event ticker — between selector and grid -->
    <LifecycleBar class="lifecycle-bar-host" />

    <!-- Empty state -->
    <div v-if="!filteredAgents.length" class="empty-state">
      <p>{{ filter === 'active' ? t('teamMonitor.noActiveAgents') : t('teamMonitor.noAgentsFound') }}</p>
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
        :on-pause-toggle="['running', 'paused', 'pausing', 'resuming'].includes(agent.status)
          ? () => handlePauseToggle(agent.name, agent.status)
          : null"
        :on-delete="isDeletableAgent(agent) ? () => requestDelete(agent.name) : null"
        :on-inject="(payload) => injectToAgent(agent.name, payload)"
      />
    </div>

    <!-- Bulk inject bar — broadcasts to all selected agents -->
    <BulkInjectBar
      v-if="filteredAgents.length"
      class="bulk-inject-host"
      :selected-names="bulkInjectTargets"
      :on-submit="bulkInject"
    />

    <!-- Delete Confirmation Modal -->
    <ConfirmModal
      :visible="deleteModal.visible"
      :title="t('teamMonitor.removeAgentTitle')"
      :confirm-text="t('teamMonitor.removeAgentConfirm')"
      variant="danger"
      :loading="deleteModal.loading"
      :error="deleteModal.error"
      @confirm="confirmDelete"
      @cancel="cancelDelete"
    >
      {{ t('teamMonitor.removeAgentLead') }} <strong>{{ deleteModal.agentName }}</strong>{{ t('teamMonitor.removeAgentTail') }}
    </ConfirmModal>

    <!-- Bulk Delete Confirmation Modal -->
    <ConfirmModal
      :visible="bulkDeleteModal.visible"
      :title="t('teamMonitor.bulkDeleteTitle', { n: bulkDeleteModal.names.length })"
      :confirm-text="t('teamMonitor.bulkDeleteConfirm', { n: bulkDeleteModal.names.length })"
      variant="danger"
      :loading="bulkDeleteModal.loading"
      :error="bulkDeleteModal.error"
      @confirm="confirmBulkDelete"
      @cancel="cancelBulkDelete"
    >
      <p>{{ t('teamMonitor.bulkDeleteBody') }}</p>
      <ul class="bulk-delete-list">
        <li v-for="n in bulkDeleteModal.names" :key="n">{{ n }}</li>
      </ul>
      <!-- Built-in agents inside the selection are surfaced explicitly so
           the user understands why the delete count is smaller than
           their tick count — never silently dropped. -->
      <div v-if="bulkDeleteModal.protected.length" class="bulk-delete-protected">
        <span class="bulk-delete-protected-icon">🔒</span>
        <div>
          <strong>{{ t('teamMonitor.builtinKept', { n: bulkDeleteModal.protected.length }) }}</strong>
          <span class="bulk-delete-protected-names">{{ bulkDeleteModal.protected.join(', ') }}</span>
        </div>
      </div>
    </ConfirmModal>
  </div>
</template>

<style scoped>
.team-monitor {
  max-width: 1600px;
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
  color: var(--text-heading, var(--text));
  margin: 0;
}

.monitor-subtitle {
  font-size: 12px;
  color: var(--text-sub, var(--text-muted));
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

.stat-total { background: var(--primary-bg-strong); color: var(--text-dim); }
/* Aligned with the canonical status palette — running=green (active),
   idle=gray (quiet). Old map (running=amber/idle=green) contradicted the
   design tokens and the rest of the app. */
.stat-running { background: var(--success-bg); color: var(--success); }
.stat-idle    { background: var(--bg-3);       color: var(--text-muted); }
.stat-error   { background: var(--danger-bg);  color: var(--danger); }

/* ─── Bulk delete: outline-danger button per design system ─── */
.bulk-delete-btn {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  height: 32px;
  padding: 0 12px;
  background: var(--bg-button);
  border: 1px solid var(--border);
  border-radius: 8px;
  color: var(--text-dim);
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  user-select: none;
  transition: border-color 0.15s ease, color 0.15s ease, background 0.15s ease;
}

.bulk-delete-btn:not(:disabled):hover {
  border-color: var(--negative-red);
  color: var(--negative-red);
}

.bulk-delete-btn:not(:disabled):active {
  background: rgba(255, 69, 96, 0.08);
}

.bulk-delete-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.bulk-delete-count {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 20px;
  height: 20px;
  padding: 0 6px;
  background: var(--negative-red);
  color: #fff;
  font-size: 11px;
  font-weight: 600;
  line-height: 1;
  border-radius: 10px;
}

/* Confirm-modal body list */
.bulk-delete-list {
  list-style: disc;
  padding-left: 20px;
  margin-top: 8px;
  max-height: 240px;
  overflow-y: auto;
  font-size: 12px;
  color: var(--text-dim);
}

.bulk-delete-list li {
  padding: 2px 0;
}

/* ─── Filter Bar ─── */
.filter-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 20px;
}

/* Row of pills: All / Active / team filters */
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
  background: var(--bg-2);
  color: var(--text-muted);
  border: 1px solid var(--border);
  cursor: pointer;
  transition: all 0.2s;
  white-space: nowrap;
  flex-shrink: 0;
}

.filter-btn:hover {
  background: var(--bg-3);
  color: var(--text-dim);
}

.filter-btn.active {
  background: var(--bg-3);
  color: var(--text);
  border-color: var(--primary);
}

/* Team pill accent on active */
.team-filter-pill.active {
  border-color: var(--team-accent);
  color: var(--team-accent);
  background: color-mix(in srgb, var(--team-accent) 10%, var(--bg-2));
}

.team-pill-label {
  max-width: 100px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
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
  background: var(--bg-2);
  color: var(--text-dim);
  border: 1px solid var(--border);
  cursor: pointer;
  transition: all 0.2s;
  white-space: nowrap;
}

.dropdown-trigger:hover {
  background: var(--bg-3);
  border-color: var(--primary);
  color: var(--text);
}

.dropdown-icon {
  font-size: 12px;
}

.dropdown-caret {
  font-size: 11px;
  transition: transform 0.2s;
  color: var(--text-subtle);
}

.dropdown-caret.open {
  transform: rotate(180deg);
}

.dropdown-panel {
  position: absolute;
  top: calc(100% + 6px);
  right: 0;
  width: 240px;
  background: var(--bg-1);
  border: 1px solid var(--border);
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
  border-bottom: 1px solid var(--border);
}

.dropdown-action {
  font-size: 11px;
  color: var(--primary);
  background: none;
  border: none;
  cursor: pointer;
  padding: 0;
  font-weight: 500;
}

.dropdown-action:hover {
  color: var(--primary-hover);
}

.dropdown-divider {
  color: var(--primary);
  font-size: 11px;
}

.dropdown-search {
  position: relative;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
}

.dropdown-search-input {
  width: 100%;
  background: var(--bg-4);
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  color: var(--text);
  font-size: 12px;
  padding: 6px 26px 6px 10px;
  outline: none;
  transition: border-color 0.15s;
}

.dropdown-search-input::placeholder {
  color: var(--text-muted);
}

.dropdown-search-input:focus {
  border-color: var(--primary);
}

.dropdown-search-clear {
  position: absolute;
  right: 16px;
  top: 50%;
  transform: translateY(-50%);
  background: none;
  border: none;
  color: var(--text-muted);
  font-size: 16px;
  line-height: 1;
  cursor: pointer;
  padding: 0 4px;
}

.dropdown-search-clear:hover {
  color: var(--text);
}

.dropdown-empty {
  padding: 14px 12px;
  font-size: 12px;
  color: var(--text-muted);
  text-align: center;
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
  color: var(--text-muted);
}

.dropdown-item:hover {
  background: rgba(30, 34, 51, 0.6);
}

.dropdown-item.checked {
  color: var(--text);
}

.dropdown-item input[type="checkbox"] {
  appearance: none;
  width: 14px;
  height: 14px;
  border-radius: 3px;
  border: 1.5px solid var(--primary);
  background: var(--bg-2);
  cursor: pointer;
  position: relative;
  flex-shrink: 0;
}

.dropdown-item input[type="checkbox"]:checked {
  background: var(--primary);
  border-color: var(--primary);
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

.dropdown-item-builtin {
  margin-left: auto;
  font-size: 11px;
  opacity: 0.6;
  flex-shrink: 0;
  cursor: help;
}

/* Confirm modal: protected (built-in) agents notice */
.bulk-delete-protected {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  margin-top: 12px;
  padding: 10px 12px;
  background: rgba(245, 158, 11, 0.08);
  border: 1px solid rgba(245, 158, 11, 0.25);
  border-radius: 6px;
  font-size: 12px;
  color: var(--text-dim);
}

.bulk-delete-protected-icon {
  font-size: 14px;
  line-height: 1.2;
  flex-shrink: 0;
}

.bulk-delete-protected-names {
  display: block;
  margin-top: 2px;
  color: var(--text-muted);
  font-size: 11px;
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
  background: var(--border);
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
  color: var(--text-subtle);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  cursor: pointer;
  user-select: none;
}

.dropdown-team-header:hover {
  color: var(--text-muted);
}

.dropdown-team-count {
  margin-left: auto;
  font-size: 10px;
  font-weight: 500;
  color: var(--text-subtle);
  background: var(--border);
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
  color: var(--text-subtle);
  font-size: 14px;
}

/* ─── Lifecycle + Bulk inject ─── */
.lifecycle-bar-host {
  margin-bottom: 12px;
}
.bulk-inject-host {
  margin-top: 14px;
  position: sticky;
  /* Lift above mini audio player when one is visible. --mini-player-h
     is updated by AppLayout (0 / 64px). On desktop the player sits
     at viewport bottom so .bulk-inject-host's sticky parent must clear
     it too. Without this offset, the bottom of the inject bar was
     getting hidden behind the mini-player's top border. */
  bottom: var(--mini-player-h, 0px);
  z-index: 5;
  /* Backdrop keeps the bar legible when grid content scrolls underneath. */
  backdrop-filter: blur(4px);
}

/* ─── Agent Grid — 2×2 (auto-fit at narrow widths) ─── */
.agent-grid {
  display: grid;
  /* 2×2 on wide screens; auto-fit on narrow ensures we never break the
     terminal cards by squeezing more than two columns. */
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}

@media (max-width: 1100px) {
  .agent-grid {
    grid-template-columns: 1fr;
  }
}

/* ─── Mobile overrides ─── */
@media (max-width: 767px) {
  .team-monitor {
    max-width: 100%;
  }

  /* The global mobile app-bar already shows "Team Monitor"; hide the
     duplicate in-page title + tagline to give the activity stream more room.
     The stats/Delete controls below stay. */
  .monitor-title,
  .monitor-subtitle {
    display: none;
  }

  /* Header: stack title + stats vertically */
  .monitor-header {
    flex-direction: column;
    align-items: flex-start;
    gap: 8px;
    margin-bottom: 0;
    padding: 10px 14px;
    border-bottom: 1px solid var(--border);
    background: var(--bg-1);
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

  /* ── Filter bar: 2-row stacked layout on mobile ── */
  .filter-bar {
    flex-direction: column;
    align-items: stretch;
    gap: 0;
    margin-bottom: 0;
    padding: 0;
    background: var(--bg-1);
    border-bottom: 1px solid var(--border);
  }

  /* Row 1: scrollable pills */
  .filter-pills-row {
    display: flex;
    flex-wrap: nowrap;
    gap: 6px;
    padding: 8px 12px;
    overflow-x: auto;
    scrollbar-width: none;
    border-bottom: 1px solid var(--bg-2);
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
    padding: 4px 0;        /* trim wrapper height (was 8px 12px) */
    box-sizing: border-box;
  }

  .dropdown-trigger {
    width: 100%;
    justify-content: space-between;
    font-size: 12.5px;
    padding: 6px 12px;     /* shorter on mobile (was 9px 14px) */
    border-radius: 8px;
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

  /* Bulk delete button: full width on mobile */
  .bulk-delete-btn {
    width: 100%;
    justify-content: center;
  }

  /* Inject bar: inline at the end of the stream, NOT sticky. Sticky honoured
     app-main's content padding-bottom (tab bar + safe-area + FAB band), so it
     pinned ~136px above the viewport bottom — i.e. floating mid-screen over the
     agent stream, worst of all when the FABs are hidden (the reserved FAB band
     becomes dead space below it). Inline flow puts it after the last agent
     card; the content padding still clears the tab bar + FABs underneath it. */
  .bulk-inject-host {
    position: static;
    backdrop-filter: none;
  }
}

</style>




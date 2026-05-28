<script setup>
/**
 * AgentsList — new design (Track 4 redesign).
 *
 * Layout:
 *   1. Orbit hero (3-tier ring around Jarvis)
 *   2. Tree / Flow view toggle (segmented control)
 *   3. Tree (default): indented file-explorer of the agent hierarchy
 *      OR Flow: Sankey-style orchestration diagram
 *
 * All data comes from the agents store — DO NOT mutate it.
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAgentsStore } from '../stores/agents'
import { useChatStore } from '../stores/chat'
import { apiFetch } from '../api'
import { useToast } from '../composables/useToast'
import { useConfirm } from '../composables/useConfirm'
import { buildOrbitGroups, classifyAgent } from '../components/agent/agentMeta.js'
import OrbitHero from '../components/agent/OrbitHero.vue'
import AgentTreeRow from '../components/agent/AgentTreeRow.vue'
import AgentFlow from '../components/agent/AgentFlow.vue'

const store = useAgentsStore()
const chatStore = useChatStore()
const router = useRouter()
const toast = useToast()
const { confirm } = useConfirm()

// 'tree' | 'flow' — tree default per design.
const viewMode = ref('tree')
// 'all' | 'core' | 'spawned' | 'teams' — which ring is highlighted.
const activeRing = ref('all')
const searchQuery = ref('')
// Teams collapsed in Tree view. Set of team names; absent = expanded.
const collapsedTeams = ref(new Set())
function toggleTeamCollapse(teamName) {
  const s = new Set(collapsedTeams.value)
  if (s.has(teamName)) s.delete(teamName); else s.add(teamName)
  collapsedTeams.value = s
}

onMounted(() => { store.fetchAgents() })

const orbitGroups = computed(() => buildOrbitGroups(store.agentsList))

const filteredAgents = computed(() => {
  const q = searchQuery.value.trim().toLowerCase()
  if (!q) return store.agentsList
  return store.agentsList.filter(a =>
    (a.name || '').toLowerCase().includes(q) ||
    (a.model || '').toLowerCase().includes(q) ||
    (a.team_name || '').toLowerCase().includes(q) ||
    (a.role || '').toLowerCase().includes(q),
  )
})

/**
 * Build the indented tree rows for the Tree view.
 *
 * Hierarchy:
 *   Jarvis (depth 0)
 *   ├── built-ins (depth 1)
 *   ├── spawned/card non-team (depth 1)
 *   ├── team PM (depth 1)
 *   │     └── team member (depth 2)
 */
const treeRows = computed(() => {
  const rows = []
  const groups = orbitGroups.value
  const conductor = groups.conductor
  // Filter targets while building so the search applies but the tree
  // structure stays intact (a member match still shows its PM).
  const q = searchQuery.value.trim().toLowerCase()
  const hit = (a) => {
    if (!q) return true
    return (
      (a.name || '').toLowerCase().includes(q) ||
      (a.model || '').toLowerCase().includes(q) ||
      (a.team_name || '').toLowerCase().includes(q) ||
      (a.role || '').toLowerCase().includes(q)
    )
  }

  if (conductor) {
    rows.push({
      agent: conductor,
      depth: 0,
      isLast: false,
      parentLines: [],
      isOrchestrator: true,
    })
  }

  // Helper: gather depth-1 children of jarvis. Last sibling at this
  // level needs isLast=true so the tree draws an L-joint, not a T.
  const depth1 = []
  if (activeRing.value === 'all' || activeRing.value === 'core') {
    for (const a of groups.core) if (hit(a)) depth1.push({ agent: a, kind: 'core' })
  }
  if (activeRing.value === 'all' || activeRing.value === 'spawned') {
    for (const a of groups.spawned) if (hit(a)) depth1.push({ agent: a, kind: 'spawned' })
  }
  if (activeRing.value === 'all' || activeRing.value === 'teams') {
    for (const team of groups.teams) {
      // Show the team if PM or any member matches search.
      const teamMatches = (team.members || []).some(hit) || (team.pm && hit(team.pm)) || hit({ team_name: team.name })
      if (teamMatches) depth1.push({ agent: team.pm || team.members?.[0], kind: 'team', team })
    }
  }

  depth1.forEach((entry, idx) => {
    const isLast = idx === depth1.length - 1
    const isTeam = entry.kind === 'team'
    const members = isTeam ? (entry.team.members || []).filter(m => m !== entry.team.pm) : []
    const teamName = isTeam ? entry.team.name : null
    const isCollapsed = isTeam && collapsedTeams.value.has(teamName)
    rows.push({
      agent: entry.agent,
      depth: 1,
      isLast,
      parentLines: [false], // depth 0 (jarvis) is always last
      isOrchestrator: isTeam,
      hasChildren: isTeam && members.length > 0,
      isCollapsed,
      teamName,
    })
    if (isTeam && !isCollapsed) {
      members.forEach((m, j) => {
        const memberLast = j === members.length - 1
        rows.push({
          agent: m,
          depth: 2,
          isLast: memberLast,
          parentLines: [false, !isLast],
          isOrchestrator: false,
          hasChildren: false,
          isCollapsed: false,
        })
      })
    }
  })

  return rows
})

// Flow view: midNodes = core + spawned (non-team), teams = teams array.
// Mirror the tree-view ``hit()`` predicate so search filters Flow too.
// Without this, the search input only affects Tree view (we forgot to
// pipe filteredAgents into the Flow computeds).
function _matches(a, q) {
  if (!q) return true
  return (
    (a?.name || '').toLowerCase().includes(q) ||
    (a?.model || '').toLowerCase().includes(q) ||
    (a?.team_name || '').toLowerCase().includes(q) ||
    (a?.role || '').toLowerCase().includes(q)
  )
}

const flowMidNodes = computed(() => {
  const q = searchQuery.value.trim().toLowerCase()
  const pick = (list) => list.filter(a => _matches(a, q))
  if (activeRing.value === 'core') return pick(orbitGroups.value.core)
  if (activeRing.value === 'spawned') return pick(orbitGroups.value.spawned)
  if (activeRing.value === 'teams') return []
  return pick([...orbitGroups.value.core, ...orbitGroups.value.spawned])
})

const flowTeams = computed(() => {
  if (activeRing.value === 'core' || activeRing.value === 'spawned') return []
  const q = searchQuery.value.trim().toLowerCase()
  if (!q) return orbitGroups.value.teams
  // Show a team if PM, any member, or the team name itself matches.
  return orbitGroups.value.teams.filter(team =>
    (team.name || '').toLowerCase().includes(q) ||
    (team.pm && _matches(team.pm, q)) ||
    (team.members || []).some(m => _matches(m, q)),
  )
})

// ── Row actions ──────────────────────────────────────────────────────
// Chat: set the chat store's active agent then navigate. ChatView's
// watcher will pick it up; doing it BEFORE push avoids the brief
// "Jarvis" flash before our preference applies.
function onRowChat(agent) {
  chatStore.setActiveAgent(agent.name)
  router.push('/chat')
}

// Pause/resume toggle: backend is single-endpoint per direction, so
// dispatch by current status. Surface backend's PauseProtected
// (HTTP 409 with approval_id) as an actionable toast — silently
// swallowing it would leave the user wondering why the button did
// nothing for a pause-locked agent.
async function onRowPauseToggle(agent) {
  const paused = (agent.status || '') === 'paused'
  const path = paused ? 'resume' : 'pause'
  try {
    await apiFetch(`/api/agents/${encodeURIComponent(agent.name)}/${path}`, {
      method: 'POST',
    })
    toast.success(paused ? `${agent.name} resumed` : `${agent.name} paused`, {
      duration: 2500,
    })
  } catch (e) {
    const detail = e?.body?.detail
    if (detail?.error === 'approval_pause_lock') {
      toast.warning(`${agent.name} is held by a pending approval`, {
        description: 'Resolve the approval before resuming.',
        duration: 6000,
      })
    } else {
      toast.error(`${path} failed`, {
        description: e?.message || String(e),
        duration: 5000,
      })
    }
  }
}

// Delete: confirm modal first — destructive. Static agents are
// disabled client-side (canDelete=false in the row), so this only
// fires for dynamic / spawned. Backend still enforces 403 as defence
// in depth; surface that path too in case the client check goes stale.
async function onRowDelete(agent) {
  const proceed = await confirm({
    title: `Delete ${agent.name}?`,
    message:
      `This removes ${agent.name} from the registry. ` +
      'Spawned subagents will be killed. This cannot be undone.',
    confirmText: 'Delete',
    cancelText: 'Keep',
    variant: 'danger',
  })
  if (!proceed) return
  try {
    await apiFetch(`/api/agents/${encodeURIComponent(agent.name)}`, {
      method: 'DELETE',
    })
    toast.success(`${agent.name} deleted`, { duration: 2500 })
    // Refresh roster — agent_removed SSE event will eventually flow,
    // but refetch is cheaper than waiting and avoids the gap where
    // the row still renders after success.
    store.fetchAgents()
  } catch (e) {
    toast.error('Delete failed', {
      description: e?.body?.detail || e?.message || String(e),
      duration: 5000,
    })
  }
}

// ── Search applies to the orbit hero too ─────────────────────────────
// Previously the search input only filtered the Tree / Flow lists,
// leaving the OrbitHero showing every agent. Building a filtered
// orbitGroups means the same search predicate covers all three views.
// Conductor is NEVER hidden — losing Jarvis from the centre would
// confuse the "Conductor in the middle" mental model the page sells.
const filteredOrbitGroups = computed(() => {
  const q = searchQuery.value.trim().toLowerCase()
  if (!q) return orbitGroups.value
  const hit = (a) => _matches(a, q)
  const g = orbitGroups.value
  return {
    conductor: g.conductor,
    core: g.core.filter(hit),
    spawned: g.spawned.filter(hit),
    teams: g.teams.filter(team =>
      (team.name || '').toLowerCase().includes(q) ||
      (team.pm && hit(team.pm)) ||
      (team.members || []).some(hit),
    ),
  }
})

// Header counts
const counts = computed(() => ({
  total: store.stats.total,
  running: store.stats.running,
  idle: store.stats.idle,
  error: store.stats.error,
}))
</script>

<template>
  <div class="agents-page jv">
    <!-- Page header -->
    <header class="page-header">
      <div class="header-text">
        <div class="eyebrow">WORKSPACE · AGENTS</div>
        <h1 class="page-title">
          Agents · <span class="grad" style="font-style: italic">orchestration tree</span>
        </h1>
        <p class="page-subtitle">
          Jarvis at the center, static workers and spawned teams orbiting outward.
        </p>
      </div>

      <div class="header-stats">
        <span class="chip chip-muted">{{ counts.total }} agents</span>
        <span class="chip chip-success">{{ counts.running }} running</span>
        <span class="chip chip-muted">{{ counts.idle }} idle</span>
        <span v-if="counts.error" class="chip chip-danger">{{ counts.error }} errored</span>
      </div>
    </header>

    <!-- Loading -->
    <div v-if="store.isLoading && !store.agentsList.length" class="state-center">
      <span class="loading-text">Loading agents…</span>
    </div>

    <template v-else>
      <!-- Orbit hero — bind to filteredOrbitGroups so the search
           toolbar input filters the diagram in lockstep with the
           Tree / Flow lists below. -->
      <OrbitHero
        :conductor="filteredOrbitGroups.conductor"
        :core="filteredOrbitGroups.core"
        :spawned="filteredOrbitGroups.spawned"
        :teams="filteredOrbitGroups.teams"
        :active-ring="activeRing"
      />

      <!-- Toolbar: ring filter pills + view toggle -->
      <div class="toolbar">
        <div class="ring-pills">
          <button
            class="ring-pill"
            :class="{ active: activeRing === 'all' }"
            @click="activeRing = 'all'"
          >All</button>
          <button
            class="ring-pill"
            :class="{ active: activeRing === 'core' }"
            @click="activeRing = 'core'"
          >Core</button>
          <button
            class="ring-pill"
            :class="{ active: activeRing === 'spawned' }"
            @click="activeRing = 'spawned'"
          >Spawned</button>
          <button
            class="ring-pill"
            :class="{ active: activeRing === 'teams' }"
            @click="activeRing = 'teams'"
          >Teams</button>
        </div>

        <input
          v-model="searchQuery"
          type="text"
          class="tb-search"
          placeholder="Search by name, role, team, model…"
        />

        <div class="seg view-toggle" role="tablist" aria-label="View mode">
          <button
            role="tab"
            :class="{ 'is-active': viewMode === 'tree' }"
            :aria-selected="viewMode === 'tree'"
            @click="viewMode = 'tree'"
          >☰ Tree</button>
          <button
            role="tab"
            :class="{ 'is-active': viewMode === 'flow' }"
            :aria-selected="viewMode === 'flow'"
            @click="viewMode = 'flow'"
          >⇄ Flow</button>
        </div>
      </div>

      <!-- Empty -->
      <div v-if="!filteredAgents.length" class="empty-state">
        <div class="empty-icon">∅</div>
        <div class="empty-title">No agents match this view</div>
        <div class="empty-sub">
          {{ searchQuery ? 'Try a different search term' : 'Spawn a team or open Settings to add agents' }}
        </div>
      </div>

      <!-- Tree view -->
      <div v-else-if="viewMode === 'tree'" class="tree-host">
        <div class="tree-header">
          <span />
          <span />
          <span />
          <span>NAME · ORCHESTRATED BY</span>
          <span>MODEL</span>
          <span>STATUS</span>
          <span style="text-align: right">TOKENS</span>
          <span style="text-align: right">LAST</span>
          <span style="text-align: right">ACTIVITY</span>
          <span style="text-align: right">ACTIONS</span>
        </div>
        <div class="tree-body">
          <AgentTreeRow
            v-for="row in treeRows"
            :key="row.agent.name + '-' + row.depth"
            :agent="row.agent"
            :depth="row.depth"
            :is-last="row.isLast"
            :parent-lines="row.parentLines"
            :is-orchestrator="row.isOrchestrator"
            :has-children="row.hasChildren"
            :is-collapsed="row.isCollapsed"
            @toggle="row.teamName && toggleTeamCollapse(row.teamName)"
            @chat="onRowChat"
            @pause-toggle="onRowPauseToggle"
            @delete="onRowDelete"
          />
        </div>
      </div>

      <!-- Flow view -->
      <AgentFlow
        v-else
        :conductor="orbitGroups.conductor"
        :mid-nodes="flowMidNodes"
        :teams="flowTeams"
      />
    </template>
  </div>
</template>

<style scoped>
.agents-page {
  display: flex;
  flex-direction: column;
  gap: 18px;
  max-width: 1280px;
  /* `.jv` scope inside this view lights up the shared atomic styles
     (chip, seg, eyebrow, grad) without forcing the whole AppLayout
     into the new theme. */
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  gap: 16px;
  flex-wrap: wrap;
}
.page-title {
  font-family: var(--font-display);
  font-size: 22px;
  font-weight: 600;
  color: var(--text);
  margin: 4px 0 2px;
  letter-spacing: -0.02em;
}
.page-subtitle {
  font-size: 12.5px;
  color: var(--text-dim);
  margin: 0;
}
.header-stats {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.state-center {
  display: flex;
  justify-content: center;
  padding: 60px 0;
}
.loading-text {
  font-size: 13px;
  color: var(--text-muted);
  animation: textPulse 1.4s ease-in-out infinite;
}
@keyframes textPulse { 0%, 100% { opacity: 1 } 50% { opacity: 0.4 } }

/* Toolbar */
.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  padding: 8px 12px;
  background: var(--bg-1);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
}
.ring-pills {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.ring-pill {
  height: 28px;
  padding: 0 12px;
  background: var(--bg-2);
  border: 1px solid var(--border-strong);
  color: var(--text-dim);
  font-size: 12px;
  border-radius: var(--r-sm);
  cursor: pointer;
  transition: all 0.15s var(--ease-out);
}
.ring-pill:hover { background: var(--bg-3); color: var(--text); }
.ring-pill.active {
  background: var(--primary-bg-strong);
  color: var(--primary-hover);
  border-color: var(--primary-bg-strong);
}

.tb-search {
  flex: 1;
  min-width: 220px;
  height: 30px;
  padding: 0 12px;
  background: var(--bg-3);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-sm);
  font-size: 12.5px;
  color: var(--text);
  outline: none;
  font-family: var(--font-body);
}
.tb-search::placeholder { color: var(--text-muted); }
.tb-search:focus { border-color: var(--primary); }

.view-toggle { flex-shrink: 0; }

/* Tree */
.tree-host {
  background: var(--bg-1);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  overflow: hidden;
}
.tree-header {
  display: grid;
  /* Last 96px column = ACTIONS, mirrors AgentTreeRow.tree-row grid. */
  grid-template-columns: auto 16px 14px minmax(220px, 1fr) 130px 86px 60px 46px 60px 96px;
  gap: 10px;
  padding: 8px 18px;
  font-family: var(--font-mono);
  font-size: 9.5px;
  letter-spacing: 0.10em;
  color: var(--text-subtle);
  text-transform: uppercase;
  background: var(--bg-2);
  border-bottom: 1px solid var(--border);
}
.tree-body {
  display: flex;
  flex-direction: column;
}

/* Empty */
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 60px 0;
  text-align: center;
}
.empty-icon {
  font-size: 36px;
  color: var(--text-subtle);
  margin-bottom: 10px;
}
.empty-title {
  font-size: 14px;
  color: var(--text-dim);
  margin-bottom: 4px;
}
.empty-sub { font-size: 12px; color: var(--text-muted); }

@media (max-width: 767px) {
  .agents-page { gap: 12px; }
  .toolbar { padding: 8px; gap: 8px; }
  .tb-search { width: 100%; min-width: 0; }
  .tree-header {
    /* Match AgentTreeRow phone layout: 5 visible cols
       — tree-lines | toggle | dot | NAME | ACTIONS.
       STATUS / LAST / MODEL / TOKENS / ACTIVITY hidden on phone (status
       conveyed by the colored dot). */
    grid-template-columns: auto 16px 14px minmax(0, 1fr) auto;
    padding: 8px 12px;
    gap: 8px;
  }
  /* Hide MODEL (5), STATUS (6), TOKENS (7), LAST (8), ACTIVITY (9). The
     remaining visible children are 1–4 (spacers + NAME) and 10 (ACTIONS).
     Without hiding STATUS/LAST here, the header rendered 7 spans into a
     5-column grid → "NAMSTATUS" text overlap bug. */
  .tree-header > :nth-child(5),
  .tree-header > :nth-child(6),
  .tree-header > :nth-child(7),
  .tree-header > :nth-child(8),
  .tree-header > :nth-child(9) {
    display: none;
  }
}

@media (max-width: 480px) {
  /* Hide the redundant total / idle chips on iPhone-class viewports —
     the OrbitHero legend below already surfaces those counts and the
     chips were eating a 2-3 line row of header chrome before any
     agent became visible. Keep the running + errored chips since
     they're action-worthy. */
  .header-stats > .chip-muted:nth-of-type(1),
  .header-stats > .chip-muted:nth-of-type(2) {
    display: none;
  }
}
</style>

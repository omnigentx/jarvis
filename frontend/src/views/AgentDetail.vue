<script setup>
import { ref, reactive, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAgentsStore } from '../stores/agents'
import { apiFetch } from '../api'
import { useConfirm } from '../composables/useConfirm'
import StatusBadge from '../components/StatusBadge.vue'
import MarkdownRenderer from '../components/MarkdownRenderer.vue'
import SkillEditorModal from '../components/agent/SkillEditorModal.vue'
import SkillDeleteModal from '../components/agent/SkillDeleteModal.vue'
import AgentMemoryPanel from './AgentMemoryPanel.vue'
import {
  roleAvaClass,
  roleColorToken,
  avatarGlyph,
} from '../components/agent/agentMeta.js'

const { confirm } = useConfirm()

const route = useRoute()
const router = useRouter()
const store = useAgentsStore()
const agentDetail = ref(null)
const validTabs = ['overview', 'skills', 'servers', 'instruction', 'context', 'versions', 'memory', 'activity']
const initialTab = validTabs.includes(route.query.tab) ? route.query.tab : 'overview'
const activeTab = ref(initialTab)
const isLoading = ref(true)
const expandedServers = reactive({})
const expandedTools = reactive({})
const expandedSkills = reactive({})

// Timeline (persistent history)
const timelineEvents = ref([])
const timelineLoading = ref(false)
const timelineFetched = ref(false)

// Context window snapshots
const contextSnapshots = ref([])
const contextLoading = ref(false)
const contextFetched = ref(false)
const expandedSnapshot = ref(null)
const snapshotMessages = ref([])
const messagesLoading = ref(false)
const expandedMessages = ref(new Set())

const agentName = computed(() => route.params.name)
const liveAgent = computed(() => store.agents.get(agentName.value))

async function fetchAgentDetail() {
  try {
    agentDetail.value = await apiFetch(`/api/agents/${agentName.value}`)
  } catch (e) {
    console.error('Failed to load agent detail:', e)
  } finally {
    isLoading.value = false
  }
}

onMounted(() => {
  fetchAgentDetail()
})

// Push-based refresh: when SpawnProgressBridge finishes persisting an agent's
// runtime introspection it broadcasts `runtime_config_ready` over the global
// activity stream. AppLayout already opens that EventSource at boot and routes
// events through agentsStore.recentEvents, so we just listen here.
//
// We walk newly-prepended events from the head until the last one we already
// processed instead of just reading events[0]. Vue coalesces same-tick array
// mutations, so if a runtime_config_ready and an unrelated event land in the
// same microtask, an events[0] check would miss the one that isn't last.
let _lastSeenEvent = null
const _stopRuntimeWatch = watch(
  () => store.recentEvents,
  (events) => {
    if (!events?.length) return
    let hit = false
    for (let i = 0; i < events.length; i++) {
      if (events[i] === _lastSeenEvent) break
      const ev = events[i]
      if (
        ev?.event_type === 'runtime_config_ready'
        && ev?.agent_name === agentName.value
      ) {
        hit = true
      }
    }
    _lastSeenEvent = events[0]
    if (hit) fetchAgentDetail()
  },
  { flush: 'post' },
)
onUnmounted(() => _stopRuntimeWatch())

const tabs = [
  { id: 'overview', label: 'Overview' },
  { id: 'skills', label: 'Skills' },
  { id: 'servers', label: 'MCP Servers' },
  { id: 'instruction', label: 'Instruction' },
  { id: 'context', label: 'Context Window' },
  { id: 'versions', label: 'Context Versions' },
  { id: 'memory', label: 'Memory' },
  { id: 'activity', label: 'History' },
]

// Merge REST detail with live SSE state
const agent = computed(() => {
  if (!agentDetail.value) return null
  return {
    ...agentDetail.value,
    status: liveAgent.value?.status || agentDetail.value.status || 'idle',
    lastAction: liveAgent.value?.lastAction || null,
    lastError: liveAgent.value?.lastError || null,
  }
})

// Agent initial letter for avatar
const agentInitial = computed(() => (agent.value?.name || '?')[0].toUpperCase())

// Instruction preview (first 12 lines)
const instructionPreview = computed(() => {
  const instr = agent.value?.instruction || ''
  const lines = instr.split('\n')
  if (lines.length <= 12) return instr
  return lines.slice(0, 12).join('\n')
})
const instructionLineCount = computed(() => {
  const instr = agent.value?.instruction || ''
  return instr.split('\n').length
})
const hasMoreInstruction = computed(() => instructionLineCount.value > 12)

// Recent SSE events for this specific agent (live, ephemeral)
const liveEvents = computed(() =>
  store.recentEvents.filter(e => e.agent_name === agentName.value).slice(0, 20)
)

// Merged history: persistent timeline + live SSE (deduped)
const mergedHistory = computed(() => {
  // Normalize timeline events
  const persistent = timelineEvents.value.map(e => ({
    id: `${e.source}_${e.timestamp}_${e.type}`,
    source: e.source,
    event_type: e.type,
    agent: e.agent,
    message: e.content,
    timestamp: e.timestamp,
    metadata: e.metadata || {},
  }))

  // Normalize live SSE events
  const live = liveEvents.value.map(e => ({
    id: `sse_${e.timestamp}_${e.event_type}`,
    source: 'sse',
    event_type: e.event_type,
    agent: e.agent_name,
    message: e.message,
    timestamp: e.timestamp,
    metadata: {},
  }))

  // Merge and dedupe by id
  const seen = new Set()
  const all = [...live, ...persistent]
  return all
    .filter(e => { if (seen.has(e.id)) return false; seen.add(e.id); return true })
    .sort((a, b) => (b.timestamp || 0) - (a.timestamp || 0))
    .slice(0, 50)
})

// Fetch persistent timeline from backend
async function fetchTimeline() {
  if (timelineFetched.value || timelineLoading.value) return
  timelineLoading.value = true
  try {
    const data = await apiFetch(`/api/agent/timeline?agent=${agentName.value}&limit=50`)
    timelineEvents.value = data.events || []
    timelineFetched.value = true
  } catch (e) {
    console.error('Failed to load timeline:', e)
  } finally {
    timelineLoading.value = false
  }
}

// Lazy load timeline when History tab is activated
watch(activeTab, (tab) => {
  if (tab === 'activity') fetchTimeline()
  if (tab === 'context') fetchContextSnapshots()
  if (tab === 'versions') fetchContextVersions()
})

// ── Context Versions (compaction timeline) ──
const contextVersions = ref([])
const versionsLoading = ref(false)
const versionsFetched = ref(false)
const versionsError = ref(false)
const versionDetails = reactive({}) // event_id → detail payload (summary/plan)
const versionDiffs = reactive({})   // event_id → diff payload
const expandedVersionId = ref(null)
const versionPanel = ref('summary') // 'summary' | 'diff'

async function fetchContextVersions({ force = false } = {}) {
  if ((versionsFetched.value && !force) || versionsLoading.value) return
  versionsLoading.value = true
  versionsError.value = false
  try {
    // No explicit limit — the backend applies the user's
    // snapshot_versions_visible setting as the default.
    const data = await apiFetch(`/api/agents/${agentName.value}/context/versions`)
    contextVersions.value = data.versions || []
    versionsFetched.value = true
  } catch (e) {
    console.error('Failed to load context versions:', e)
    // Distinct from the empty state — an unreachable backend must not
    // read as "no compactions yet" (PR #85 review F6).
    versionsError.value = true
  } finally {
    versionsLoading.value = false
  }
}

// Live refresh: when a compaction completes while the user is on the
// versions tab, the SSE event flips liveAgent.compaction — refetch so the
// new version appears without a manual reload.
watch(
  () => liveAgent.value?.compaction?.last?.timestamp,
  (ts, old) => {
    if (ts && ts !== old && activeTab.value === 'versions') {
      fetchContextVersions({ force: true })
    }
  },
)

async function toggleVersion(version, panel = 'summary') {
  if (expandedVersionId.value === version.id && versionPanel.value === panel) {
    expandedVersionId.value = null
    return
  }
  expandedVersionId.value = version.id
  versionPanel.value = panel
  // Errors are never cached: a failed lazy-load retries on the next
  // expand (or via the explicit Retry button) instead of sticking.
  if (panel === 'summary' && (!versionDetails[version.id] || versionDetails[version.id].error)) {
    delete versionDetails[version.id]
    try {
      versionDetails[version.id] = await apiFetch(
        `/api/agents/${agentName.value}/context/versions/${version.id}`,
      )
    } catch (e) {
      console.error('Failed to load version detail:', e)
      versionDetails[version.id] = { error: true }
    }
  }
  if (panel === 'diff' && (!versionDiffs[version.id] || versionDiffs[version.id].error)) {
    delete versionDiffs[version.id]
    try {
      versionDiffs[version.id] = await apiFetch(
        `/api/agents/${agentName.value}/context/versions/${version.id}/diff`,
      )
    } catch (e) {
      console.error('Failed to load version diff:', e)
      versionDiffs[version.id] = { error: true }
    }
  }
}

function retryVersionPanel(version) {
  // Force re-entry into the same panel: clear the collapse short-circuit
  // by nulling the expansion first.
  const panel = versionPanel.value
  expandedVersionId.value = null
  toggleVersion(version, panel)
}

function savingsPct(v) {
  return Math.round((v.reduction_ratio || 0) * 100)
}

// Fetch context snapshots from backend
async function fetchContextSnapshots() {
  if (contextFetched.value || contextLoading.value) return
  contextLoading.value = true
  try {
    const data = await apiFetch(`/api/agents/${agentName.value}/context?limit=10`)
    contextSnapshots.value = data.snapshots || []
    contextFetched.value = true
  } catch (e) {
    console.error('Failed to load context snapshots:', e)
  } finally {
    contextLoading.value = false
  }
}

async function toggleSnapshotMessages(snapshot) {
  if (expandedSnapshot.value === snapshot.id) {
    expandedSnapshot.value = null
    snapshotMessages.value = []
    return
  }
  expandedSnapshot.value = snapshot.id
  messagesLoading.value = true
  try {
    const data = await apiFetch(`/api/agents/${agentName.value}/context/${snapshot.id}/messages`)
    snapshotMessages.value = data.messages || []
  } catch (e) {
    console.error('Failed to load context messages:', e)
    snapshotMessages.value = []
  } finally {
    messagesLoading.value = false
  }
}

function formatTokens(n) {
  if (!n) return '0'
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M'
  if (n >= 1000) return (n / 1000).toFixed(1) + 'K'
  return String(n)
}

function triggerLabel(trigger) {
  const map = { task_complete: '✅ Task Complete', idle: '💤 Idle', error: '❌ Error', manual: '🔧 Manual' }
  return map[trigger] || trigger
}

function roleClass(role) {
  if (role === 'user' || role === 'Role.USER') return 'role-user'
  if (role === 'assistant' || role === 'Role.ASSISTANT') return 'role-assistant'
  return 'role-system'
}

function roleLabel(role) {
  if (role === 'user' || role === 'Role.USER') return 'User'
  if (role === 'assistant' || role === 'Role.ASSISTANT') return 'Assistant'
  return 'System'
}

function truncateContent(content, maxLen = 300) {
  if (!content || content.length <= maxLen) return content
  return content.slice(0, maxLen) + '…'
}

function toggleMessage(idx) {
  if (expandedMessages.value.has(idx)) {
    expandedMessages.value.delete(idx)
  } else {
    expandedMessages.value.add(idx)
  }
}

// ----- Skill management (CRUD via /api/skills) ---------------------------

const skillMeta = ref(new Map()) // name -> { is_builtin, used_by[] }
const skillsMetaFetched = ref(false)

const editorVisible = ref(false)
const editorMode = ref('edit') // 'edit' | 'create'
const editorTarget = ref('')

const deleteModalVisible = ref(false)
const deleteTarget = ref({ name: '', usedBy: [] })

async function fetchSkillsMeta() {
  try {
    const data = await apiFetch('/api/skills')
    const m = new Map()
    for (const s of data.skills || []) {
      m.set(s.name, { is_builtin: !!s.is_builtin, used_by: s.used_by || [] })
    }
    skillMeta.value = m
    skillsMetaFetched.value = true
  } catch (e) {
    console.error('Failed to load skill metadata:', e)
  }
}

// Tri-state guard: until /api/skills has confirmed `is_builtin: false` for a
// given skill, treat it as builtin. Otherwise the Delete button briefly
// enables for built-in skills during the initial metadata fetch (small race,
// but the user IS faster than a network round-trip on a warm connection).
// This also handles the case where /api/skills failed: deletion stays
// blocked client-side and the server still returns 403 as defence in depth.
function isSkillBuiltin(name) {
  const meta = skillMeta.value.get(name)
  if (!meta) return true
  return meta.is_builtin === true
}
// Whether we have authoritative metadata for this skill yet — drives a
// "loading" tooltip on the delete button before the first fetch completes.
function hasSkillMeta(name) {
  return skillMeta.value.has(name)
}
function skillUsedBy(name) {
  return skillMeta.value.get(name)?.used_by || []
}

function openEditSkill(name) {
  editorMode.value = 'edit'
  editorTarget.value = name
  editorVisible.value = true
}
function openCreateSkill() {
  editorMode.value = 'create'
  editorTarget.value = ''
  editorVisible.value = true
}
function openDeleteSkill(name) {
  deleteTarget.value = { name, usedBy: skillUsedBy(name) }
  deleteModalVisible.value = true
}

async function reloadAgentDetail() {
  // After CRUD, the agent's resolved skills may have changed (content edits;
  // create/delete only matter if user manually wired them into agent cards).
  // Either way, refetch is cheap and keeps the panel honest.
  try {
    agentDetail.value = await apiFetch(`/api/agents/${agentName.value}`)
  } catch (e) {
    console.error('Failed to refresh agent detail:', e)
  }
}

async function onSkillSaved() {
  await Promise.all([fetchSkillsMeta(), reloadAgentDetail()])
  // Modal stays open so the user can see the saved state and keep editing.
}
async function onSkillDeleted() {
  deleteModalVisible.value = false
  // If we were editing the deleted skill, close the editor too.
  if (editorVisible.value && editorTarget.value === deleteTarget.value.name) {
    editorVisible.value = false
  }
  await Promise.all([fetchSkillsMeta(), reloadAgentDetail()])
}

// ----- Attach/detach skills to this agent -------------------------------

const attachPickerOpen = ref(false)
const attachBusy = ref(false)
const attachToast = ref('')

const isAgentCardBased = computed(() => agent.value?.type === 'card')
// "Defined in agent.py" warning ONLY applies to truly built-in agents
// (Jarvis, PersonalAgent, etc). Team/dynamic agents are spawned at runtime
// and their config lives in the spawn registry, not agent.py.
const isBuiltinAgent = computed(() => agent.value?.type === 'builtin')
const isSpawnedAgent = computed(() => {
  const t = agent.value?.type
  return t === 'team' || t === 'dynamic'
})

const attachableSkills = computed(() => {
  // Skills in the global library that aren't already attached to this agent.
  const attached = new Set((agent.value?.skills || []).map((s) => s.name))
  return [...skillMeta.value.entries()]
    .filter(([name]) => !attached.has(name))
    .map(([name, meta]) => ({ name, ...meta }))
})

async function attachSkill(skillName) {
  if (attachBusy.value) return
  if (isBuiltinAgent.value) {
    const proceed = await confirm({
      title: `Attach to ${agentName.value}?`,
      message:
        `${agentName.value} is defined in code (agent.py). Attaching ` +
        `'${skillName}' will only apply at runtime and revert when the ` +
        `backend restarts unless you also add it to ${agentName.value}'s ` +
        `get_skills(...) call.`,
      confirmText: 'Attach (runtime only)',
      variant: 'warning',
    })
    if (!proceed) return
  } else if (isSpawnedAgent.value) {
    const proceed = await confirm({
      title: `Attach to ${agentName.value}?`,
      message:
        `${agentName.value} is a spawned agent. Attaching '${skillName}' ` +
        `only affects the live instance — it will be lost when the agent ` +
        `stops. Edit the team template to persist this change across spawns.`,
      confirmText: 'Attach (runtime only)',
      variant: 'warning',
    })
    if (!proceed) return
  }
  attachBusy.value = true
  attachToast.value = ''
  try {
    const res = await apiFetch(
      `/api/skills/${encodeURIComponent(skillName)}/agents/${encodeURIComponent(agentName.value)}`,
      { method: 'PUT' },
    )
    attachToast.value = res.persisted
      ? `Attached '${skillName}'.`
      : `Attached '${skillName}' — runtime only, reverts on restart.`
    attachPickerOpen.value = false
    await Promise.all([fetchSkillsMeta(), reloadAgentDetail()])
    setTimeout(() => (attachToast.value = ''), 4000)
  } catch (err) {
    attachToast.value = `Attach failed: ${err?.body?.detail?.message || err?.message || String(err)}`
  } finally {
    attachBusy.value = false
  }
}

async function detachSkill(skillName) {
  let message
  if (isAgentCardBased.value) {
    message = `'${skillName}' will be removed from ${agentName.value}. The skill itself stays in the library.`
  } else if (isSpawnedAgent.value) {
    message = `${agentName.value} is a spawned agent. Detaching only affects the live instance; '${skillName}' will reattach on the next spawn unless you also edit the team template.`
  } else {
    message = `${agentName.value} is code-based. Detaching only applies at runtime; the skill will reattach on next backend restart unless you remove it from agent.py.`
  }
  const proceed = await confirm({
    title: `Detach from ${agentName.value}?`,
    message,
    confirmText: 'Detach',
    variant: 'warning',
  })
  if (!proceed) return
  try {
    await apiFetch(
      `/api/skills/${encodeURIComponent(skillName)}/agents/${encodeURIComponent(agentName.value)}`,
      { method: 'DELETE' },
    )
    await Promise.all([fetchSkillsMeta(), reloadAgentDetail()])
  } catch (err) {
    attachToast.value = `Detach failed: ${err?.body?.detail?.message || err?.message || String(err)}`
    setTimeout(() => (attachToast.value = ''), 4000)
  }
}

// Lazy-load skill metadata when the Skills tab is opened. Fall back to also
// pulling on Overview because the panel there shows skills too.
watch(activeTab, (tab) => {
  if ((tab === 'skills' || tab === 'overview') && !skillsMetaFetched.value) {
    fetchSkillsMeta()
  }
})
onMounted(() => {
  // Kick off a meta fetch alongside the initial detail load.
  fetchSkillsMeta()
})

// Per-server tool data for accordion display.
// Backend now returns ``{server: {tools, status, error}}`` so the UI can
// surface MCP attach failures alongside the connected servers. We also
// tolerate the legacy ``{server: [tool, ...]}`` shape so the component
// keeps rendering during the deploy window.
const serverTools = computed(() => {
  const tools = agent.value?.tools || {}
  return Object.entries(tools).map(([name, info]) => {
    const isLegacy = Array.isArray(info)
    const rawList = isLegacy ? info : (info?.tools || [])
    const status = isLegacy ? 'connected' : (info?.status || 'connected')
    return {
      name,
      status,
      connected: status === 'connected',
      error: isLegacy ? '' : (info?.error || ''),
      tools: rawList.map(t => {
        if (typeof t === 'string') return { name: t, description: '' }
        return { name: t.name, description: t.description || '' }
      }),
    }
  })
})

// All tools flattened for display (handles both formats)
const allTools = computed(() => {
  const tools = agent.value?.tools || {}
  const result = []
  for (const [server, info] of Object.entries(tools)) {
    const toolList = Array.isArray(info) ? info : (info?.tools || [])
    for (const tool of toolList) {
      const name = typeof tool === 'string' ? tool : tool.name
      result.push({ server, name })
    }
  }
  return result
})

const runtimePending = computed(() => !!agent.value?.runtime_pending)

const failedSkillCount = computed(
  () => (agent.value?.skills || []).filter(s => s.status === 'failed').length,
)
const failedServerCount = computed(
  () => serverTools.value.filter(s => !s.connected).length,
)

// Total tool count across all servers
const totalToolCount = computed(() => allTools.value.length)

function toggleServer(srv) {
  expandedServers[srv] = !expandedServers[srv]
}

function toggleTool(serverName, toolName) {
  const key = `${serverName}/${toolName}`
  expandedTools[key] = !expandedTools[key]
}

function isToolExpanded(serverName, toolName) {
  return !!expandedTools[`${serverName}/${toolName}`]
}

function toggleSkill(skillName) {
  expandedSkills[skillName] = !expandedSkills[skillName]
}

function formatTime(ts) {
  if (!ts) return ''
  return new Date(ts * 1000).toLocaleTimeString()
}

function formatDate(ts) {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const now = new Date()
  const isToday = d.toDateString() === now.toDateString()
  if (isToday) return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function historyBadgeClass(type) {
  if (!type) return 'badge-default'
  if (type.startsWith('meeting_')) return 'badge-meeting'
  if (type.startsWith('inbox_')) return 'badge-inbox'
  if (type.startsWith('spawn_')) return 'badge-spawn'
  if (type === 'tool_call') return 'badge-tool'
  if (type === 'tool_result' || type === 'result') return 'badge-success'
  if (type === 'thinking') return 'badge-thinking'
  if (type === 'error') return 'badge-error'
  return 'badge-default'
}

function historyBadgeLabel(type) {
  if (!type) return 'event'
  if (type.startsWith('meeting_')) return type.replace('meeting_', '📋 ')
  if (type.startsWith('inbox_')) return type.replace('inbox_', '📨 ')
  if (type.startsWith('spawn_')) return type.replace('spawn_', '🚀 ')
  return type
}
</script>

<template>
  <div class="agent-detail">


    <!-- Loading -->
    <div v-if="isLoading" class="loading-state">
      <div class="loading-text">Loading agent details...</div>
    </div>

    <template v-else-if="agent">
      <!-- Agent Header Card with role band on the left -->
      <div class="header-card" :style="{ '--role-band': `var(${roleColorToken(agent)})` }">
        <span class="role-band" aria-hidden="true"></span>
        <div class="header-left">
          <div
            class="agent-avatar"
            :class="[roleAvaClass(agent), { 'avatar-running': agent.status === 'running' }]"
          >
            {{ avatarGlyph(agent) }}
          </div>
          <div class="header-info">
            <div class="header-name-row">
              <h1 class="agent-name">{{ agent.name }}</h1>
              <StatusBadge :status="agent.status || 'idle'" />
              <span v-if="agent.is_default" class="badge badge-master">Conductor</span>
            </div>
            <p class="header-meta">
              {{ agent.description || 'AI Agent' }}
              · {{ agent.type }}
              · {{ agent.model || 'openai.gpt-4o-mini' }}
            </p>
          </div>
        </div>
        <div class="header-actions">
          <button class="btn btn-outline" disabled>Restart</button>
          <router-link
            v-if="agent.is_default"
            :to="{ path: '/agents/' + agent.name + '/inject' }"
            class="btn btn-primary"
          >
            Inject Prompt
          </router-link>
        </div>
      </div>

      <!-- Tabs -->
      <div class="tabs-bar">
        <button
          v-for="tab in tabs"
          :key="tab.id"
          @click="activeTab = tab.id"
          class="tab-item"
          :class="{ active: activeTab === tab.id }"
        >
          {{ tab.label }}
        </button>
      </div>

      <!-- ===== OVERVIEW TAB ===== -->
      <div v-if="activeTab === 'overview'" class="animate-fade-in">
        <!-- Stats Row (full width) -->
        <div class="stats-row">
          <div class="stat-card">
            <span class="stat-label">Model</span>
            <span class="stat-value stat-green">{{ agent.model?.includes('.') ? agent.model.slice(agent.model.indexOf('.') + 1) : (agent.model || '—') }}</span>
          </div>
          <div class="stat-card">
            <span class="stat-label">Type</span>
            <span class="stat-value stat-blue">{{ agent.type }}</span>
          </div>
          <div class="stat-card">
            <span class="stat-label">Skills</span>
            <span class="stat-value stat-purple">{{ agent.skills?.length || 0 }}</span>
          </div>
          <div class="stat-card">
            <span class="stat-label">Servers</span>
            <span class="stat-value stat-orange">{{ agent.servers?.length || 0 }}</span>
          </div>
        </div>

        <!-- 2-column panels -->
        <div class="overview-columns">
          <!-- Left Column -->
          <div class="overview-col">
            <!-- Skills Panel -->
            <div class="panel" v-if="runtimePending || agent.skills?.length">
              <div class="panel-header">
                <h3>
                  Skills ({{ agent.skills?.length || 0 }})
                  <span v-if="failedSkillCount" class="header-failed-pill" :title="`${failedSkillCount} skill(s) failed to load`">
                    {{ failedSkillCount }} failed
                  </span>
                </h3>
                <button class="view-all-link" @click="activeTab = 'skills'">View All →</button>
              </div>
              <div v-if="runtimePending" class="runtime-pending-skeleton">
                <div class="skeleton-row" />
                <div class="skeleton-row" />
                <div class="skeleton-row" />
                <span class="runtime-pending-label">Waiting for agent runtime…</span>
              </div>
              <div v-else class="skill-list">
                <div
                  v-for="skill in agent.skills"
                  :key="skill.name"
                  class="skill-item"
                  :class="`skill-status-${skill.status || 'loaded'}`"
                  :title="skill.status === 'failed' ? `${skill.name} was requested but did not load` : ''"
                >
                  <span class="skill-icon">⚡</span>
                  <div class="skill-info">
                    <span class="skill-name">
                      {{ skill.name }}
                      <span v-if="skill.status === 'failed'" class="badge badge-failed">● Failed</span>
                    </span>
                    <span v-if="skill.description" class="skill-desc">{{ skill.description }}</span>
                  </div>
                </div>
              </div>
            </div>

            <!-- Child Agents -->
            <div class="panel" v-if="agent.child_agents?.length">
              <div class="panel-header">
                <h3>Sub-agents ({{ agent.child_agents.length }})</h3>
              </div>
              <div class="server-list">
                <router-link
                  v-for="child in agent.child_agents"
                  :key="child"
                  :to="'/agents/' + child"
                  class="server-item"
                >
                  <div class="server-icon">🤖</div>
                  <span class="server-name">{{ child }}</span>
                  <StatusBadge 
                    :status="store.agents.get(child)?.status || 'idle'" 
                    class="ml-auto"
                  />
                </router-link>
              </div>
            </div>
          </div>
          
          <!-- Right Column -->
          <div class="overview-col">
            <!-- MCP Servers Panel -->
            <div class="panel" v-if="runtimePending || serverTools.length">
              <div class="panel-header">
                <h3>
                  MCP Servers ({{ serverTools.length }})
                  <span v-if="failedServerCount" class="header-failed-pill" :title="`${failedServerCount} server(s) failed to attach`">
                    {{ failedServerCount }} failed
                  </span>
                </h3>
                <button class="view-all-link" @click="activeTab = 'servers'">View All →</button>
              </div>
              <div v-if="runtimePending" class="runtime-pending-skeleton">
                <div class="skeleton-row" />
                <div class="skeleton-row" />
                <div class="skeleton-row" />
                <span class="runtime-pending-label">Waiting for agent runtime…</span>
              </div>
              <div v-else class="server-list">
                <div
                  v-for="srv in serverTools"
                  :key="srv.name"
                  class="server-item"
                  :class="`server-status-${srv.status}`"
                  :title="srv.error || ''"
                >
                  <div class="server-icon">🔌</div>
                  <span class="server-name">{{ srv.name }}</span>
                  <span v-if="srv.connected" class="badge badge-connected">● Connected</span>
                  <span v-else class="badge badge-failed">● Failed</span>
                </div>
              </div>
            </div>

            <!-- Instruction Preview -->
            <div class="panel" v-if="agent.instruction">
              <div class="panel-header">
                <h3>Instruction (preview)</h3>
                <button class="view-all-link" @click="activeTab = 'instruction'">Expand →</button>
              </div>
              <div class="instruction-preview">
                <!-- See INSTRUCTION TAB note: render as text so XML tags
                     used by fast-agent's skill injection stay visible. -->
                <MarkdownRenderer :content="instructionPreview" content-type="text" />
              </div>
              <div v-if="hasMoreInstruction" class="instruction-more">
                … {{ instructionLineCount - 12 }} more lines
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- ===== SKILLS TAB ===== -->
      <div v-else-if="activeTab === 'skills'" class="animate-fade-in">
        <div v-if="isBuiltinAgent" class="code-agent-banner">
          <strong>This agent is defined in code (agent.py).</strong>
          Attach/detach changes apply at runtime — they revert on backend
          restart unless you also edit
          <code>get_skills(...)</code> in agent.py.
        </div>
        <div v-else-if="isSpawnedAgent" class="code-agent-banner">
          <strong>This is a spawned agent.</strong>
          Skill changes here only affect the live instance — when the agent
          stops or the team is torn down, all attachments are lost. Edit the
          team template to make changes persistent across spawns.
        </div>
        <p v-if="attachToast" class="attach-toast">{{ attachToast }}</p>
        <div class="skills-tab-toolbar">
          <p class="skills-tab-hint">
            Skills are shared across agents. Editing one updates every agent that references it.
          </p>
          <div class="skills-tab-actions">
            <div class="attach-wrap">
              <button
                class="btn-secondary-skill"
                @click="attachPickerOpen = !attachPickerOpen"
                type="button"
              >+ Attach existing</button>
              <div v-if="attachPickerOpen" class="attach-menu" @click.stop>
                <div v-if="!attachableSkills.length" class="attach-empty">
                  All available skills are already attached.
                </div>
                <button
                  v-for="s in attachableSkills"
                  :key="s.name"
                  class="attach-item"
                  :disabled="attachBusy"
                  @click="attachSkill(s.name)"
                >
                  <span class="attach-skill-name">
                    {{ s.name }}
                    <span v-if="s.is_builtin" class="skill-builtin-badge">Built-in</span>
                  </span>
                </button>
              </div>
            </div>
            <button class="btn-create-skill" @click="openCreateSkill" type="button">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round">
                <line x1="12" y1="5" x2="12" y2="19"/>
                <line x1="5" y1="12" x2="19" y2="12"/>
              </svg>
              New skill
            </button>
          </div>
        </div>
        <div v-if="runtimePending" class="runtime-pending-skeleton runtime-pending-skeleton-large">
          <div class="skeleton-row" />
          <div class="skeleton-row" />
          <div class="skeleton-row" />
          <span class="runtime-pending-label">Waiting for agent runtime to report which skills it actually loaded…</span>
        </div>
        <div v-else-if="agent.skills?.length" class="accordion-list">
          <div
            v-for="skill in agent.skills"
            :key="skill.name"
            class="accordion-card skill-accordion-card"
            :class="[
              { 'accordion-expanded': expandedSkills[skill.name] },
              `skill-status-${skill.status || 'loaded'}`,
            ]"
          >
            <!-- Skill Header -->
            <div class="accordion-header skill-accordion-header">
              <button
                class="skill-header-clickable"
                @click="toggleSkill(skill.name)"
                :aria-expanded="expandedSkills[skill.name]"
              >
                <span class="skill-accordion-icon">⚡</span>
                <div class="skill-header-info">
                  <span class="accordion-title">
                    {{ skill.name }}
                    <span
                      v-if="hasSkillMeta(skill.name) && isSkillBuiltin(skill.name)"
                      class="skill-builtin-badge"
                      title="Ships with Jarvis. Editable, but cannot be deleted."
                    >Built-in</span>
                    <span
                      v-if="skill.status === 'failed'"
                      class="badge badge-failed"
                      title="The agent requested this skill but it failed to load (missing file, parse error, or wrong skills dir). It is NOT injected into the agent's prompt."
                    >● Failed</span>
                  </span>
                  <span v-if="skill.description" class="skill-header-preview">
                    {{ skill.description }}
                  </span>
                </div>
              </button>
              <div class="accordion-header-right">
                <button
                  class="skill-action-btn"
                  type="button"
                  @click.stop="openEditSkill(skill.name)"
                  title="Edit skill"
                  aria-label="Edit skill"
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
                    <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
                    <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
                  </svg>
                </button>
                <button
                  class="skill-action-btn"
                  type="button"
                  @click.stop="detachSkill(skill.name)"
                  :title="`Detach from ${agentName}`"
                  aria-label="Detach skill from this agent"
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
                    <path d="M18 6L6 18M6 6l12 12"/>
                  </svg>
                </button>
                <button
                  class="skill-action-btn skill-action-delete"
                  type="button"
                  @click.stop="openDeleteSkill(skill.name)"
                  :disabled="isSkillBuiltin(skill.name)"
                  :title="!hasSkillMeta(skill.name) ? 'Loading skill metadata…' : (isSkillBuiltin(skill.name) ? 'Built-in skills cannot be deleted' : 'Delete skill from library')"
                  :aria-label="isSkillBuiltin(skill.name) ? 'Built-in skill, cannot delete' : 'Delete skill'"
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
                    <polyline points="3 6 5 6 21 6"/>
                    <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
                  </svg>
                </button>
                <button
                  class="skill-action-btn"
                  type="button"
                  @click.stop="toggleSkill(skill.name)"
                  :aria-label="expandedSkills[skill.name] ? 'Collapse' : 'Expand'"
                >
                  <span
                    class="accordion-chevron"
                    :class="{ 'chevron-open': expandedSkills[skill.name] }"
                  >›</span>
                </button>
              </div>
            </div>

            <!-- Skill Expanded Body: content only (description stays in header) -->
            <div v-if="expandedSkills[skill.name]" class="accordion-body skill-accordion-body">
              <MarkdownRenderer
                v-if="skill.content"
                :content="skill.content"
                content-type="markdown"
              />
            </div>
          </div>
        </div>
        <div v-else class="empty-state">
          No skills attached to this agent.
          <button class="empty-cta" @click="openCreateSkill">Create one →</button>
        </div>
      </div>

      <!-- ===== MCP SERVERS TAB ===== -->
      <div v-else-if="activeTab === 'servers'" class="animate-fade-in">
        <div v-if="runtimePending" class="runtime-pending-skeleton runtime-pending-skeleton-large">
          <div class="skeleton-row" />
          <div class="skeleton-row" />
          <div class="skeleton-row" />
          <span class="runtime-pending-label">Waiting for agent runtime to report which MCP servers actually attached…</span>
        </div>
        <div v-else-if="serverTools.length" class="accordion-list">
          <div
            v-for="srv in serverTools"
            :key="srv.name"
            class="accordion-card"
            :class="[
              { 'accordion-expanded': expandedServers[srv.name] },
              `server-status-${srv.status}`,
            ]"
          >
            <!-- Accordion Header -->
            <button class="accordion-header" @click="toggleServer(srv.name)">
              <div class="accordion-header-left">
                <span class="accordion-icon">🔌</span>
                <span class="accordion-title">{{ srv.name }}</span>
                <span class="badge badge-connected" v-if="srv.connected">● Connected</span>
                <span
                  class="badge badge-failed"
                  v-else
                  :title="srv.error || 'MCP server failed to attach. The agent does NOT see these tools.'"
                >● Failed</span>
              </div>
              <div class="accordion-header-right">
                <span class="accordion-tool-count" v-if="srv.tools.length">
                  {{ srv.tools.length }} tool{{ srv.tools.length !== 1 ? 's' : '' }}
                </span>
                <span class="accordion-tool-count muted" v-else-if="srv.connected">Tools not available</span>
                <span class="accordion-tool-count muted" v-else>0 tools</span>
                <span class="accordion-chevron" :class="{ 'chevron-open': expandedServers[srv.name] }">›</span>
              </div>
            </button>

            <!-- Accordion Body -->
            <div v-if="expandedServers[srv.name]" class="accordion-body">
              <div v-if="!srv.connected && srv.error" class="server-error-banner">
                <strong>Attach error:</strong> {{ srv.error }}
              </div>
              <div v-else-if="!srv.connected" class="server-error-banner muted">
                MCP server is not attached. No error message reported.
              </div>
              <div
                v-for="tool in srv.tools"
                :key="tool.name"
                class="accordion-tool-item"
                :class="{ 'tool-clickable': !!tool.description }"
                @click="tool.description && toggleTool(srv.name, tool.name)"
              >
                <span class="accordion-tool-icon">🔧</span>
                <div class="accordion-tool-info">
                  <span class="accordion-tool-name">{{ tool.name }}</span>
                  <span
                    v-if="tool.description"
                    class="accordion-tool-desc"
                    :class="{ 'desc-expanded': isToolExpanded(srv.name, tool.name) }"
                  >{{ tool.description }}</span>
                </div>
              </div>
            </div>
          </div>
        </div>
        <div v-else class="empty-state">No MCP servers configured</div>
      </div>

      <!-- ===== INSTRUCTION TAB ===== -->
      <div v-else-if="activeTab === 'instruction'" class="animate-fade-in">
        <div class="panel">
          <div class="instruction-full">
            <!-- Render as text: fast-agent injects literal XML-like tags
                 (<available_skills>, <skill>, <scripts>, <references>, ...)
                 into the prompt. Markdown sanitization strips unknown HTML
                 tags so those would silently disappear, misleading the user
                 about what the LLM actually sees. -->
            <MarkdownRenderer
              :content="agent.instruction || 'No instruction configured'"
              content-type="text"
            />
          </div>
        </div>
      </div>

      <!-- ===== CONTEXT WINDOW TAB ===== -->
      <div v-else-if="activeTab === 'context'" class="animate-fade-in">
        <!-- Loading -->
        <div v-if="contextLoading && !contextSnapshots.length" class="loading-state">
          <div class="loading-text">Loading context snapshots...</div>
        </div>

        <!-- Empty -->
        <div v-else-if="!contextSnapshots.length" class="empty-state">
          No context window snapshots recorded yet
        </div>

        <!-- Snapshot list -->
        <div v-else class="context-list">
          <div
            v-for="snap in contextSnapshots"
            :key="snap.id"
            class="context-card"
            :class="{ 'context-card-expanded': expandedSnapshot === snap.id }"
          >
            <!-- Snapshot header (clickable) -->
            <div class="context-header" @click="toggleSnapshotMessages(snap)">
              <div class="context-header-left">
                <span class="context-trigger">{{ triggerLabel(snap.trigger) }}</span>
                <span class="context-time">{{ formatDate(snap.created_at) }}</span>
              </div>
              <div class="context-stats">
                <span class="context-stat">
                  <span class="stat-icon">💬</span>
                  {{ snap.message_count }} msgs
                </span>
                <span class="context-stat">
                  <span class="stat-icon">📥</span>
                  {{ formatTokens(snap.total_input_tokens) }}
                </span>
                <span class="context-stat">
                  <span class="stat-icon">📤</span>
                  {{ formatTokens(snap.total_output_tokens) }}
                </span>
                <span class="context-run-id" v-if="snap.run_id">
                  {{ snap.run_id.slice(0, 8) }}
                </span>
                <span class="expand-icon">{{ expandedSnapshot === snap.id ? '▼' : '▶' }}</span>
              </div>
            </div>

            <!-- Expanded messages -->
            <div v-if="expandedSnapshot === snap.id" class="context-messages">
              <div v-if="messagesLoading" class="loading-text" style="padding: 16px;">
                Loading messages...
              </div>
              <div v-else-if="!snapshotMessages.length" class="empty-state" style="padding: 16px;">
                No messages in this snapshot
              </div>
              <div v-else class="messages-scroll">
                <div
                  v-for="(msg, idx) in snapshotMessages"
                  :key="idx"
                  class="context-msg"
                  :class="[roleClass(msg.role), { 'msg-expanded': expandedMessages.has(idx) }]"
                  @click="toggleMessage(idx)"
                >
                  <div class="msg-header">
                    <span class="msg-role-badge" :class="roleClass(msg.role)">
                      {{ roleLabel(msg.role) }}
                    </span>
                    <span v-if="msg.has_tool_calls" class="msg-tool-badge">
                      🔧 {{ msg.tool_count }} tool{{ msg.tool_count > 1 ? 's' : '' }}
                    </span>
                    <span v-if="msg.has_tool_results" class="msg-tool-badge result">
                      📊 result
                    </span>
                    <span class="msg-index">#{{ idx + 1 }}</span>
                  </div>
                  <div class="msg-content" :class="{ 'msg-content-full': expandedMessages.has(idx) }">
                    <MarkdownRenderer
                      :content="expandedMessages.has(idx) ? msg.content : truncateContent(msg.content)"
                      content-type="markdown"
                      :enable-mermaid="expandedMessages.has(idx)"
                    />
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- ===== CONTEXT VERSIONS TAB (compaction timeline) ===== -->
      <div v-else-if="activeTab === 'versions'" class="animate-fade-in">
        <!-- Live banner while a compaction is running -->
        <div v-if="liveAgent?.compaction?.inProgress" class="version-live-banner">
          <span class="version-live-dot" />
          Compacting context…
        </div>

        <!-- Loading -->
        <div v-if="versionsLoading && !contextVersions.length" class="loading-state">
          <div class="loading-text">Loading compaction versions...</div>
        </div>

        <!-- Fetch error: distinct from empty, with a retry path -->
        <div v-else-if="versionsError" class="empty-state">
          Failed to load compaction versions —
          <button class="version-retry-btn" @click="fetchContextVersions({ force: true })">
            Retry
          </button>
        </div>

        <!-- Empty -->
        <div v-else-if="!contextVersions.length" class="empty-state">
          No context compactions yet — versions appear here when the agent's
          context is automatically compacted (see Settings → Context Compaction)
        </div>

        <!-- Version timeline -->
        <div v-else class="context-list">
          <div
            v-for="v in contextVersions"
            :key="v.id"
            class="context-card"
            :class="{ 'context-card-expanded': expandedVersionId === v.id }"
          >
            <div class="context-header" @click="toggleVersion(v, 'summary')">
              <div class="context-header-left">
                <span
                  class="version-status"
                  :class="v.status === 'completed' ? 'version-status-ok' : 'version-status-fail'"
                >
                  {{ v.status === 'completed' ? '✓ Compacted' : '✕ Failed' }}
                </span>
                <span class="context-time">{{ formatDate(v.created_at) }}</span>
                <span class="version-trigger">{{ v.trigger }}</span>
              </div>
              <div class="context-stats">
                <template v-if="v.status === 'completed'">
                  <span class="context-stat" title="messages before → after">
                    💬 {{ v.message_count_before }} → {{ v.message_count_after }}
                  </span>
                  <span class="context-stat" title="estimated tokens before → after">
                    🧮 {{ formatTokens(v.estimated_tokens_before) }} → {{ formatTokens(v.estimated_tokens_after) }}
                  </span>
                  <span class="version-saved">
                    −{{ formatTokens(v.saved_tokens) }} ({{ savingsPct(v) }}%)
                  </span>
                </template>
                <span v-else class="version-error-preview">{{ v.error_message }}</span>
                <span class="expand-icon">{{ expandedVersionId === v.id ? '▼' : '▶' }}</span>
              </div>
            </div>

            <!-- Expanded panel -->
            <div v-if="expandedVersionId === v.id" class="version-detail">
              <div class="version-detail-toolbar">
                <button
                  class="version-tab-btn"
                  :class="{ active: versionPanel === 'summary' }"
                  @click.stop="toggleVersion(v, 'summary')"
                >Summary</button>
                <button
                  v-if="v.status === 'completed'"
                  class="version-tab-btn"
                  :class="{ active: versionPanel === 'diff' }"
                  @click.stop="toggleVersion(v, 'diff')"
                >Before / After</button>
                <span class="version-meta">
                  confidence {{ Math.round((v.confidence || 0) * 100) }}%
                  <template v-if="v.raw_snapshot_id"> · raw snapshot #{{ v.raw_snapshot_id }}</template>
                </span>
              </div>

              <!-- Summary panel -->
              <div v-if="versionPanel === 'summary'">
                <div v-if="!versionDetails[v.id]" class="loading-text" style="padding: 16px;">
                  Loading summary...
                </div>
                <div v-else-if="versionDetails[v.id].error" class="empty-state" style="padding: 16px;">
                  Failed to load detail —
                  <button class="version-retry-btn" @click.stop="retryVersionPanel(v)">Retry</button>
                </div>
                <template v-else>
                  <div
                    v-if="versionDetails[v.id].plan?.risks?.length"
                    class="version-risks"
                  >
                    ⚠ {{ versionDetails[v.id].plan.risks.join(' · ') }}
                  </div>
                  <pre class="version-summary-pre">{{ versionDetails[v.id].summary_message || versionDetails[v.id].error_message }}</pre>
                </template>
              </div>

              <!-- Diff panel -->
              <div v-else-if="versionPanel === 'diff'">
                <div v-if="!versionDiffs[v.id]" class="loading-text" style="padding: 16px;">
                  Loading diff...
                </div>
                <div v-else-if="versionDiffs[v.id].error" class="empty-state" style="padding: 16px;">
                  Diff unavailable —
                  <button class="version-retry-btn" @click.stop="retryVersionPanel(v)">Retry</button>
                </div>
                <div v-else class="version-diff">
                  <div class="version-diff-col">
                    <div class="version-diff-title">
                      Before ({{ versionDiffs[v.id].before.length }} msgs)
                    </div>
                    <div
                      v-for="(m, i) in versionDiffs[v.id].before"
                      :key="'b' + i"
                      class="version-diff-msg"
                      :class="'diff-' + m.disposition"
                    >
                      <span class="version-diff-role">{{ m.role }}</span>
                      <span class="version-diff-disposition">{{ m.disposition }}</span>
                      <span class="version-diff-preview">{{ m.preview }}</span>
                    </div>
                  </div>
                  <div class="version-diff-col">
                    <div class="version-diff-title">
                      After ({{ versionDiffs[v.id].after.length }} msgs)
                    </div>
                    <div
                      v-for="(m, i) in versionDiffs[v.id].after"
                      :key="'a' + i"
                      class="version-diff-msg"
                      :class="'diff-' + m.disposition"
                    >
                      <span class="version-diff-role">{{ m.role }}</span>
                      <span class="version-diff-disposition">{{ m.disposition }}</span>
                      <span class="version-diff-preview">{{ m.preview }}</span>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- ===== MEMORY TAB ===== -->
      <div v-else-if="activeTab === 'memory'" class="animate-fade-in">
        <AgentMemoryPanel :agent-name="agentName" />
      </div>

      <!-- ===== HISTORY TAB ===== -->
      <div v-else-if="activeTab === 'activity'" class="animate-fade-in">
        <!-- Loading state -->
        <div v-if="timelineLoading && !mergedHistory.length" class="loading-state">
          <div class="loading-text">Loading history...</div>
        </div>

        <!-- Empty state -->
        <div v-else-if="!mergedHistory.length" class="empty-state">
          No activity recorded for this agent
        </div>

        <!-- History list -->
        <div v-else class="activity-list">
          <div
            v-for="event in mergedHistory"
            :key="event.id"
            class="activity-item"
          >
            <span class="activity-time">{{ formatDate(event.timestamp) }}</span>
            <div class="activity-body">
              <span
                class="activity-badge"
                :class="historyBadgeClass(event.event_type)"
              >
                {{ historyBadgeLabel(event.event_type) }}
              </span>
              <span class="activity-message">{{ event.message }}</span>
              <span v-if="event.metadata?.agenda" class="activity-meta">
                📌 {{ event.metadata.agenda }}
              </span>
              <span v-if="event.metadata?.to" class="activity-meta">
                → {{ event.metadata.to }}
              </span>
            </div>
          </div>
        </div>
      </div>
    </template>

    <!-- Not found -->
    <div v-else class="empty-state" style="padding: 80px 0;">
      <div style="font-size: 2.5rem; margin-bottom: 0.5rem;">❓</div>
      <h3 style="color: var(--color-text-primary)">Agent not found</h3>
      <p style="color: var(--color-text-muted)">{{ agentName }}</p>
    </div>

    <SkillEditorModal
      :visible="editorVisible"
      :mode="editorMode"
      :skill-name="editorTarget"
      @close="editorVisible = false"
      @saved="onSkillSaved"
    />
    <SkillDeleteModal
      :visible="deleteModalVisible"
      :skill-name="deleteTarget.name"
      :used-by="deleteTarget.usedBy"
      @close="deleteModalVisible = false"
      @deleted="onSkillDeleted"
    />
  </div>
</template>

<style scoped>
.agent-detail {
  max-width: 1200px;
  margin: 0 auto;
}

/* ── Header Card ── */
.header-card {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: var(--bg-1, var(--bg-1));
  border: 1px solid var(--border, var(--border));
  border-radius: var(--r-md, 12px);
  /* Extra left padding makes room for the 4px role band. */
  padding: 20px 24px 20px 28px;
  margin-bottom: 20px;
  overflow: hidden;
}
/* Role-tinted band on the left edge — design handoff §8. */
.role-band {
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 4px;
  background: var(--role-band, var(--border-strong));
}
.header-left {
  display: flex;
  align-items: center;
  gap: 16px;
}
.agent-avatar {
  width: 52px;
  height: 52px;
  border-radius: 50%;
  background: linear-gradient(135deg, #1e3a5f, var(--primary));
  display: flex;
  align-items: center;
  justify-content: center;
  font-family: var(--font-mono, 'JetBrains Mono', monospace);
  font-size: 18px;
  font-weight: 700;
  color: var(--text-dim);
  flex-shrink: 0;
  border: 2px solid transparent;
  transition: border-color 0.3s;
}
/* Role-tinted avatar variants — re-declared here in component scope so
   they apply without needing the .jv parent class. */
.agent-avatar.ava-jarvis { background: linear-gradient(135deg, var(--primary, #6366f1), var(--accent, #22d3ee)); color: white; }
.agent-avatar.ava-pm  { background: var(--role-pm,  #A5B4FC); color: var(--bg-1); }
.agent-avatar.ava-sa  { background: var(--role-sa,  #67E8F9); color: var(--bg-1); }
.agent-avatar.ava-ba  { background: var(--role-ba,  #C4B5FD); color: var(--bg-1); }
.agent-avatar.ava-dev { background: var(--role-dev, #6EE7B7); color: var(--bg-1); }
.agent-avatar.ava-qe  { background: var(--role-qe,  #FCD34D); color: var(--bg-1); }
.agent-avatar.ava-des { background: var(--role-des, #F9A8D4); color: var(--bg-1); }
.agent-avatar.ava-dso { background: var(--role-dso, #FDBA74); color: var(--bg-1); }
.avatar-running {
  border-color: #10b981;
  box-shadow: 0 0 12px rgba(16, 185, 129, 0.25);
}
.header-name-row {
  display: flex;
  align-items: center;
  gap: 10px;
}
.agent-name {
  font-family: var(--font-display, 'Space Grotesk', sans-serif);
  font-size: 22px;
  font-weight: 600;
  letter-spacing: -0.02em;
  color: var(--text, var(--text));
  margin: 0;
}
.header-meta {
  font-size: 12px;
  color: var(--color-text-muted, var(--text-muted));
  margin-top: 4px;
}
.header-actions {
  display: flex;
  gap: 10px;
}

/* ── Badges ── */
.badge {
  font-size: 11px;
  font-weight: 600;
  padding: 3px 10px;
  border-radius: 20px;
  white-space: nowrap;
}
.badge-master {
  background: rgba(99, 102, 241, 0.15);
  color: #818cf8;
}
.badge-connected {
  font-size: 11px;
  color: #10b981;
  margin-left: auto;
}
.badge-disconnected {
  font-size: 11px;
  color: #ef4444;
  margin-left: auto;
}
.badge-failed {
  font-size: 11px;
  color: #ef4444;
  background: rgba(239, 68, 68, 0.12);
  border: 1px solid rgba(239, 68, 68, 0.35);
  margin-left: auto;
}
.header-failed-pill {
  display: inline-block;
  margin-left: 8px;
  font-size: 11px;
  font-weight: 600;
  color: #ef4444;
  background: rgba(239, 68, 68, 0.12);
  border: 1px solid rgba(239, 68, 68, 0.35);
  padding: 1px 8px;
  border-radius: 10px;
  vertical-align: middle;
}
.skill-status-failed .skill-name,
.skill-status-failed .accordion-title {
  color: #f87171;
}
.skill-status-failed .skill-icon,
.skill-status-failed .skill-accordion-icon {
  opacity: 0.55;
}
.server-status-failed .server-name,
.server-status-failed .accordion-title {
  color: #f87171;
}
.server-status-failed .server-icon,
.server-status-failed .accordion-icon {
  opacity: 0.55;
}
.server-error-banner {
  background: var(--danger-bg);
  border: 1px solid rgba(239, 68, 68, 0.30);
  color: var(--danger);
  font-size: 12px;
  line-height: 1.5;
  padding: 8px 12px;
  border-radius: 6px;
  margin: 8px 0;
}
/* Light theme keeps --danger at #EF4444 (red-500), which is too pale on the
   light danger-bg — drop to red-700 for AA-legible text. */
:root[data-theme="light"] .server-error-banner {
  color: #B91C1C;
  border-color: rgba(185, 28, 28, 0.30);
}
.server-error-banner.muted {
  background: rgba(239, 68, 68, 0.04);
  color: var(--text-muted);
}
.runtime-pending-skeleton {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 0;
}
.runtime-pending-skeleton-large {
  padding: 24px 0;
}
.skeleton-row {
  height: 14px;
  border-radius: 6px;
  background: linear-gradient(
    90deg,
    rgba(255, 255, 255, 0.04) 0%,
    rgba(255, 255, 255, 0.08) 50%,
    rgba(255, 255, 255, 0.04) 100%
  );
  background-size: 200% 100%;
  animation: shimmer 1.4s linear infinite;
}
@keyframes shimmer {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}
.runtime-pending-label {
  font-size: 12px;
  color: var(--color-text-muted, var(--text-muted));
  margin-top: 6px;
}

/* ── Buttons ── */
.btn {
  font-size: 13px;
  font-weight: 500;
  padding: 8px 20px;
  border-radius: 8px;
  border: none;
  cursor: pointer;
  text-decoration: none;
  transition: all 0.15s;
}
.btn-outline {
  background: transparent;
  border: 1px solid var(--color-border, var(--border));
  color: var(--color-text-secondary, var(--text-dim));
}
.btn-outline:hover:not(:disabled) { border-color: var(--color-text-muted); }
.btn-outline:disabled { opacity: 0.4; cursor: not-allowed; }
.btn-primary {
  background: #ef4444;
  color: white;
}
.btn-primary:hover { background: #dc2626; }

/* ── Tabs (segmented control per design handoff) ── */
.tabs-bar {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  padding: 3px;
  background: var(--bg-2, #11141B);
  border: 1px solid var(--border-strong, rgba(255,255,255,0.12));
  border-radius: var(--r-md, 10px);
  margin-bottom: 20px;
  /* Avoid stretching across the full row width — segmented control sits
     left, the design shows it as a contained pill. */
  width: fit-content;
  max-width: 100%;
  overflow-x: auto;
}
.tab-item {
  padding: 6px 14px;
  height: 28px;
  font-size: 12.5px;
  font-weight: 500;
  color: var(--text-dim, var(--text-dim));
  background: transparent;
  border: none;
  border-radius: 8px;
  cursor: pointer;
  transition: all 0.15s var(--ease-out, cubic-bezier(0.2,0.7,0.2,1));
  white-space: nowrap;
}
.tab-item:hover { color: var(--text, var(--text)); background: var(--bg-3, var(--bg-3)); }
.tab-item.active {
  background: var(--primary-bg-strong, rgba(99, 102, 241, 0.18));
  color: var(--primary-hover, #818CF8);
}

/* ── Overview 2-column Layout ── */
.overview-columns {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
.overview-col {
  min-width: 0; /* prevent grid blowout from long text */
}
@media (max-width: 900px) {
  .overview-columns { grid-template-columns: 1fr; }
}

/* ── Stats Row ── */
.stats-row {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
  margin-bottom: 16px;
}
.stat-card {
  background: var(--color-bg-card, var(--bg-1));
  border: 1px solid var(--color-border, var(--border));
  border-radius: 10px;
  padding: 14px 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.stat-label {
  font-size: 11px;
  color: var(--color-text-muted, var(--text-muted));
  text-transform: uppercase;
  letter-spacing: 0.3px;
}
.stat-value {
  font-size: 16px;
  font-weight: 700;
}
.stat-green { color: #10b981; }
.stat-blue { color: #3b82f6; }
.stat-purple { color: #818cf8; }
.stat-orange { color: #f59e0b; }

/* ── Panel ── */
.panel {
  background: var(--color-bg-card, var(--bg-1));
  border: 1px solid var(--color-border, var(--border));
  border-radius: 12px;
  padding: 16px;
  margin-bottom: 16px;
}
.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}
.panel-header h3 {
  font-size: 14px;
  font-weight: 600;
  color: var(--color-text-primary, var(--text));
  margin: 0;
}
.view-all-link {
  font-size: 12px;
  font-weight: 500;
  color: var(--color-accent, #00d4aa);
  background: none;
  border: none;
  cursor: pointer;
}
.view-all-link:hover { text-decoration: underline; }

/* ── Skills (Overview panel – compact list) ── */
.skill-list { display: flex; flex-direction: column; gap: 4px; }
.skill-item {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 12px;
  border-radius: 8px;
  background: rgba(255,255,255,0.02);
}
.skill-icon {
  font-size: 16px;
  flex-shrink: 0;
  margin-top: 1px;
}
.skill-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.skill-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text-primary, var(--text));
}
.skill-desc {
  font-size: 11px;
  color: var(--color-text-muted, var(--text-muted));
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ── Skills Tab Toolbar ── */
.code-agent-banner {
  margin-bottom: 12px;
  padding: 10px 14px;
  background: color-mix(in srgb, var(--warning) 12%, transparent);
  border: 1px solid color-mix(in srgb, var(--warning) 35%, transparent);
  border-radius: 8px;
  color: var(--text);
  font-size: 12px;
  line-height: 1.6;
}
.code-agent-banner strong { color: var(--warning); font-weight: 600; }
.code-agent-banner code {
  background: var(--bg-3);
  border: 1px solid var(--border);
  color: var(--text);
  padding: 1px 5px;
  border-radius: 4px;
  font-size: 11px;
  font-family: var(--font-mono);
}
.attach-toast {
  margin: 0 0 10px;
  padding: 8px 12px;
  background: var(--primary-bg);
  border: 1px solid color-mix(in srgb, var(--primary) 28%, transparent);
  border-radius: 8px;
  color: var(--text);
  font-size: 12px;
}

.skills-tab-actions {
  display: flex;
  gap: 8px;
  align-items: center;
}
.btn-secondary-skill {
  padding: 7px 12px;
  background: var(--bg-2);
  border: 1px solid var(--border-strong);
  color: var(--text);
  border-radius: 8px;
  font-size: 13px;
  cursor: pointer;
  font-weight: 500;
}
.btn-secondary-skill:hover {
  background: var(--bg-3);
  color: var(--text);
  border-color: var(--primary);
}
.attach-wrap { position: relative; }
.attach-menu {
  position: absolute;
  right: 0;
  top: calc(100% + 4px);
  min-width: 240px;
  max-height: 300px;
  overflow-y: auto;
  background: var(--bg-1);
  border: 1px solid var(--border);
  border-radius: 10px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5);
  z-index: 50;
}
.attach-item {
  display: flex;
  align-items: center;
  width: 100%;
  padding: 8px 12px;
  background: transparent;
  border: none;
  color: var(--text-dim);
  font-size: 13px;
  cursor: pointer;
  text-align: left;
}
.attach-item:hover:not(:disabled) {
  background: var(--bg-3);
  color: var(--text);
}
.attach-item:disabled { opacity: 0.5; cursor: not-allowed; }
.attach-skill-name {
  display: flex;
  align-items: center;
  /* Push badge to the right edge of every row so the BUILT-IN tags form
     a clean vertical line down the menu instead of floating wherever
     each name ends. */
  justify-content: space-between;
  gap: 8px;
  flex: 1;
  min-width: 0;
}
/* Long names ("debugging-strategies") must truncate with ellipsis, not
   wrap onto a second line and squeeze the badge. The badge stays one
   line (nowrap + flex-shrink: 0) so "BUILT-IN" never breaks into
   "BUILT-\nIN" — the wrap reported 2026-05-27. */
.attach-skill-name > :first-child:not(.skill-builtin-badge) {
  /* When the first child is the bare text node, this selector misses it.
     Fall back: use ellipsis on the host directly via overflow + min-width
     and let the badge's flex-shrink:0 own its space.  */
}
.attach-skill-name .skill-builtin-badge {
  flex-shrink: 0;
  white-space: nowrap;
  margin-left: 0; /* parent gap handles spacing */
}
.attach-empty { padding: 12px; color: var(--text-subtle); font-size: 12px; font-style: italic; }

.skills-tab-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
  flex-wrap: wrap;
}
.skills-tab-hint {
  margin: 0;
  font-size: 12px;
  color: var(--color-text-muted, var(--text-muted));
}
.btn-create-skill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 7px 14px;
  background: var(--primary);
  border: 1px solid var(--primary);
  color: #fff;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.15s;
}
.btn-create-skill:hover {
  background: var(--primary-hover);
  border-color: var(--primary-hover);
  color: #fff;
}
.empty-cta {
  display: inline-block;
  margin-left: 8px;
  background: none;
  border: none;
  color: var(--primary);
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  padding: 0;
}
.empty-cta:hover { color: var(--primary-hover); text-decoration: underline; }
.empty-cta:hover { color: #93c5fd; }

/* ── Skills Tab Accordion ── */
.skill-accordion-card {
  /* inherits accordion-card styles */
}
.skill-accordion-header {
  /* Replaces the original <button>: now a flex container hosting a
     clickable region (title) and per-row action buttons. We can't use a
     <button> as the wrapper or the nested action buttons would be invalid.
     Zero out the inherited .accordion-header padding/cursor — the inner
     .skill-header-clickable owns those now (so hover and click hit-area
     stay scoped to the title region, not the action buttons). */
  display: flex !important;
  align-items: stretch;
  gap: 0;
  padding: 0 8px 0 0;
  cursor: default;
}
.skill-accordion-header:hover { background: none; }
.skill-header-clickable {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 12px;
  background: none;
  border: none;
  color: inherit;
  text-align: left;
  padding: 12px 14px;
  cursor: pointer;
  min-width: 0;
}
.skill-header-clickable:hover { background: rgba(255, 255, 255, 0.02); }
.skill-action-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  margin: auto 0;
  background: transparent;
  border: 1px solid transparent;
  color: var(--color-text-muted, var(--text-muted));
  border-radius: 7px;
  cursor: pointer;
  transition: all 0.15s;
}
.skill-action-btn:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.04);
  color: var(--color-text-primary, var(--text));
  border-color: rgba(255, 255, 255, 0.06);
}
.skill-action-btn:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}
.skill-action-delete:hover:not(:disabled) {
  background: rgba(239, 68, 68, 0.12);
  border-color: rgba(239, 68, 68, 0.25);
  color: #f87171;
}
.skill-action-btn .accordion-chevron {
  /* Reuse existing chevron styling but inside the action button host. */
  font-size: 16px;
  line-height: 1;
}
.skill-builtin-badge {
  display: inline-block;
  margin-left: 8px;
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  padding: 2px 6px;
  border-radius: 4px;
  background: var(--primary-bg-strong);
  color: var(--primary);
  border: 1px solid color-mix(in srgb, var(--primary) 30%, transparent);
  vertical-align: middle;
}
.skill-accordion-icon {
  font-size: 20px;
  flex-shrink: 0;
  filter: drop-shadow(0 0 4px rgba(0, 212, 170, 0.3));
}
.skill-header-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  text-align: left;
}
.skill-header-preview {
  font-size: 11px;
  color: var(--color-text-muted, var(--text-muted));
  white-space: normal;
  word-break: break-word;
}
.skill-tag {
  font-size: 10px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 20px;
  background: rgba(0, 212, 170, 0.1);
  color: #00d4aa;
  white-space: nowrap;
}
.skill-accordion-body {
  border-top: 1px solid var(--color-border, var(--border));
  padding: 16px;
}
.skill-full-desc {
  font-size: 13px;
  line-height: 1.7;
  color: var(--color-text-secondary, var(--text-dim));
  margin-bottom: 12px;
  white-space: pre-wrap;
  word-break: break-word;
}
/* skill-content-block removed — was a redundant inner box that
   produced "padding inside padding" against the accordion-body
   container. Markdown now renders directly with the body's own
   padding. */
.skill-content-pre {
  font-family: 'SF Mono', 'JetBrains Mono', 'Cascadia Code', monospace;
  font-size: 12px;
  line-height: 1.7;
  color: var(--color-text-secondary, var(--text-dim));
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0;
  padding: 14px;
  max-height: 400px;
  overflow-y: auto;
}
.skill-no-content {
  font-size: 12px;
  color: var(--color-text-subtle, var(--text-subtle));
  font-style: italic;
  text-align: center;
  padding: 8px 0;
}

/* ── Servers ── */
.server-list { display: flex; flex-direction: column; gap: 4px; }
.server-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-radius: 8px;
  background: rgba(255,255,255,0.02);
  text-decoration: none;
  transition: background 0.15s;
}
.server-icon {
  font-size: 18px;
  flex-shrink: 0;
}
.server-name {
  font-size: 13px;
  font-weight: 500;
  color: var(--color-text-primary, var(--text));
}

/* ── Instruction ── */
/* No bg/border/padding here — the parent ``.panel`` already provides
   a card with padding 16px. Adding another bordered+padded layer
   inside produced the "padding inside padding" double-box look. */
.instruction-preview {
  max-height: 280px;
  overflow: hidden;
}
.instruction-full {
  max-height: none;
  overflow: visible;
}
.instruction-more {
  font-size: 11px;
  color: var(--color-text-subtle, var(--text-subtle));
  padding-top: 8px;
  text-align: center;
}

/* ── Accordion (MCP Servers Tab) ── */
.accordion-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.accordion-card {
  background: var(--color-bg-card, var(--bg-1));
  border: 1px solid var(--color-border, var(--border));
  border-radius: 12px;
  overflow: hidden;
  transition: border-color 0.2s;
}
.accordion-card:hover {
  border-color: var(--primary);
}
.accordion-expanded {
  border-color: var(--primary);
}
.accordion-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding: 14px 16px;
  background: none;
  border: none;
  cursor: pointer;
  color: inherit;
  text-align: left;
  transition: background 0.15s;
}
.accordion-header:hover {
  background: rgba(255, 255, 255, 0.02);
}
.accordion-header-left {
  display: flex;
  align-items: center;
  gap: 10px;
}
.accordion-header-right {
  display: flex;
  align-items: center;
  gap: 10px;
}
.accordion-icon {
  font-size: 18px;
  flex-shrink: 0;
}
.accordion-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text-primary, var(--text));
}
.accordion-tool-count {
  font-size: 11px;
  color: var(--color-text-muted, var(--text-muted));
}
.accordion-tool-count.muted {
  color: var(--color-text-subtle, var(--text-subtle));
}
.accordion-chevron {
  font-size: 18px;
  font-weight: 300;
  color: var(--color-text-subtle, var(--text-subtle));
  transition: transform 0.2s ease;
  transform: rotate(0deg);
}
.chevron-open {
  transform: rotate(90deg);
}
.accordion-body {
  padding: 0 16px 12px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  animation: fadeIn 0.15s ease-out;
}
.accordion-tool-item {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 8px 12px;
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.015);
}
.accordion-tool-icon {
  font-size: 14px;
  flex-shrink: 0;
  margin-top: 2px;
}
.accordion-tool-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.accordion-tool-name {
  font-size: 12px;
  font-weight: 500;
  color: var(--color-text-primary, var(--text));
}
.accordion-tool-desc {
  font-size: 11px;
  color: var(--color-text-muted, var(--text-muted));
  line-height: 1.4;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  transition: all 0.2s ease;
  cursor: pointer;
}
.accordion-tool-desc.desc-expanded {
  -webkit-line-clamp: unset;
  line-clamp: unset;
  display: block;
}
.tool-clickable {
  cursor: pointer;
  transition: background 0.15s ease;
}
.tool-clickable:hover {
  background: rgba(255, 255, 255, 0.03);
}

/* ── History source badges ── */
.badge-meeting { background: rgba(139, 92, 246, 0.12); color: #a78bfa; }
.badge-inbox   { background: rgba(59, 130, 246, 0.12); color: #60a5fa; }
.badge-spawn   { background: rgba(16, 185, 129, 0.12); color: #34d399; }

.activity-meta {
  display: block;
  font-size: 11px;
  color: var(--color-text-subtle, var(--text-subtle));
  margin-top: 2px;
}

/* ── Activity ── */
.activity-list { display: flex; flex-direction: column; gap: 4px; }
.activity-item {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 10px 14px;
  border-radius: 8px;
  background: var(--color-bg-card, var(--bg-1));
}
.activity-time {
  font-size: 11px;
  color: var(--color-text-subtle, var(--text-subtle));
  white-space: nowrap;
  margin-top: 2px;
}
.activity-body {
  flex: 1;
  min-width: 0;
}
.activity-badge {
  font-size: 10px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 4px;
  margin-right: 8px;
}
.badge-tool { background: rgba(59,130,246,0.12); color: #60a5fa; }
.badge-success { background: rgba(16,185,129,0.12); color: #34d399; }
.badge-thinking { background: rgba(245,158,11,0.12); color: #fbbf24; }
.badge-error { background: rgba(239,68,68,0.12); color: #f87171; }
.badge-default { background: rgba(100,116,139,0.12); color: #94a3b8; }
.activity-message {
  font-size: 13px;
  color: var(--color-text-secondary, var(--text-dim));
}

/* ── Empty State ── */
.empty-state {
  text-align: center;
  padding: 48px 0;
  font-size: 13px;
  color: var(--color-text-muted, var(--text-muted));
}

/* ── Loading ── */
.loading-state {
  display: flex;
  justify-content: center;
  padding: 80px 0;
}
.loading-text {
  color: var(--color-text-muted, var(--text-muted));
  animation: pulse 1.5s ease-in-out infinite;
}
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}

/* ── Animations ── */
.animate-fade-in {
  animation: fadeIn 0.15s ease-out;
}
@keyframes fadeIn {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: translateY(0); }
}

/* ════════════════════════════════════════════════
   MOBILE RESPONSIVE — max-width: 767px
   ════════════════════════════════════════════════ */
@media (max-width: 767px) {
  .agent-detail {
    max-width: 100%;
    /* no padding — AppLayout handles edge padding */
  }


  .desktop-only { display: none !important; }

  /* ── Header card ── */
  .header-card {
    flex-direction: column;
    align-items: flex-start;
    gap: 14px;
    border-radius: 0;
    border-left: none;
    border-right: none;
    border-top: none;
    padding: 14px;
    margin-bottom: 0;
  }

  .header-left { gap: 12px; }

  .agent-avatar {
    width: 44px;
    height: 44px;
    font-size: 18px;
  }

  .agent-name { font-size: 16px; }

  .header-meta { font-size: 11px; }

  /* Hide restart/inject buttons on mobile — use Inject tab instead */
  .header-actions { display: none; }

  /* ── Tabs bar: flat horizontal-scroll strip ──
     The desktop "pill" (fit-content + border + bg + radius) gets clipped
     mid-tab at the screen edge with 6 tabs, reading as broken. On mobile
     drop the pill chrome: a flat full-width strip with a bottom border
     scrolls cleanly and the active tab's own highlight marks selection. */
  .tabs-bar {
    display: flex;
    width: auto;
    overflow-x: auto;
    scrollbar-width: none;
    margin-bottom: 0;
    padding: 0;
    background: transparent;
    border: none;
    border-radius: 0;
    border-bottom: 1px solid var(--color-border, var(--border));
    -webkit-overflow-scrolling: touch;
  }

  .tabs-bar::-webkit-scrollbar { display: none; }

  .tab-item {
    padding: 10px 16px;
    font-size: 13px;
    white-space: nowrap;
    flex-shrink: 0;
  }

  /* ── Overview: 1-column ── */
  .stats-row {
    grid-template-columns: 1fr 1fr;
    gap: 8px;
    padding: 12px 14px;
    background: transparent;
    margin-bottom: 0;
  }

  .stat-card {
    padding: 10px 12px;
    border-radius: 8px;
  }

  .stat-value { font-size: 14px; }

  .overview-columns {
    grid-template-columns: 1fr;
    padding: 0 14px;
    gap: 12px;
  }

  .panel {
    margin-bottom: 12px;
    border-radius: 8px;
    padding: 12px 14px;
  }

  /* ── Activity / History ── */
  .activity-item {
    flex-direction: column;
    gap: 4px;
    padding: 10px 14px;
  }

  .activity-time {
    font-size: 10px;
  }

  .activity-message {
    font-size: 12px;
    word-break: break-word;
  }

  /* ── Accordion (MCP servers) ── */
  .accordion-header {
    padding: 12px 14px;
  }

  /* ── Instruction ── */
  .instruction-preview { max-height: 200px; }
  .skill-accordion-body { padding: 12px; }

  /* Attach-skill dropdown: anchored to the right edge of the +Attach
     button, the desktop 240px min-width overflowed off-screen on
     phones. Full-bleed sheet-style placement on mobile keeps every
     skill row reachable without horizontal scroll. */
  .attach-menu {
    left: 8px;
    right: 8px;
    min-width: 0;
    max-height: 60vh;
  }

  /* Skill accordion header on mobile: 4 action buttons + title +
     badges all on one row squeezed the title to ~2 chars before
     ellipsis. Stack the action row below the title so each gets a
     full-width hit area. */
  .skill-accordion-header { flex-direction: column; align-items: stretch; gap: 6px; }
  .skill-header-clickable { width: 100%; }
  .accordion-header-right {
    width: 100%;
    justify-content: flex-end;
    gap: 6px;
  }

  /* ── Context Window ── */
  .context-header {
    flex-direction: column;
    align-items: flex-start;
    padding: 12px 14px;
    gap: 8px;
  }
  .context-header-left {
    width: 100%;
  }
  .context-stats {
    width: 100%;
    flex-wrap: wrap;
    gap: 8px 12px;
  }
  .context-run-id {
    margin-left: 0;
  }
  .expand-icon {
    position: absolute;
    right: 14px;
    top: 14px;
  }
  .context-msg {
    padding: 8px 10px;
  }
  .msg-content {
    font-size: 11px;
    max-height: 150px;
  }
  .messages-scroll {
    padding: 6px;
  }
}

/* ── Context Window Tab ── */
.context-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.context-card {
  background: var(--color-bg-card, var(--bg-1));
  border: 1px solid var(--color-border, var(--border));
  border-radius: 10px;
  overflow: hidden;
  transition: border-color 0.2s;
}
.context-card:hover {
  border-color: var(--color-border-active, var(--primary));
}
.context-card-expanded {
  border-color: var(--color-accent-blue, #3b82f6);
}
.context-header {
  padding: 14px 18px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  cursor: pointer;
  gap: 12px;
  position: relative;
}
.context-header:hover {
  background: rgba(255,255,255,0.02);
}
.context-header-left {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}
.context-trigger {
  font-weight: 600;
  font-size: 13px;
  color: var(--color-text-primary, var(--text));
  white-space: nowrap;
}
.context-time {
  font-size: 12px;
  color: var(--color-text-muted, var(--text-muted));
  white-space: nowrap;
}
.context-stats {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-shrink: 0;
}
.context-stat {
  font-size: 12px;
  color: var(--color-text-secondary, var(--text-dim));
  display: flex;
  align-items: center;
  gap: 3px;
}
.stat-icon {
  font-size: 11px;
}
.context-run-id {
  font-family: monospace;
  font-size: 11px;
  color: var(--color-text-subtle, var(--text-subtle));
  background: rgba(255,255,255,0.04);
  padding: 2px 6px;
  border-radius: 4px;
}
.expand-icon {
  font-size: 10px;
  color: var(--color-text-muted, var(--text-muted));
  margin-left: 4px;
}

/* Messages area */
.context-messages {
  border-top: 1px solid var(--color-border, var(--border));
}
.messages-scroll {
  max-height: 500px;
  overflow-y: auto;
  padding: 8px;
}
.context-msg {
  padding: 10px 14px;
  border-radius: 8px;
  margin-bottom: 6px;
  border-left: 3px solid transparent;
}
.context-msg.role-user {
  background: rgba(30, 58, 95, 0.3);
  border-left-color: #3b82f6;
}
.context-msg.role-assistant {
  background: rgba(13, 26, 18, 0.3);
  border-left-color: #10b981;
}
.context-msg.role-system {
  background: rgba(99, 102, 241, 0.1);
  border-left-color: #6366f1;
}
.msg-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.msg-role-badge {
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 4px;
}
.msg-role-badge.role-user {
  background: rgba(59, 130, 246, 0.2);
  color: #60a5fa;
}
.msg-role-badge.role-assistant {
  background: rgba(16, 185, 129, 0.2);
  color: #34d399;
}
.msg-role-badge.role-system {
  background: rgba(99, 102, 241, 0.2);
  color: #818cf8;
}
.msg-tool-badge {
  font-size: 11px;
  padding: 2px 6px;
  border-radius: 4px;
  background: rgba(245, 158, 11, 0.15);
  color: #fbbf24;
}
.msg-tool-badge.result {
  background: rgba(16, 185, 129, 0.15);
  color: #34d399;
}
.msg-index {
  font-size: 10px;
  color: var(--color-text-subtle, var(--text-subtle));
  margin-left: auto;
}
.msg-content {
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
  color: var(--color-text-secondary, var(--text-dim));
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.5;
  margin: 0;
  max-height: 200px;
  overflow-y: auto;
  cursor: pointer;
  transition: max-height 0.3s ease;
}
.msg-content-full {
  max-height: none;
}
.context-msg {
  cursor: pointer;
}
.context-msg:hover {
  background: rgba(255, 255, 255, 0.02);
}
.msg-expanded {
  border-left: 2px solid var(--color-accent, #3b82f6);
}

/* ── Context Versions (compaction timeline) ── */
.version-live-banner {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  margin-bottom: 12px;
  border-radius: 8px;
  background: var(--warning-bg, rgba(245, 158, 11, 0.1));
  color: var(--warning, #f59e0b);
  font-size: 13px;
}
.version-live-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--warning, #f59e0b);
  /* reuses the existing `pulse` keyframes defined above (context section) */
  animation: pulse 1.2s ease-in-out infinite;
}
.version-retry-btn {
  background: var(--bg-3, #161922);
  border: 1px solid var(--border-strong, rgba(255, 255, 255, 0.12));
  color: var(--text, #f1f2f6);
  font-size: 12px;
  border-radius: 6px;
  padding: 3px 10px;
  cursor: pointer;
}
.version-retry-btn:hover {
  border-color: var(--primary, #3b82f6);
}
.version-status {
  font-size: 12px;
  font-weight: 600;
}
.version-status-ok { color: var(--success, #10b981); }
.version-status-fail { color: var(--danger, #ef4444); }
.version-trigger {
  font-size: 11px;
  color: var(--text-muted, #7b8094);
  border: 1px solid var(--border, rgba(255, 255, 255, 0.06));
  border-radius: 4px;
  padding: 1px 6px;
}
.version-saved {
  font-size: 12px;
  font-weight: 600;
  color: var(--success, #10b981);
  background: var(--success-bg, rgba(16, 185, 129, 0.1));
  border-radius: 4px;
  padding: 2px 8px;
}
.version-error-preview {
  font-size: 12px;
  color: var(--danger, #ef4444);
  max-width: 360px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.version-detail {
  border-top: 1px solid var(--border, rgba(255, 255, 255, 0.06));
}
.version-detail-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
}
.version-tab-btn {
  background: var(--bg-3, #161922);
  border: 1px solid var(--border, rgba(255, 255, 255, 0.06));
  color: var(--text-dim, #b6bac6);
  font-size: 12px;
  border-radius: 6px;
  padding: 4px 10px;
  cursor: pointer;
}
.version-tab-btn.active {
  color: var(--text, #f1f2f6);
  border-color: var(--primary, #3b82f6);
}
.version-meta {
  margin-left: auto;
  font-size: 11px;
  color: var(--text-muted, #7b8094);
}
.version-risks {
  margin: 0 14px 8px;
  padding: 8px 10px;
  border-radius: 6px;
  background: var(--warning-bg, rgba(245, 158, 11, 0.1));
  color: var(--warning, #f59e0b);
  font-size: 12px;
}
.version-summary-pre {
  margin: 0 14px 14px;
  padding: 12px;
  border-radius: 8px;
  background: var(--bg-0, #07080b);
  color: var(--text-dim, #b6bac6);
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 360px;
  overflow-y: auto;
}
.version-diff {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
  padding: 0 14px 14px;
}
.version-diff-col {
  min-width: 0;
}
.version-diff-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-dim, #b6bac6);
  margin-bottom: 6px;
}
.version-diff-msg {
  display: flex;
  gap: 6px;
  align-items: baseline;
  font-size: 11px;
  padding: 4px 6px;
  border-radius: 4px;
  margin-bottom: 2px;
  background: var(--bg-2, #11141b);
}
.version-diff-msg.diff-dropped { opacity: 0.45; text-decoration: line-through; }
.version-diff-msg.diff-summary { border-left: 2px solid var(--primary, #3b82f6); }
.version-diff-msg.diff-truncated { border-left: 2px solid var(--warning, #f59e0b); }
.version-diff-role {
  flex: none;
  font-weight: 600;
  color: var(--text-muted, #7b8094);
  text-transform: uppercase;
  font-size: 10px;
}
.version-diff-disposition {
  flex: none;
  font-size: 10px;
  color: var(--text-muted, #7b8094);
}
.version-diff-preview {
  color: var(--text-dim, #b6bac6);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
@media (max-width: 720px) {
  .version-diff {
    grid-template-columns: 1fr;
  }
}

/* ── Mobile Responsive ── */

</style>

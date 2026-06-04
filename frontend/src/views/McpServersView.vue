<script setup>
/**
 * MCP Servers — DB-backed catalog UI.
 *
 * Logic preserved verbatim from the previous McpServersView (CRUD, env
 * masking, smoke test, attach/detach, SSE live events). Only the layout
 * and styling were rewritten to match the redesign.
 */
import { ref, computed, onMounted, onUnmounted, reactive, watch } from 'vue'
import { apiFetch, ApiError, buildSSEUrl } from '../api'
import { useAgentsStore } from '../stores/agents'
import { useConfirm } from '../composables/useConfirm'
import { useToast } from '../composables/useToast'
import { useSSEConnection } from '../composables/useSSEConnection.js'
import { useBreakpoint } from '../composables/useBreakpoint'

const { isMobile } = useBreakpoint()

const agentsStore = useAgentsStore()
const { confirm } = useConfirm()
const toast = useToast()
function pushToast({ type = 'info', message }) {
  ;(toast[type] || toast.info)(message)
}

const servers = ref([])
const events = ref([])
const loading = ref(false)
const loadError = ref('')
const selectedName = ref('')
// On mobile the master-detail collapses to one pane at a time.
// `isMobileDetailView` is true when the user has tapped a row → show
// detail with a back button; false → show list. Mirrors ApprovalsView.
const isMobileDetailView = computed(() => isMobile.value && !!selectedName.value)
function clearMobileSelection() { selectedName.value = '' }
const search = ref('')
const filterMode = ref('all') // all | running | stopped | error
const tab = ref('config') // config | agents | events

const editing = reactive({
  open: false,
  isCreate: false,
  name: '',
  transport: 'stdio',
  command: '',
  argsText: '',
  envRows: [],
  url: '',
  saving: false,
  error: '',
  smokeStatus: '',
  smokeError: '',
})

const filtered = computed(() => {
  const q = search.value.trim().toLowerCase()
  return servers.value.filter((s) => {
    if (filterMode.value === 'running' && s.status !== 'running') return false
    if (filterMode.value === 'stopped' && s.status !== 'stopped') return false
    if (filterMode.value === 'error' && s.status !== 'error') return false
    if (!q) return true
    return (s.name + ' ' + (s.command || '') + ' ' + (s.url || '')).toLowerCase().includes(q)
  })
})

const selected = computed(() => servers.value.find((s) => s.name === selectedName.value) || null)

const statusCounts = computed(() => ({
  total: servers.value.length,
  running: servers.value.filter(s => s.status === 'running').length,
  stopped: servers.value.filter(s => s.status === 'stopped').length,
  error: servers.value.filter(s => s.status === 'error').length,
}))

const agentChoices = computed(() =>
  [...agentsStore.agents.values()].map((a) => ({
    name: a.name,
    type: a.type,
    is_card_based: a.type === 'card',
  })),
)
function unattachedAgents(server) {
  if (!server) return []
  const attached = new Set(server.attached_agents || [])
  return agentChoices.value.filter((a) => !attached.has(a.name))
}

const attachOpen = ref(false)
const attachMenuPos = ref({ top: null, bottom: null, left: 0, width: 240, maxHeight: 320 })
const attachTriggerRef = ref(null)
function _measureAttachAnchor() {
  const el = attachTriggerRef.value
  if (!el) return
  const r = el.getBoundingClientRect()
  const vw = window.innerWidth
  const vh = window.innerHeight
  const GAP = 4
  // Reserve room for the mobile bottom tab bar / mini player so the menu
  // never opens behind them (the "+ Attach to…" trigger sits low in the
  // detail pane → downward menu was clipped off-screen).
  const BOTTOM_RESERVE = 88
  const DESIRED = 320
  const width = Math.max(r.width, 240)
  const left = Math.max(8, Math.min(r.left, vw - width - 8))
  const spaceBelow = vh - r.bottom - GAP - BOTTOM_RESERVE
  const spaceAbove = r.top - GAP
  if (spaceBelow >= 160 || spaceBelow >= spaceAbove) {
    // Open downward, capped to the space above the reserved bottom area.
    attachMenuPos.value = {
      top: r.bottom + GAP, bottom: null, left, width,
      maxHeight: Math.max(140, Math.min(DESIRED, spaceBelow)),
    }
  } else {
    // Not enough room below — flip up, anchoring the menu's bottom just
    // above the trigger.
    attachMenuPos.value = {
      top: null, bottom: vh - r.top + GAP, left, width,
      maxHeight: Math.max(140, Math.min(DESIRED, spaceAbove)),
    }
  }
}
function toggleAttachMenu() {
  attachOpen.value = !attachOpen.value
  if (attachOpen.value) _measureAttachAnchor()
}
function closeAttachMenu(ev) {
  const t = ev.target
  if (t?.closest?.('.mcp-attach-wrap') || t?.closest?.('.mcp-attach-menu')) return
  attachOpen.value = false
}
function _onWindowScrollOrResize() {
  if (attachOpen.value) _measureAttachAnchor()
}

// ── load ────────────────────────────────────────────────────────────

async function loadServers() {
  loading.value = true
  loadError.value = ''
  try {
    const data = await apiFetch('/api/mcp/servers')
    servers.value = data.servers || []
    if (selectedName.value && !servers.value.find((s) => s.name === selectedName.value)) {
      selectedName.value = ''
    } else if (selectedName.value) {
      await loadServerDetail(selectedName.value)
    }
  } catch (err) {
    loadError.value = _friendly(err)
  } finally {
    loading.value = false
  }
}

async function loadServerDetail(name) {
  try {
    const detail = await apiFetch(`/api/mcp/servers/${encodeURIComponent(name)}`)
    const idx = servers.value.findIndex((s) => s.name === name)
    if (idx >= 0) servers.value[idx] = { ...servers.value[idx], ...detail }
  } catch (err) {
    console.warn('[mcp] detail fetch failed for', name, err)
  }
}

watch(selectedName, (n) => {
  if (n) loadServerDetail(n)
})

async function loadEvents() {
  try {
    const data = await apiFetch('/api/mcp/events?limit=50')
    events.value = data.events || []
  } catch (err) {
    console.error('[mcp] loadEvents failed', err)
  }
}

function _friendly(err) {
  if (err instanceof ApiError && err.body && typeof err.body === 'object') {
    return err.body.message || err.body.detail?.message || err.message
  }
  return err?.message || String(err)
}

// ── editor open helpers ─────────────────────────────────────────────

function openCreate() {
  editing.open = true
  editing.isCreate = true
  editing.name = ''
  editing.transport = 'stdio'
  editing.command = ''
  editing.argsText = ''
  editing.envRows = []
  editing.url = ''
  editing.error = ''
  editing.smokeStatus = ''
  editing.smokeError = ''
}

function openEdit(server) {
  editing.open = true
  editing.isCreate = false
  editing.name = server.name
  editing.transport = server.transport
  editing.command = server.command || ''
  editing.argsText = (server.args || []).join('\n')
  editing.envRows = Object.entries(server.env || {}).map(([k, v]) => ({
    key: k,
    value: v,
    masked: v === '••••',
    revealed: false,
  }))
  editing.url = server.url || ''
  editing.error = ''
  editing.smokeStatus = ''
  editing.smokeError = ''
}

function closeEditor() {
  editing.open = false
}

function addEnvRow() {
  editing.envRows.push({ key: '', value: '', masked: false, revealed: true })
}

function removeEnvRow(idx) {
  editing.envRows.splice(idx, 1)
}

async function revealSecret(idx) {
  const row = editing.envRows[idx]
  if (!row.masked) return
  try {
    const data = await apiFetch(`/api/mcp/servers/${editing.name}/secret/${encodeURIComponent(row.key)}`)
    row.value = data.value
    row.revealed = true
  } catch (err) {
    pushToast({ type: 'error', message: 'Failed to reveal secret: ' + _friendly(err) })
  }
}

function hideSecret(idx) {
  const row = editing.envRows[idx]
  row.value = '••••'
  row.revealed = false
}

function _buildPayload() {
  const args = editing.argsText
    .split('\n')
    .map((s) => s.trim())
    .filter((s) => s.length > 0)
  const env = {}
  for (const row of editing.envRows) {
    if (!row.key) continue
    if (row.masked && !row.revealed) continue
    env[row.key] = row.value
  }
  const payload = { transport: editing.transport }
  if (editing.transport === 'stdio') {
    payload.command = editing.command
    payload.args = args
  } else {
    payload.url = editing.url
  }
  if (Object.keys(env).length > 0) payload.env = env
  return payload
}

async function saveServer() {
  editing.saving = true
  editing.error = ''
  editing.smokeStatus = ''
  editing.smokeError = ''
  try {
    if (editing.isCreate) {
      const payload = { name: editing.name, ..._buildPayload() }
      await apiFetch('/api/mcp/servers', { method: 'POST', body: JSON.stringify(payload) })
      pushToast({ type: 'success', message: `Created ${editing.name}` })
    } else {
      const payload = _buildPayload()
      const resp = await apiFetch(`/api/mcp/servers/${editing.name}`, {
        method: 'PUT',
        body: JSON.stringify(payload),
      })
      if (resp.fanout && !resp.fanout.all_ok) {
        const failures = resp.fanout.agents.filter((a) => !a.ok).map((a) => `${a.agent}: ${a.error}`).join('; ')
        pushToast({ type: 'warning', message: `Saved with reconnect failures — ${failures}` })
      } else {
        pushToast({ type: 'success', message: `Updated ${editing.name}` })
      }
    }
    closeEditor()
    await loadServers()
    await loadEvents()
  } catch (err) {
    if (err instanceof ApiError && err.body?.smoke_failed) {
      editing.smokeStatus = 'fail'
      editing.smokeError = _friendly(err)
    } else {
      editing.error = _friendly(err)
    }
  } finally {
    editing.saving = false
  }
}

async function testCurrent() {
  editing.saving = true
  editing.smokeStatus = ''
  editing.smokeError = ''
  try {
    const result = editing.isCreate
      ? await apiFetch('/api/mcp/servers/test', {
          method: 'POST',
          body: JSON.stringify(_buildPayload()),
        })
      : await apiFetch(`/api/mcp/servers/${editing.name}/test`, { method: 'POST' })
    if (result.ok) {
      editing.smokeStatus = 'ok'
      editing.smokeError = `OK — ${(result.tools || []).length} tools discovered`
    } else {
      editing.smokeStatus = 'fail'
      editing.smokeError = result.error || 'unknown error'
    }
  } catch (err) {
    editing.smokeStatus = 'fail'
    editing.smokeError = _friendly(err)
  } finally {
    editing.saving = false
  }
}

const refreshingTools = ref(false)
async function refreshTools(server) {
  refreshingTools.value = true
  try {
    const result = await apiFetch(`/api/mcp/servers/${server.name}/test`, { method: 'POST' })
    if (result.ok) {
      pushToast({
        type: 'success',
        message: `Connected to ${server.name} — ${(result.tools || []).length} tools refreshed`,
      })
      await loadServerDetail(server.name)
    } else {
      pushToast({ type: 'error', message: `Test failed: ${result.error || 'unknown'}` })
    }
  } catch (err) {
    pushToast({ type: 'error', message: _friendly(err) })
  } finally {
    refreshingTools.value = false
  }
}

async function deleteServer(server) {
  const ok = await confirm({
    title: `Delete ${server.name}?`,
    message: server.attached_agents?.length
      ? `Will detach from ${server.attached_agents.length} agent(s) and delete the server. Any in-flight tool calls will be interrupted.`
      : 'Server will be removed from the catalog.',
    confirmText: 'Delete',
    variant: 'danger',
  })
  if (!ok) return
  try {
    await apiFetch(`/api/mcp/servers/${server.name}`, { method: 'DELETE' })
    pushToast({ type: 'success', message: `Deleted ${server.name}` })
    if (selectedName.value === server.name) selectedName.value = ''
    await loadServers()
    await loadEvents()
  } catch (err) {
    pushToast({ type: 'error', message: _friendly(err) })
  }
}

const attaching = ref(false)

async function attachToAgent(server, agent) {
  attaching.value = true
  attachOpen.value = false
  const path = `/api/mcp/servers/${server.name}/agents/${encodeURIComponent(agent.name)}`
  try {
    const result = await apiFetch(path, { method: 'POST' })
    if (result.warning) {
      pushToast({ type: 'warning', message: `Persisted but runtime: ${result.warning}` })
    } else {
      pushToast({
        type: 'success',
        message: `Attached ${server.name} → ${agent.name} (+${result.tools_added?.length || 0} tools)`,
      })
    }
    await loadServers()
    await loadEvents()
  } catch (err) {
    pushToast({ type: 'error', message: _friendly(err) })
  } finally {
    attaching.value = false
  }
}

async function detachFromAgent(server, agentName) {
  attaching.value = true
  const path = `/api/mcp/servers/${server.name}/agents/${encodeURIComponent(agentName)}`
  try {
    await apiFetch(path, { method: 'DELETE' })
    pushToast({ type: 'success', message: `Detached ${server.name} from ${agentName}` })
    await loadServers()
    await loadEvents()
  } catch (err) {
    pushToast({ type: 'error', message: _friendly(err) })
  } finally {
    attaching.value = false
  }
}

useSSEConnection(buildSSEUrl('/api/mcp/events/stream'), {
  onMessage(msg) {
    try {
      const event = JSON.parse(msg.data)
      events.value.unshift({
        id: `live-${Date.now()}-${Math.random()}`,
        timestamp: event.ts,
        action: event.action,
        server: event.server,
        agent: event.agent,
        actor: 'live',
        outcome: event.outcome,
        duration_ms: event.duration_ms,
        detail: event.detail || {},
      })
      if (events.value.length > 200) events.value.length = 200
      if (['create', 'update', 'delete', 'attach', 'detach'].includes(event.action)) {
        loadServers()
      }
    } catch (e) {
      console.warn('[mcp.sse] parse failed', e)
    }
  },
})

onMounted(async () => {
  await Promise.all([loadServers(), loadEvents(), agentsStore.fetchAgents()])
  document.addEventListener('click', closeAttachMenu)
  window.addEventListener('scroll', _onWindowScrollOrResize, true)
  window.addEventListener('resize', _onWindowScrollOrResize)
})

onUnmounted(() => {
  document.removeEventListener('click', closeAttachMenu)
  window.removeEventListener('scroll', _onWindowScrollOrResize, true)
  window.removeEventListener('resize', _onWindowScrollOrResize)
})

function fmtTime(ts) {
  if (!ts) return ''
  return new Date(ts * 1000).toLocaleTimeString()
}
</script>

<template>
  <div class="mcp jv">
    <!-- ─── Header ─── -->
    <div class="mcp__header">
      <div class="mcp__heading">
        <div class="eyebrow">SYSTEM · MCP SERVERS</div>
        <h1 class="mcp__title">
          <span class="grad" style="font-style: italic;">{{ statusCounts.total }}</span> MCP servers
          <span class="mcp__title-sub">
            · {{ statusCounts.running }} running · {{ statusCounts.stopped }} stopped · {{ statusCounts.error }} error
          </span>
        </h1>
        <p class="mcp__desc">
          DB-backed catalog. Built-ins seeded from
          <code class="mcp__inline-code">fastagent.config.yaml</code> on first boot — editable but not deletable.
        </p>
      </div>
      <button class="btn btn-primary" @click="openCreate">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round">
          <line x1="12" y1="5" x2="12" y2="19"/>
          <line x1="5" y1="12" x2="19" y2="12"/>
        </svg>
        New server
      </button>
    </div>

    <!-- ─── Toolbar ─── -->
    <div class="mcp__toolbar">
      <div class="mcp__search">
        <svg viewBox="0 0 24 24" fill="none" width="14" height="14" stroke="currentColor" stroke-width="1.8">
          <circle cx="11" cy="11" r="7"/>
          <line x1="21" y1="21" x2="16.65" y2="16.65" stroke-linecap="round"/>
        </svg>
        <input v-model="search" placeholder="Filter by name, command, or url…" class="mcp__search-input" />
      </div>
      <div class="seg">
        <button :class="{ 'is-active': filterMode === 'all' }" @click="filterMode = 'all'">
          All {{ statusCounts.total }}
        </button>
        <button :class="{ 'is-active': filterMode === 'running' }" @click="filterMode = 'running'">
          ● {{ statusCounts.running }}
        </button>
        <button :class="{ 'is-active': filterMode === 'stopped' }" @click="filterMode = 'stopped'">
          ○ {{ statusCounts.stopped }}
        </button>
        <button :class="{ 'is-active': filterMode === 'error' }" @click="filterMode = 'error'">
          ! {{ statusCounts.error }}
        </button>
      </div>
      <button class="btn btn-secondary" @click="loadServers" :disabled="loading">
        {{ loading ? 'Loading…' : '↻ Refresh' }}
      </button>
    </div>

    <div v-if="loadError" class="mcp__error">
      {{ loadError }}
      <button class="btn btn-secondary" @click="loadServers">Retry</button>
    </div>

    <!-- ─── Two-pane layout ───
         On mobile the panes collapse to one-at-a-time: list when no
         row is selected, detail (with back button) when one is. The
         class hooks below let CSS hide the inactive pane. -->
    <div
      class="mcp__layout"
      :class="{
        'mcp__layout--mobile-list': isMobile && !isMobileDetailView,
        'mcp__layout--mobile-detail': isMobileDetailView,
      }"
    >
      <!-- Left: server list -->
      <aside class="mcp__list">
        <button
          v-for="s in filtered"
          :key="s.name"
          type="button"
          class="mcp-row"
          :class="{ 'mcp-row--active': selectedName === s.name }"
          @click="selectedName = s.name"
        >
          <div class="mcp-row__head">
            <code class="mcp-row__name">{{ s.name }}</code>
            <span class="mcp-row__dot" :class="`mcp-row__dot--${s.status}`"></span>
          </div>
          <div class="mcp-row__meta">
            <span>{{ s.transport }}</span>
            <span>· {{ (s.tools || []).length || '—' }} tools</span>
            <span>· {{ (s.attached_agents || []).length }} agents</span>
            <span v-if="s.is_builtin" class="mcp-row__lock">· 🔒</span>
          </div>
        </button>
        <div v-if="!filtered.length && !loading" class="mcp__empty">No servers match.</div>
      </aside>

      <!-- Right: detail -->
      <section v-if="selected" class="mcp__detail">
        <!-- Mobile back button — returns to the list pane. -->
        <button
          v-if="isMobile"
          class="mcp-back-btn"
          @click="clearMobileSelection"
          aria-label="Back to list"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
            <path d="M19 12H5M12 19l-7-7 7-7" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
          <span>Servers</span>
        </button>
        <header class="mcp-detail__head">
          <div class="mcp-detail__head-main">
            <code class="mcp-detail__name">{{ selected.name }}</code>
            <span class="mcp-detail__pill" :class="`mcp-detail__pill--${selected.status}`">
              <span class="mcp-detail__pill-dot"></span>
              {{ selected.status }}
            </span>
            <span v-if="selected.is_builtin" class="mcp-detail__builtin">🔒 BUILTIN</span>
          </div>
          <div class="mcp-detail__actions">
            <button class="btn btn-secondary" :disabled="refreshingTools" @click="refreshTools(selected)">
              {{ refreshingTools ? 'Testing…' : '✓ Test & refresh' }}
            </button>
            <button class="btn btn-secondary" @click="openEdit(selected)">Edit</button>
            <button
              class="btn btn-icon btn-ghost mcp-detail__delete"
              @click="deleteServer(selected)"
              :disabled="selected.is_builtin"
              :title="selected.is_builtin ? 'Built-in servers cannot be deleted' : 'Delete'"
            >
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
                <polyline points="3 6 5 6 21 6"/>
                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
              </svg>
            </button>
          </div>
        </header>

        <nav class="mcp-tabs">
          <button :class="{ 'is-active': tab === 'config' }" @click="tab = 'config'">Config</button>
          <button :class="{ 'is-active': tab === 'agents' }" @click="tab = 'agents'">
            Agents <span class="mcp-tabs__count">{{ (selected.attached_agents || []).length }}</span>
          </button>
          <button :class="{ 'is-active': tab === 'events' }" @click="tab = 'events'">Events</button>
        </nav>

        <!-- Config tab -->
        <div v-if="tab === 'config'" class="mcp-tab">
          <div class="hud mcp-config">
            <div class="hud-br"></div>
            <div class="mono-label mcp-config__label">● COMMAND + ENV</div>
            <div class="mcp-config__body">
              <div><span class="mcp-config__k">command:</span> <span class="mcp-config__v-warm">{{ selected.command || '—' }}</span></div>
              <div><span class="mcp-config__k">args:</span> <span class="mcp-config__v">{{ JSON.stringify(selected.args || []) }}</span></div>
              <div v-if="selected.url"><span class="mcp-config__k">url:</span> <span class="mcp-config__v">{{ selected.url }}</span></div>
              <div class="mcp-config__env-head">env:</div>
              <div
                v-for="(v, k) in selected.env || {}"
                :key="k"
                class="mcp-config__env-row"
              >
                <code class="mcp-config__env-k">{{ k }}</code>
                <span class="mcp-config__eq">=</span>
                <code class="mcp-config__env-v" :class="{ 'mcp-config__env-v--secret': v === '••••' }">{{ v }}</code>
              </div>
              <div v-if="!Object.keys(selected.env || {}).length" class="mcp-config__none">(no env vars)</div>
            </div>
          </div>

          <!-- Tools -->
          <div class="mcp-section">
            <div class="mcp-section__head">
              <h3 class="mcp-section__title">
                Tools <span class="mcp-section__subcount">({{ (selected.tools || []).length }} discovered)</span>
              </h3>
              <button
                class="btn btn-ghost mcp-section__action"
                :disabled="refreshingTools"
                :title="`Re-run smoke test for ${selected.name}`"
                @click="refreshTools(selected)"
              >
                {{ refreshingTools ? 'Refreshing…' : '↻ Refresh tools' }}
              </button>
            </div>
            <div class="mcp-tool-list">
              <div v-for="t in selected.tools || []" :key="t.name || t" class="mcp-tool">
                <code class="mcp-tool__name">{{ t.name || t }}</code>
                <span v-if="t.description" class="mcp-tool__desc">{{ t.description }}</span>
              </div>
              <div v-if="!(selected.tools || []).length" class="mcp-tool mcp-tool--empty">
                (none discovered — click <em>Refresh tools</em> to fetch from a live connection)
              </div>
            </div>
          </div>
        </div>

        <!-- Agents tab -->
        <div v-if="tab === 'agents'" class="mcp-tab">
          <div class="mcp-section">
            <h3 class="mcp-section__title">Currently attached</h3>
            <div v-if="(selected.attached_agents || []).length" class="mcp-attached">
              <span
                v-for="a in selected.attached_agents"
                :key="a"
                class="mcp-agent-chip"
              >
                <code>{{ a }}</code>
                <button
                  class="mcp-agent-chip__detach"
                  type="button"
                  :title="`Detach from ${a}`"
                  :disabled="attaching"
                  @click="detachFromAgent(selected, a)"
                >×</button>
              </span>
            </div>
            <p v-else class="mcp-section__muted">Not attached to any agent.</p>

            <h3 class="mcp-section__title" style="margin-top: 18px;">Add to agent</h3>
            <div class="mcp-attach-wrap">
              <button ref="attachTriggerRef" class="btn btn-secondary" @click.stop="toggleAttachMenu">
                + Attach to…
              </button>
            </div>
          </div>
        </div>

        <Teleport to="body">
          <div
            v-if="attachOpen && tab === 'agents'"
            class="mcp-attach-menu jv"
            :style="{
              top: attachMenuPos.top != null ? attachMenuPos.top + 'px' : undefined,
              bottom: attachMenuPos.bottom != null ? attachMenuPos.bottom + 'px' : undefined,
              left: attachMenuPos.left + 'px',
              minWidth: attachMenuPos.width + 'px',
              maxHeight: attachMenuPos.maxHeight + 'px',
            }"
            @click.stop
          >
            <div v-if="!unattachedAgents(selected).length" class="mcp-attach-menu__empty">
              Already attached to all agents.
            </div>
            <button
              v-for="a in unattachedAgents(selected)"
              :key="a.name"
              class="mcp-attach-menu__item"
              :disabled="attaching"
              @click="attachToAgent(selected, a)"
            >
              <span>{{ a.name }}</span>
              <span v-if="!a.is_card_based" class="mcp-attach-menu__warn" title="Code-based agent — change reverts on restart">
                runtime
              </span>
            </button>
          </div>
        </Teleport>

        <!-- Events tab -->
        <div v-if="tab === 'events'" class="mcp-tab">
          <p class="mcp-section__muted">Live + history, newest first. Filtered to this server.</p>
          <div class="mcp-event-list">
            <div
              v-for="ev in events.filter(e => e.server === selected.name)"
              :key="ev.id"
              class="mcp-event"
            >
              <span class="mcp-event__time">{{ fmtTime(ev.timestamp) }}</span>
              <span class="mcp-event__action">{{ ev.action }}</span>
              <span v-if="ev.agent" class="mcp-event__agent">{{ ev.agent }}</span>
              <span class="mcp-event__outcome" :class="`mcp-event__outcome--${ev.outcome}`">{{ ev.outcome }}</span>
              <span v-if="ev.duration_ms != null" class="mcp-event__duration">{{ ev.duration_ms }}ms</span>
              <details v-if="ev.detail && Object.keys(ev.detail).length" class="mcp-event__detail">
                <summary>detail</summary>
                <pre>{{ JSON.stringify(ev.detail, null, 2) }}</pre>
              </details>
            </div>
            <div v-if="!events.filter(e => e.server === selected.name).length" class="mcp-event mcp-event--empty">
              (no events yet)
            </div>
          </div>
        </div>
      </section>

      <section v-else class="mcp__detail mcp__detail--empty">
        <p class="mcp-section__muted">Select a server on the left, or create a new one.</p>
      </section>
    </div>

    <!-- ─── Editor Modal ─── -->
    <Teleport to="body">
      <div v-if="editing.open" class="mcp-modal-overlay jv" @click.self="closeEditor">
        <div class="mcp-modal">
          <header class="mcp-modal__head">
            <h3>{{ editing.isCreate ? 'New MCP Server' : `Edit ${editing.name}` }}</h3>
            <button class="btn btn-icon btn-ghost" @click="closeEditor" aria-label="Close">×</button>
          </header>
          <div class="mcp-modal__body">
            <label class="mcp-field">
              <span>Name</span>
              <input v-model="editing.name" :disabled="!editing.isCreate" placeholder="my-tool" class="mcp-input" />
            </label>
            <label class="mcp-field">
              <span>Transport</span>
              <select v-model="editing.transport" class="mcp-input">
                <option value="stdio">stdio</option>
                <option value="http">http</option>
                <option value="sse">sse</option>
              </select>
            </label>
            <template v-if="editing.transport === 'stdio'">
              <label class="mcp-field">
                <span>Command</span>
                <input v-model="editing.command" placeholder="python" class="mcp-input" />
              </label>
              <label class="mcp-field">
                <span>Args (one per line)</span>
                <textarea v-model="editing.argsText" rows="4" class="mcp-input mcp-input--mono" placeholder="-m&#10;my_module" />
              </label>
            </template>
            <template v-else>
              <label class="mcp-field">
                <span>URL</span>
                <input v-model="editing.url" placeholder="https://example.com/mcp" class="mcp-input" />
              </label>
            </template>
            <div class="mcp-field">
              <span>Env vars</span>
              <div class="mcp-env-table" v-if="editing.envRows.length">
                <div class="mcp-env-row" v-for="(row, idx) in editing.envRows" :key="idx">
                  <input v-model="row.key" placeholder="KEY" class="mcp-input mcp-input--mono" />
                  <input
                    v-model="row.value"
                    :type="row.masked && !row.revealed ? 'password' : 'text'"
                    placeholder="value"
                    class="mcp-input mcp-input--mono"
                    :readonly="row.masked && !row.revealed"
                  />
                  <button v-if="row.masked && !row.revealed" type="button" class="btn btn-icon btn-ghost" @click="revealSecret(idx)" title="Reveal">
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
                  </button>
                  <button v-else-if="row.masked && row.revealed" type="button" class="btn btn-icon btn-ghost" @click="hideSecret(idx)" title="Hide">
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>
                  </button>
                  <button type="button" class="btn btn-icon btn-ghost mcp-env-row__remove" @click="removeEnvRow(idx)" title="Remove">
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
                  </button>
                </div>
              </div>
              <button type="button" class="btn btn-ghost mcp-env-add" @click="addEnvRow">+ Add env var</button>
            </div>

            <div v-if="editing.smokeStatus === 'ok'" class="mcp-banner mcp-banner--success">{{ editing.smokeError }}</div>
            <div v-if="editing.smokeStatus === 'fail'" class="mcp-banner mcp-banner--error">Smoke test failed: {{ editing.smokeError }}</div>
            <div v-if="editing.error" class="mcp-banner mcp-banner--error">{{ editing.error }}</div>
          </div>
          <footer class="mcp-modal__foot">
            <button class="btn btn-secondary" @click="testCurrent" :disabled="editing.saving">Test connection</button>
            <button class="btn btn-primary" @click="saveServer" :disabled="editing.saving">
              {{ editing.saving ? 'Saving…' : 'Save' }}
            </button>
          </footer>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.mcp {
  max-width: 1200px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 14px;
  color: var(--text);
}

/* Header */
.mcp__header {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  gap: 16px;
  padding-bottom: 14px;
  border-bottom: 1px solid var(--border);
}
.mcp__heading { display: flex; flex-direction: column; gap: 4px; }
.mcp__title {
  font-family: var(--font-display);
  font-size: 22px;
  letter-spacing: -0.02em;
  margin: 4px 0 0;
}
.mcp__title-sub {
  color: var(--text-muted);
  font-size: 14px;
  font-weight: 400;
  margin-left: 6px;
}
.mcp__desc {
  font-size: 12.5px;
  color: var(--text-dim);
  max-width: 720px;
}
.mcp__inline-code {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--accent);
}

/* Toolbar */
.mcp__toolbar {
  display: flex;
  gap: 12px;
  align-items: center;
  flex-wrap: wrap;
}
.mcp__search {
  flex: 1;
  min-width: 220px;
  display: flex;
  align-items: center;
  gap: 8px;
  height: 34px;
  padding: 0 12px;
  background: var(--bg-2);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  color: var(--text-muted);
}
.mcp__search:focus-within { border-color: var(--primary); color: var(--text); }
.mcp__search-input {
  flex: 1;
  background: transparent;
  border: 0;
  outline: 0;
  color: var(--text);
  font-family: var(--font-body);
  font-size: 13px;
}
.mcp__search-input::placeholder { color: var(--text-muted); }

.mcp__error {
  padding: 8px 12px;
  background: var(--danger-bg);
  border: 1px solid rgba(239, 68, 68, 0.2);
  border-radius: var(--r-md);
  color: var(--danger);
  font-size: 12.5px;
  display: flex;
  gap: 8px;
  align-items: center;
}

/* Layout */
.mcp__layout {
  display: grid;
  grid-template-columns: 320px 1fr;
  gap: 14px;
  align-items: start;
  min-height: 320px;
}

/* Server list (left) */
.mcp__list {
  display: flex;
  flex-direction: column;
  gap: 4px;
  background: var(--bg-1);
  border: 1px solid var(--border);
  border-radius: var(--r-lg);
  padding: 8px;
  max-height: calc(100vh - 240px);
  overflow-y: auto;
}
.mcp-row {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px 12px;
  background: transparent;
  border: 1px solid transparent;
  border-left: 3px solid transparent;
  border-radius: var(--r-md);
  cursor: pointer;
  text-align: left;
  transition: background 0.12s var(--ease-out), border-color 0.12s var(--ease-out);
}
.mcp-row:hover { background: var(--bg-2); }
.mcp-row--active {
  background: var(--primary-bg);
  border-left-color: var(--primary);
}
.mcp-row__head {
  display: flex;
  align-items: center;
  gap: 8px;
  justify-content: space-between;
}
.mcp-row__name {
  font-family: var(--font-mono);
  font-size: 12.5px;
  font-weight: 500;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
  flex: 1;
}
.mcp-row__dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.mcp-row__dot--running { background: var(--success); box-shadow: 0 0 6px rgba(16, 185, 129, 0.45); }
.mcp-row__dot--stopped { background: var(--text-muted); }
.mcp-row__dot--error { background: var(--danger); box-shadow: 0 0 6px rgba(239, 68, 68, 0.45); }
.mcp-row__dot--unknown { background: var(--bg-4); }
.mcp-row__meta {
  display: flex;
  gap: 6px;
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-muted);
  letter-spacing: 0.04em;
  flex-wrap: wrap;
}
.mcp-row__lock { color: var(--text-subtle); }

.mcp__empty {
  padding: 24px 12px;
  text-align: center;
  color: var(--text-muted);
  font-size: 12.5px;
}

/* Detail panel (right) */
.mcp__detail {
  background: var(--bg-1);
  border: 1px solid var(--border);
  border-radius: var(--r-lg);
  padding: 18px;
  max-height: calc(100vh - 240px);
  overflow-y: auto;
}
.mcp__detail--empty {
  min-height: 320px;
  display: flex;
  align-items: center;
  justify-content: center;
  /* In light theme the parent card (.mcp__detail) is the same white as
     the page → empty state had no edge at all (2026-05-27 "no border"
     report). Dashed border + dim bg makes the slot visible as an
     "empty placeholder waiting to be filled" rather than dead space. */
  background: var(--bg-2);
  border: 1px dashed var(--border-strong);
  border-radius: var(--r-md);
  color: var(--text-muted);
}

.mcp-detail__head {
  display: flex;
  align-items: center;
  gap: 12px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--border);
  flex-wrap: wrap;
}
.mcp-detail__head-main {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: 1;
  min-width: 0;
}
.mcp-detail__name {
  font-family: var(--font-mono);
  font-size: 16px;
  font-weight: 600;
  color: var(--text);
}
.mcp-detail__pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 8px;
  border-radius: 999px;
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  border: 1px solid var(--border-strong);
  background: var(--bg-3);
  color: var(--text-muted);
}
.mcp-detail__pill-dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.mcp-detail__pill--running { color: var(--success); background: var(--success-bg); border-color: rgba(16,185,129,0.30); }
.mcp-detail__pill--stopped { color: var(--text-muted); }
.mcp-detail__pill--error { color: var(--danger); background: var(--danger-bg); border-color: rgba(239,68,68,0.30); }
.mcp-detail__builtin {
  padding: 1px 6px;
  border-radius: 3px;
  background: var(--bg-3);
  border: 1px solid var(--border-strong);
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-muted);
}
.mcp-detail__actions {
  display: flex;
  gap: 6px;
  flex-shrink: 0;
}
.mcp-detail__delete:hover:not(:disabled) {
  color: var(--danger);
  background: var(--danger-bg);
}

/* Tabs */
.mcp-tabs {
  display: flex;
  gap: 4px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 14px;
  padding-top: 6px;
}
.mcp-tabs button {
  padding: 8px 12px;
  border: 0;
  background: transparent;
  color: var(--text-dim);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
}
.mcp-tabs button:hover { color: var(--text); }
.mcp-tabs button.is-active {
  color: var(--text);
  border-bottom-color: var(--primary);
}
.mcp-tabs__count {
  display: inline-block;
  margin-left: 4px;
  padding: 0 6px;
  background: var(--bg-3);
  color: var(--text-muted);
  border-radius: 9px;
  font-family: var(--font-mono);
  font-size: 10px;
}

.mcp-tab { color: var(--text-dim); }

/* Config block */
.mcp-config {
  padding: 14px;
  margin-bottom: 14px;
  background: var(--bg-2);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
}
.mcp-config__label { color: var(--accent); margin-bottom: 10px; }
.mcp-config__body {
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.7;
}
.mcp-config__k { color: var(--text-muted); }
.mcp-config__v { color: var(--text); word-break: break-all; }
.mcp-config__v-warm { color: var(--accent-warm); }
.mcp-config__env-head { margin-top: 8px; color: var(--text-muted); }
.mcp-config__env-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 8px;
  margin-left: 16px;
  margin-top: 2px;
  background: var(--bg-1);
  border-radius: 4px;
}
.mcp-config__env-k { color: var(--primary-hover); font-size: 11px; }
.mcp-config__eq { color: var(--text-subtle); }
.mcp-config__env-v { color: var(--text); font-size: 11px; flex: 1; word-break: break-all; }
.mcp-config__env-v--secret { color: var(--text-muted); }
.mcp-config__none { color: var(--text-muted); font-style: italic; margin-left: 16px; }

/* Section */
.mcp-section { margin-bottom: 16px; }
.mcp-section__head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}
.mcp-section__title {
  font-size: 14px;
  font-weight: 600;
  margin: 0;
}
.mcp-section__subcount {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-muted);
  font-weight: 400;
}
.mcp-section__action {
  margin-left: auto;
  height: 26px;
  padding: 0 10px;
  font-size: 11px;
}
.mcp-section__muted {
  color: var(--text-muted);
  font-size: 12px;
}

.mcp-tool-list {
  background: var(--bg-2);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.mcp-tool {
  display: flex;
  gap: 12px;
  padding: 5px 8px;
  align-items: center;
  font-size: 12px;
}
.mcp-tool__name {
  color: var(--accent-warm);
  font-family: var(--font-mono);
  font-size: 11.5px;
  min-width: 200px;
}
.mcp-tool__desc { color: var(--text-dim); }
.mcp-tool--empty { color: var(--text-muted); font-style: italic; }

/* Attached agents */
.mcp-attached {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}
.mcp-agent-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 4px 4px 10px;
  background: var(--bg-2);
  border: 1px solid var(--border-strong);
  border-radius: 999px;
  font-size: 12px;
}
.mcp-agent-chip code {
  font-family: var(--font-mono);
  font-size: 11.5px;
  color: var(--text);
}
.mcp-agent-chip__detach {
  width: 18px;
  height: 18px;
  border: 0;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.mcp-agent-chip__detach:hover:not(:disabled) {
  background: var(--danger-bg);
  color: var(--danger);
}

.mcp-attach-wrap { display: inline-block; }
.mcp-attach-menu {
  position: fixed;
  background: var(--bg-2);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  box-shadow: var(--shadow-md);
  z-index: 1000;
  max-height: 320px;
  overflow-y: auto;
}
.mcp-attach-menu__item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding: 8px 12px;
  background: transparent;
  border: 0;
  color: var(--text-dim);
  font-size: 13px;
  cursor: pointer;
  text-align: left;
  gap: 10px;
}
.mcp-attach-menu__item:hover:not(:disabled) {
  background: var(--bg-3);
  color: var(--text);
}
.mcp-attach-menu__item:disabled { opacity: 0.5; cursor: not-allowed; }
.mcp-attach-menu__empty {
  padding: 12px;
  color: var(--text-subtle);
  font-size: 12px;
  font-style: italic;
}
.mcp-attach-menu__warn {
  font-family: var(--font-mono);
  font-size: 9.5px;
  padding: 1px 5px;
  background: rgba(245,158,11,0.10);
  color: var(--warning);
  border-radius: 3px;
  border: 1px solid rgba(245,158,11,0.30);
}

/* Events list */
.mcp-event-list { display: flex; flex-direction: column; gap: 6px; margin-top: 8px; }
.mcp-event {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding: 8px 10px;
  background: var(--bg-2);
  border: 1px solid var(--border);
  border-radius: var(--r-sm);
  font-family: var(--font-mono);
  font-size: 11px;
}
.mcp-event--empty { color: var(--text-muted); font-style: italic; }
.mcp-event__time { color: var(--text-subtle); }
.mcp-event__action { color: var(--text); font-weight: 600; }
.mcp-event__agent {
  color: var(--primary-hover);
  background: var(--primary-bg);
  padding: 1px 6px;
  border-radius: 3px;
}
.mcp-event__outcome {
  padding: 1px 8px;
  border-radius: 3px;
  font-weight: 600;
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.mcp-event__outcome--ok { background: var(--success-bg); color: var(--success); }
.mcp-event__outcome--fail { background: var(--danger-bg); color: var(--danger); }
.mcp-event__duration { color: var(--text-subtle); }
.mcp-event__detail { width: 100%; margin-top: 4px; }
.mcp-event__detail summary { cursor: pointer; color: var(--text-muted); }
.mcp-event__detail pre {
  background: var(--bg-1);
  border: 1px solid var(--border);
  border-radius: var(--r-sm);
  padding: 8px;
  font-size: 11px;
  color: var(--text-dim);
  overflow-x: auto;
  margin-top: 4px;
}

/* Modal */
.mcp-modal-overlay {
  position: fixed;
  inset: 0;
  background: var(--bg-overlay);
  backdrop-filter: blur(8px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}
.mcp-modal {
  background: var(--bg-1);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-lg);
  width: min(640px, 92vw);
  max-height: 90vh;
  display: flex;
  flex-direction: column;
  box-shadow: var(--shadow-lg);
}
.mcp-modal__head {
  padding: 18px 20px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.mcp-modal__head h3 {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
}
.mcp-modal__body {
  padding: 18px 20px;
  overflow-y: auto;
  flex: 1;
}
.mcp-modal__foot {
  padding: 14px 20px;
  border-top: 1px solid var(--border);
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}

.mcp-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 14px;
}
.mcp-field > span {
  font-family: var(--font-mono);
  font-size: 10.5px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--text-muted);
}
.mcp-input {
  padding: 8px 12px;
  background: var(--bg-2);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  color: var(--text);
  font-size: 13px;
  font-family: inherit;
}
.mcp-input:focus {
  outline: 0;
  border-color: var(--primary);
  box-shadow: 0 0 0 3px var(--primary-bg);
}
.mcp-input:disabled { opacity: 0.6; cursor: not-allowed; }
.mcp-input--mono { font-family: var(--font-mono); font-size: 12px; }

.mcp-env-table { display: flex; flex-direction: column; gap: 6px; margin-bottom: 8px; }
.mcp-env-row {
  display: grid;
  grid-template-columns: 1fr 1fr auto auto;
  gap: 6px;
  align-items: center;
}
.mcp-env-row__remove:hover { color: var(--danger); background: var(--danger-bg); }
.mcp-env-add { height: 28px; padding: 0 10px; font-size: 11.5px; }

.mcp-banner {
  margin: 12px 0;
  padding: 8px 12px;
  border-radius: var(--r-md);
  font-size: 12px;
}
.mcp-banner--success { background: var(--success-bg); color: var(--success); border: 1px solid rgba(16,185,129,0.2); }
.mcp-banner--error { background: var(--danger-bg); color: var(--danger); border: 1px solid rgba(239,68,68,0.2); }

/* Master-detail collapses to one pane at a time on the same breakpoint
   useBreakpoint.js uses (< 768px). The 900px threshold the file shipped
   with stranded users between 768–900px in a 2-column layout that
   neither pane could fit comfortably. */
@media (max-width: 767px) {
  .mcp__layout { grid-template-columns: 1fr; }
  .mcp__list { max-height: none; }
  .mcp__detail { max-height: none; }

  /* Show only one pane at a time on phone. Without this the list +
     detail stack vertically and the user has to scroll past the full
     list to read the detail. */
  .mcp__layout--mobile-detail .mcp__list { display: none; }
  .mcp__layout--mobile-list .mcp__detail { display: none; }

  /* Detail actions row was overflowing on <380px (3 buttons +
     2 gaps + delete icon > 320px). The head's flex-shrink:0 kept the
     actions group at its single-line max-content width even after
     wrapping to its own line, so it spilled past the right edge. Give
     it the full row width so its own flex-wrap can actually engage. */
  .mcp-detail__actions { flex-basis: 100%; flex-wrap: wrap; gap: 6px; }

  /* Tool rows: the 200px-min name column + center alignment left the
     name floating in a tall empty column while the description wrapped
     into a sliver beside it. Stack name over full-width description. */
  .mcp-tool { flex-direction: column; align-items: flex-start; gap: 2px; }
  .mcp-tool__name { min-width: 0; }

  /* Env row: 4 columns × 90px is unreadable on a 360px viewport.
     Stack: key/value share row 1 (key 1fr, remove auto), value
     full-width on row 2. */
  .mcp-env-row {
    grid-template-columns: 1fr auto;
    grid-template-rows: auto auto;
  }
  .mcp-env-row > :nth-child(2) {
    grid-column: 1 / -1;
  }
}

.mcp-back-btn {
  display: none;
  align-items: center;
  gap: 6px;
  margin-bottom: 12px;
  padding: 6px 12px 6px 8px;
  background: var(--bg-2);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  color: var(--text-dim);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
}
.mcp-back-btn:active { background: var(--bg-3); color: var(--text); }
@media (max-width: 767px) {
  .mcp-back-btn { display: inline-flex; }
}
</style>

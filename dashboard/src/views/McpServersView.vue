<script setup>
/**
 * MCP Servers — DB-backed catalog UI.
 *
 * Lets the user CRUD MCP server definitions (command/args/env), attach/detach
 * them to individual agents, and watch lifecycle events live via SSE. Built-in
 * servers (seeded from fastagent.config.yaml) are editable but cannot be
 * deleted — backend returns 403.
 *
 * Secret-looking env values (TOKEN/KEY/SECRET/PASSWORD/CREDENTIAL) come back
 * masked as "••••" from /api/mcp/servers; the eye icon does an extra GET to
 * /api/mcp/servers/{name}/secret/{key} to reveal a single value at a time.
 */
import { ref, computed, onMounted, onUnmounted, reactive, watch } from 'vue'
import { apiFetch, ApiError, buildSSEUrl } from '../api'
import { useAgentsStore } from '../stores/agents'
import { useConfirm } from '../composables/useConfirm'
import { useToast } from '../composables/useToast'
import { useSSEConnection } from '../composables/useSSEConnection.js'

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
const search = ref('')
const filterMode = ref('all') // all | builtin | user
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
  smokeStatus: '', // '', 'ok', 'fail'
  smokeError: '',
})

const filtered = computed(() => {
  const q = search.value.trim().toLowerCase()
  return servers.value.filter((s) => {
    if (filterMode.value === 'builtin' && !s.is_builtin) return false
    if (filterMode.value === 'user' && s.is_builtin) return false
    if (!q) return true
    return (s.name + ' ' + (s.command || '') + ' ' + (s.url || '')).toLowerCase().includes(q)
  })
})

const selected = computed(() => servers.value.find((s) => s.name === selectedName.value) || null)

// Agent metadata (name + type) for warning pills, mirrors SkillsLibraryView.
const agentChoices = computed(() =>
  [...agentsStore.agents.values()].map((a) => ({
    name: a.name,
    type: a.type,                       // 'card' | 'builtin'
    is_card_based: a.type === 'card',
  })),
)
function unattachedAgents(server) {
  if (!server) return []
  const attached = new Set(server.attached_agents || [])
  return agentChoices.value.filter((a) => !attached.has(a.name))
}

// Attach-dropdown open state. Teleported to body so it can escape the
// detail panel's overflow:auto clipping. Anchored under the trigger button
// via getBoundingClientRect at open time + scroll/resize listeners.
const attachOpen = ref(false)
const attachMenuPos = ref({ top: 0, left: 0, width: 240 })
const attachTriggerRef = ref(null)
function _measureAttachAnchor() {
  const el = attachTriggerRef.value
  if (!el) return
  const r = el.getBoundingClientRect()
  attachMenuPos.value = {
    top: r.bottom + 4,
    left: r.left,
    width: Math.max(r.width, 240),
  }
}
function toggleAttachMenu() {
  attachOpen.value = !attachOpen.value
  if (attachOpen.value) _measureAttachAnchor()
}
function closeAttachMenu(ev) {
  // Click inside the trigger or inside the teleported menu must NOT close.
  const t = ev.target
  if (t?.closest?.('.attach-wrap') || t?.closest?.('.attach-menu')) return
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
      // Refresh full detail (incl. tools) of the currently selected server.
      await loadServerDetail(selectedName.value)
    }
  } catch (err) {
    loadError.value = _friendly(err)
  } finally {
    loading.value = false
  }
}

// Fetch full detail (incl. tools[]) for a single server and merge into the
// list array. List endpoint omits tools to keep the payload small.
async function loadServerDetail(name) {
  try {
    const detail = await apiFetch(`/api/mcp/servers/${encodeURIComponent(name)}`)
    const idx = servers.value.findIndex((s) => s.name === name)
    if (idx >= 0) servers.value[idx] = { ...servers.value[idx], ...detail }
  } catch (err) {
    console.warn('[mcp] detail fetch failed for', name, err)
  }
}

// Lazy-load detail whenever user picks a different server.
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

// ── env row helpers ─────────────────────────────────────────────────

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

// ── build payload ───────────────────────────────────────────────────

function _buildPayload() {
  const args = editing.argsText
    .split('\n')
    .map((s) => s.trim())
    .filter((s) => s.length > 0)
  const env = {}
  for (const row of editing.envRows) {
    if (!row.key) continue
    if (row.masked && !row.revealed) continue // user never revealed → no change → skip
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

// ── save / test / delete ────────────────────────────────────────────

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

// ── attach / detach ─────────────────────────────────────────────────

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

// ── live events SSE ─────────────────────────────────────────────────

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

// ── lifecycle ───────────────────────────────────────────────────────

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
  <div class="mcp-view">
    <header class="header">
      <div>
        <h1>MCP Servers</h1>
        <p>DB-backed catalog. Built-in servers are seeded from fastagent.config.yaml on first boot.</p>
      </div>
      <button class="btn-primary" @click="openCreate">+ New Server</button>
    </header>

    <div class="toolbar">
      <input v-model="search" placeholder="Search by name, command, or url…" class="search-input" />
      <div class="filter-tabs">
        <button :class="{ active: filterMode === 'all' }" @click="filterMode = 'all'">All</button>
        <button :class="{ active: filterMode === 'builtin' }" @click="filterMode = 'builtin'">Built-in</button>
        <button :class="{ active: filterMode === 'user' }" @click="filterMode = 'user'">User-created</button>
      </div>
      <button class="btn-secondary" @click="loadServers" :disabled="loading">
        {{ loading ? 'Loading…' : 'Refresh' }}
      </button>
    </div>

    <div v-if="loadError" class="error">
      {{ loadError }}
      <button @click="loadServers">Retry</button>
    </div>

    <div class="layout">
      <!-- Left: server list -->
      <div class="server-list">
        <button
          v-for="s in filtered"
          :key="s.name"
          type="button"
          class="server-row"
          :class="{ active: selectedName === s.name }"
          @click="selectedName = s.name"
        >
          <div class="server-row__head">
            <span class="server-row__name">{{ s.name }}</span>
            <span :class="['status-dot', `status-dot--${s.status}`]" :title="`status: ${s.status}`"></span>
          </div>
          <div class="server-row__meta">
            <span class="meta-tag">{{ s.transport }}</span>
            <span v-if="s.is_builtin" class="badge badge-builtin">built-in</span>
            <span class="meta-sub">· {{ s.attached_agents.length }} agent{{ s.attached_agents.length === 1 ? '' : 's' }}</span>
          </div>
        </button>
        <div v-if="!filtered.length && !loading" class="empty">No servers match.</div>
      </div>

      <!-- Right: detail panel -->
      <section v-if="selected" class="detail">
        <header class="detail__head">
          <div>
            <h2>{{ selected.name }}</h2>
            <div class="detail__sub">
              <span class="meta-tag">{{ selected.transport }}</span>
              <span v-if="selected.is_builtin" class="badge badge-builtin">built-in</span>
              <span :class="['status-pill', `status-pill--${selected.status}`]">{{ selected.status }}</span>
            </div>
          </div>
          <div class="detail__actions">
            <button class="btn-secondary" :disabled="refreshingTools" @click="refreshTools(selected)">
              {{ refreshingTools ? 'Testing…' : 'Test &amp; refresh' }}
            </button>
            <button class="btn-secondary" @click="openEdit(selected)">Edit</button>
            <button
              class="btn-danger"
              @click="deleteServer(selected)"
              :disabled="selected.is_builtin"
              :title="selected.is_builtin ? 'Built-in servers cannot be deleted' : 'Delete'"
            >Delete</button>
          </div>
        </header>

        <nav class="tabs">
          <button :class="{ active: tab === 'config' }" @click="tab = 'config'">Config</button>
          <button :class="{ active: tab === 'agents' }" @click="tab = 'agents'">
            Agents <span class="tab-count">{{ selected.attached_agents.length }}</span>
          </button>
          <button :class="{ active: tab === 'events' }" @click="tab = 'events'">Events</button>
        </nav>

        <div v-if="tab === 'config'" class="tab-pane">
          <dl class="config-grid">
            <dt>Transport</dt><dd>{{ selected.transport }}</dd>
            <dt>Command</dt><dd><code v-if="selected.command">{{ selected.command }}</code><span v-else class="muted">—</span></dd>
            <dt>Args</dt><dd><code v-if="(selected.args || []).length">{{ (selected.args || []).join(' ') }}</code><span v-else class="muted">—</span></dd>
            <dt>URL</dt><dd><code v-if="selected.url">{{ selected.url }}</code><span v-else class="muted">—</span></dd>
            <dt>Built-in</dt><dd>{{ selected.is_builtin ? 'yes' : 'no' }}</dd>
          </dl>
          <h4 class="section-title">Env</h4>
          <ul class="env-list">
            <li v-for="(v, k) in selected.env" :key="k">
              <code class="env-key">{{ k }}</code>
              <code class="env-val">{{ v }}</code>
            </li>
            <li v-if="!Object.keys(selected.env || {}).length" class="muted">(none)</li>
          </ul>
          <div class="section-row">
            <h4 class="section-title">Tools <span class="muted">({{ (selected.tools || []).length }})</span></h4>
            <button
              class="btn-secondary btn-secondary--small"
              :disabled="refreshingTools"
              :title="`Re-run smoke test and refresh tool descriptions cache for ${selected.name}`"
              @click="refreshTools(selected)"
            >
              {{ refreshingTools ? 'Refreshing…' : '↻ Refresh tools' }}
            </button>
          </div>
          <ul class="tool-list">
            <li v-for="t in selected.tools || []" :key="t.name || t">
              <code>{{ t.name || t }}</code>
              <span v-if="t.description" class="muted"> — {{ t.description }}</span>
            </li>
            <li v-if="!(selected.tools || []).length" class="muted">(none discovered — click Refresh tools to fetch from a live connection)</li>
          </ul>
        </div>

        <div v-if="tab === 'agents'" class="tab-pane">
          <h4 class="section-title">Currently attached</h4>
          <div v-if="selected.attached_agents.length" class="attached-row">
            <button
              v-for="a in selected.attached_agents"
              :key="a"
              class="link link-detach"
              type="button"
              :title="`Detach from ${a} — interrupts in-flight tool calls`"
              :disabled="attaching"
              @click="detachFromAgent(selected, a)"
            >× {{ a }}</button>
          </div>
          <p v-else class="muted small">Not attached to any agent.</p>

          <h4 class="section-title">Add to agent</h4>
          <div class="attach-wrap">
            <button ref="attachTriggerRef" class="btn-secondary" @click.stop="toggleAttachMenu">
              Attach to…
            </button>
          </div>
        </div>

        <!-- Teleported dropdown — escapes detail panel's overflow:auto so
             long agent lists can scroll inside the menu instead of being
             clipped by the parent. -->
        <Teleport to="body">
          <div
            v-if="attachOpen && tab === 'agents'"
            class="attach-menu"
            :style="{ top: attachMenuPos.top + 'px', left: attachMenuPos.left + 'px', minWidth: attachMenuPos.width + 'px' }"
            @click.stop
          >
            <div v-if="!unattachedAgents(selected).length" class="attach-empty">
              Already attached to all agents.
            </div>
            <button
              v-for="a in unattachedAgents(selected)"
              :key="a.name"
              class="attach-item"
              :disabled="attaching"
              @click="attachToAgent(selected, a)"
            >
              <span class="attach-agent-name">{{ a.name }}</span>
              <span v-if="!a.is_card_based" class="attach-warn-pill" title="Code-based agent — change reverts on restart">
                runtime only
              </span>
            </button>
          </div>
        </Teleport>

        <div v-if="tab === 'events'" class="tab-pane">
          <p class="muted small">Realtime + history (newest first). Filtered to this server.</p>
          <ul class="event-list">
            <li
              v-for="ev in events.filter((e) => e.server === selected.name)"
              :key="ev.id"
              class="event-row"
            >
              <span class="event-row__time">{{ fmtTime(ev.timestamp) }}</span>
              <span class="event-row__action">{{ ev.action }}</span>
              <span v-if="ev.agent" class="event-row__agent">{{ ev.agent }}</span>
              <span :class="['event-row__outcome', `outcome--${ev.outcome}`]">{{ ev.outcome }}</span>
              <span v-if="ev.duration_ms != null" class="event-row__duration">{{ ev.duration_ms }}ms</span>
              <details v-if="ev.detail && Object.keys(ev.detail).length" class="event-row__detail">
                <summary>detail</summary>
                <pre>{{ JSON.stringify(ev.detail, null, 2) }}</pre>
              </details>
            </li>
            <li v-if="!events.filter((e) => e.server === selected.name).length" class="muted">(no events yet)</li>
          </ul>
        </div>
      </section>

      <section v-else class="detail detail--empty">
        <p class="muted">Select a server on the left, or create a new one.</p>
      </section>
    </div>

    <!-- Editor modal -->
    <Teleport to="body">
      <div v-if="editing.open" class="modal-overlay" @click.self="closeEditor">
        <div class="modal">
          <header class="modal__head">
            <h3>{{ editing.isCreate ? 'New MCP Server' : `Edit ${editing.name}` }}</h3>
            <button class="modal__close" @click="closeEditor" aria-label="Close">×</button>
          </header>
          <div class="modal__body">
            <label class="field">
              <span>Name</span>
              <input v-model="editing.name" :disabled="!editing.isCreate" placeholder="my-tool" class="text-input" />
            </label>
            <label class="field">
              <span>Transport</span>
              <select v-model="editing.transport" class="text-input">
                <option value="stdio">stdio</option>
                <option value="http">http</option>
                <option value="sse">sse</option>
              </select>
            </label>
            <template v-if="editing.transport === 'stdio'">
              <label class="field">
                <span>Command</span>
                <input v-model="editing.command" placeholder="python" class="text-input" />
              </label>
              <label class="field">
                <span>Args (one per line)</span>
                <textarea v-model="editing.argsText" rows="4" class="text-input mono" placeholder="-m&#10;my_module" />
              </label>
            </template>
            <template v-else>
              <label class="field">
                <span>URL</span>
                <input v-model="editing.url" placeholder="https://example.com/mcp" class="text-input" />
              </label>
            </template>
            <div class="field">
              <span>Env vars</span>
              <div class="env-table" v-if="editing.envRows.length">
                <div class="env-table__row" v-for="(row, idx) in editing.envRows" :key="idx">
                  <input v-model="row.key" placeholder="KEY" class="text-input mono" />
                  <input
                    v-model="row.value"
                    :type="row.masked && !row.revealed ? 'password' : 'text'"
                    placeholder="value"
                    class="text-input mono"
                    :readonly="row.masked && !row.revealed"
                  />
                  <button v-if="row.masked && !row.revealed" type="button" class="icon-btn" @click="revealSecret(idx)" title="Reveal">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
                  </button>
                  <button v-else-if="row.masked && row.revealed" type="button" class="icon-btn" @click="hideSecret(idx)" title="Hide">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>
                  </button>
                  <button type="button" class="icon-btn icon-btn-danger" @click="removeEnvRow(idx)" title="Remove">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
                  </button>
                </div>
              </div>
              <button type="button" class="btn-secondary btn-secondary--small" @click="addEnvRow">+ Add env var</button>
            </div>

            <div v-if="editing.smokeStatus === 'ok'" class="banner banner--success">{{ editing.smokeError }}</div>
            <div v-if="editing.smokeStatus === 'fail'" class="banner banner--error">Smoke test failed: {{ editing.smokeError }}</div>
            <div v-if="editing.error" class="banner banner--error">{{ editing.error }}</div>
          </div>
          <footer class="modal__foot">
            <button class="btn-secondary" @click="testCurrent" :disabled="editing.saving">Test connection</button>
            <button class="btn-primary" @click="saveServer" :disabled="editing.saving">
              {{ editing.saving ? 'Saving…' : 'Save' }}
            </button>
          </footer>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
/* Layout — same envelope as SkillsLibraryView */
.mcp-view { max-width: 1200px; margin: 0 auto; padding: 24px; }
.header {
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: 16px; margin-bottom: 20px;
}
.header h1 { margin: 0 0 6px; font-size: 22px; color: #f0f2f5; }
.header p { margin: 0; font-size: 13px; color: #8b8fa3; max-width: 700px; }

.btn-primary {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 8px 16px; background: #3b82f6; border: 1px solid #3b82f6;
  color: white; border-radius: 8px; font-size: 13px; font-weight: 600;
  cursor: pointer; white-space: nowrap;
}
.btn-primary:hover:not(:disabled) { background: #2563eb; border-color: #2563eb; }
.btn-primary:disabled { opacity: 0.55; cursor: not-allowed; }

.btn-secondary {
  padding: 7px 14px; background: #111318; border: 1px solid #1a1d2e;
  color: #c4c8d4; border-radius: 8px; font-size: 12px;
  cursor: pointer; font-weight: 500;
}
.btn-secondary:hover:not(:disabled) { background: #1e2233; color: #f0f2f5; border-color: #2a3556; }
.btn-secondary:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-secondary--small { padding: 5px 10px; font-size: 11px; }

.btn-danger {
  padding: 7px 14px; background: rgba(239, 68, 68, 0.06);
  border: 1px solid rgba(239, 68, 68, 0.2);
  color: #f87171; border-radius: 8px; font-size: 12px;
  cursor: pointer; font-weight: 500;
}
.btn-danger:hover:not(:disabled) { background: rgba(239, 68, 68, 0.15); border-color: rgba(239, 68, 68, 0.35); }
.btn-danger:disabled { opacity: 0.4; cursor: not-allowed; }

.toolbar {
  display: flex; align-items: center; gap: 12px;
  margin-bottom: 16px; flex-wrap: wrap;
}
.search-input {
  flex: 1; min-width: 220px; padding: 8px 14px;
  background: #0c0e15; border: 1px solid #1a1d2e;
  border-radius: 8px; color: #f0f2f5; font-size: 13px;
}
.search-input:focus { outline: none; border-color: #3b82f6; box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.15); }

.filter-tabs {
  display: flex; background: #0c0e15;
  border: 1px solid #1a1d2e; border-radius: 8px; padding: 2px;
}
.filter-tabs button {
  padding: 6px 14px; background: transparent; border: none;
  color: #8b8fa3; font-size: 12px; font-weight: 500;
  border-radius: 6px; cursor: pointer;
}
.filter-tabs button:hover:not(.active) { color: #c4c8d4; }
.filter-tabs button.active { background: #1e2233; color: #f0f2f5; }

.error {
  margin: 0 0 12px; padding: 8px 12px;
  background: rgba(239, 68, 68, 0.08); border: 1px solid rgba(239, 68, 68, 0.2);
  border-radius: 8px; color: #f87171; font-size: 13px;
}
.error button {
  margin-left: 8px; background: transparent; border: 1px solid rgba(239, 68, 68, 0.3);
  color: #f87171; padding: 2px 10px; border-radius: 4px; cursor: pointer;
}

.empty {
  padding: 40px 20px; text-align: center;
  color: #8b8fa3; font-size: 13px;
  background: #0c0e15; border: 1px dashed #1a1d2e; border-radius: 12px;
}

/* Two-pane layout */
.layout {
  display: grid; grid-template-columns: 320px 1fr; gap: 16px;
  align-items: start;
}

/* Left list */
.server-list { display: flex; flex-direction: column; gap: 8px; }
.server-row {
  display: flex; flex-direction: column; gap: 6px;
  padding: 12px 14px; background: #0c0e15;
  border: 1px solid #1a1d2e; border-radius: 12px;
  text-align: left; cursor: pointer; transition: border-color 0.15s, background 0.15s;
}
.server-row:hover { border-color: #2a3556; }
.server-row.active { background: #111318; border-color: #3b82f6; }
.server-row__head { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.server-row__name { color: #f0f2f5; font-weight: 600; font-size: 13px; }
.server-row__meta { display: flex; align-items: center; gap: 8px; font-size: 11px; color: #555872; flex-wrap: wrap; }
.meta-tag {
  padding: 1px 6px; background: #111318; border: 1px solid #1a1d2e;
  border-radius: 4px; color: #8b8fa3; font-size: 10px; font-weight: 500;
  text-transform: uppercase; letter-spacing: 0.04em;
}
.meta-sub { color: #555872; }

/* Status indicator (dot in list, pill in detail) */
.status-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.status-dot--running { background: #34d399; box-shadow: 0 0 0 3px rgba(16, 185, 129, 0.15); }
.status-dot--stopped { background: #555872; }
.status-dot--error { background: #f87171; box-shadow: 0 0 0 3px rgba(239, 68, 68, 0.15); }
.status-dot--unknown { background: #2a3556; }

.status-pill {
  font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em;
  padding: 2px 8px; border-radius: 4px;
}
.status-pill--running { background: rgba(16, 185, 129, 0.12); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.25); }
.status-pill--stopped { background: rgba(85, 88, 114, 0.12); color: #8b8fa3; border: 1px solid rgba(85, 88, 114, 0.25); }
.status-pill--error { background: rgba(239, 68, 68, 0.12); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.25); }
.status-pill--unknown { background: rgba(85, 88, 114, 0.08); color: #555872; border: 1px solid #1a1d2e; }

.badge {
  font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em;
  padding: 2px 7px; border-radius: 4px;
}
.badge-builtin {
  background: rgba(59, 130, 246, 0.15); color: #60a5fa;
  border: 1px solid rgba(59, 130, 246, 0.25);
}

/* Detail panel */
.detail {
  background: #0c0e15; border: 1px solid #1a1d2e; border-radius: 12px;
  padding: 20px; min-height: 320px;
  position: sticky; top: 16px; max-height: calc(100vh - 32px); overflow-y: auto;
}
.detail--empty { display: flex; align-items: center; justify-content: center; }
.detail__head {
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: 16px; margin-bottom: 16px;
}
.detail__head h2 { margin: 0 0 6px; font-size: 18px; color: #f0f2f5; font-weight: 600; }
.detail__sub { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.detail__actions { display: flex; gap: 8px; flex-shrink: 0; }

.tabs { display: flex; gap: 4px; border-bottom: 1px solid #1a1d2e; margin-bottom: 16px; }
.tabs button {
  padding: 8px 14px; border: none; background: transparent;
  color: #8b8fa3; font-size: 13px; font-weight: 500;
  cursor: pointer; border-bottom: 2px solid transparent;
  margin-bottom: -1px;
}
.tabs button:hover:not(.active) { color: #c4c8d4; }
.tabs button.active { color: #f0f2f5; border-bottom-color: #3b82f6; }
.tab-count {
  display: inline-block; margin-left: 4px; padding: 0 6px;
  background: #1e2233; color: #c4c8d4; border-radius: 9px; font-size: 11px;
}

.tab-pane { color: #c4c8d4; }
.section-title { margin: 16px 0 8px; font-size: 12px; font-weight: 600; color: #8b8fa3; text-transform: uppercase; letter-spacing: 0.05em; }
.section-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.section-row .section-title { margin: 16px 0 8px; }

.config-grid {
  display: grid; grid-template-columns: 110px 1fr;
  gap: 6px 16px; margin: 0; font-size: 13px;
}
.config-grid dt { color: #8b8fa3; }
.config-grid dd { margin: 0; color: #c4c8d4; }
.config-grid code { background: #0a0d14; border: 1px solid #1a1d2e; padding: 2px 6px; border-radius: 4px; font-size: 12px; color: #c4c8d4; }

.env-list, .tool-list, .agent-list, .event-list { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: 6px; }
.env-list li { display: flex; gap: 8px; align-items: center; font-size: 13px; }
.env-key { color: #60a5fa; background: rgba(59, 130, 246, 0.06); padding: 2px 6px; border-radius: 4px; font-size: 12px; }
.env-val { color: #c4c8d4; background: #0a0d14; border: 1px solid #1a1d2e; padding: 2px 6px; border-radius: 4px; font-size: 12px; }
.tool-list li { font-size: 13px; color: #c4c8d4; }
.tool-list code { background: #0a0d14; border: 1px solid #1a1d2e; padding: 2px 6px; border-radius: 4px; font-size: 12px; color: #c4c8d4; }

.attached-row { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 4px; }

.link {
  background: none; border: none; color: #60a5fa;
  font-size: 13px; cursor: pointer; padding: 0;
}
.link:hover { color: #93c5fd; }
.link-detach {
  font-size: 11px; padding: 3px 9px; border-radius: 4px;
  background: rgba(239, 68, 68, 0.06); color: #fca5a5;
  border: 1px solid rgba(239, 68, 68, 0.2);
}
.link-detach:hover:not(:disabled) { background: rgba(239, 68, 68, 0.15); color: #f87171; }
.link-detach:disabled { opacity: 0.5; cursor: not-allowed; }

.attach-wrap { display: inline-block; }
/* Teleported to <body> so use position: fixed; coords come from
   getBoundingClientRect() at open time. */
.attach-menu {
  position: fixed;
  background: #0c0e15;
  border: 1px solid #1a1d2e; border-radius: 10px;
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.6);
  z-index: 1000; max-height: 320px; overflow-y: auto;
}
.attach-item {
  display: flex; align-items: center; justify-content: space-between;
  width: 100%; padding: 8px 12px;
  background: transparent; border: none; color: #c4c8d4;
  font-size: 13px; cursor: pointer; text-align: left; gap: 10px;
}
.attach-item:hover:not(:disabled) { background: #1e2233; color: #f0f2f5; }
.attach-item:disabled { opacity: 0.5; cursor: not-allowed; }
.attach-empty { padding: 12px; color: #555872; font-size: 12px; font-style: italic; }
.attach-agent-name { flex: 1; min-width: 0; }
.attach-warn-pill {
  font-size: 10px; padding: 2px 6px;
  background: rgba(245, 158, 11, 0.1); color: #fbbf24;
  border-radius: 4px; border: 1px solid rgba(245, 158, 11, 0.2);
  white-space: nowrap;
}

.event-row {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  padding: 8px 10px; background: #0a0d14; border: 1px solid #1a1d2e;
  border-radius: 8px; font-size: 12px;
}
.event-row__time { color: #555872; font-family: monospace; font-size: 11px; }
.event-row__action { font-weight: 600; color: #f0f2f5; }
.event-row__agent { color: #60a5fa; padding: 1px 6px; background: rgba(59, 130, 246, 0.1); border-radius: 4px; font-size: 11px; }
.event-row__outcome { padding: 1px 8px; border-radius: 4px; font-weight: 600; font-size: 10px; text-transform: uppercase; letter-spacing: 0.05em; }
.outcome--ok { background: rgba(16, 185, 129, 0.12); color: #34d399; }
.outcome--fail { background: rgba(239, 68, 68, 0.12); color: #f87171; }
.event-row__duration { color: #555872; font-size: 11px; }
.event-row__detail { width: 100%; margin-top: 4px; }
.event-row__detail summary { cursor: pointer; color: #8b8fa3; font-size: 11px; }
.event-row__detail pre { background: #0c0e15; border: 1px solid #1a1d2e; border-radius: 6px; padding: 8px; font-size: 11px; color: #c4c8d4; overflow-x: auto; margin-top: 4px; }

.muted { color: #555872; }
.small { font-size: 12px; }

/* Modal — borrow Skills modal styling */
.modal-overlay {
  position: fixed; inset: 0;
  background: rgba(0, 0, 0, 0.6);
  backdrop-filter: blur(6px); -webkit-backdrop-filter: blur(6px);
  display: flex; align-items: center; justify-content: center;
  z-index: 1000;
}
.modal {
  background: #0c0e15; border: 1px solid #1a1d2e; border-radius: 14px;
  width: min(640px, 92vw); max-height: 90vh;
  display: flex; flex-direction: column;
  box-shadow: 0 24px 60px rgba(0, 0, 0, 0.5);
}
.modal__head {
  padding: 18px 20px; border-bottom: 1px solid #1a1d2e;
  display: flex; align-items: center; justify-content: space-between;
}
.modal__head h3 { margin: 0; font-size: 16px; color: #f0f2f5; font-weight: 600; }
.modal__close {
  width: 28px; height: 28px; border: none; background: transparent;
  color: #8b8fa3; font-size: 20px; cursor: pointer; border-radius: 6px;
  display: flex; align-items: center; justify-content: center;
}
.modal__close:hover { background: #1e2233; color: #f0f2f5; }
.modal__body { padding: 18px 20px; overflow-y: auto; flex: 1; }
.modal__foot {
  padding: 14px 20px; border-top: 1px solid #1a1d2e;
  display: flex; justify-content: flex-end; gap: 10px;
}

.field { display: flex; flex-direction: column; gap: 6px; margin-bottom: 14px; }
.field > span { font-size: 12px; color: #8b8fa3; font-weight: 500; }
.text-input {
  padding: 8px 12px; background: #111318; border: 1px solid #1a1d2e;
  border-radius: 8px; color: #f0f2f5; font-size: 13px;
  font-family: inherit;
}
.text-input:focus { outline: none; border-color: #3b82f6; box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.15); }
.text-input:disabled { opacity: 0.6; cursor: not-allowed; }
.text-input.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, monospace; font-size: 12px; }

.env-table { display: flex; flex-direction: column; gap: 6px; margin-bottom: 8px; }
.env-table__row { display: grid; grid-template-columns: 1fr 1fr auto auto; gap: 6px; align-items: center; }

.icon-btn {
  width: 30px; height: 30px;
  display: flex; align-items: center; justify-content: center;
  background: transparent; border: 1px solid transparent;
  color: #8b8fa3; border-radius: 7px; cursor: pointer;
}
.icon-btn:hover:not(:disabled) { background: rgba(255, 255, 255, 0.04); color: #f0f2f5; border-color: rgba(255, 255, 255, 0.06); }
.icon-btn-danger:hover:not(:disabled) { background: rgba(239, 68, 68, 0.1); color: #f87171; border-color: rgba(239, 68, 68, 0.25); }

.banner {
  margin: 12px 0; padding: 8px 12px; border-radius: 8px;
  font-size: 12px;
}
.banner--success { background: rgba(16, 185, 129, 0.08); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.2); }
.banner--error { background: rgba(239, 68, 68, 0.08); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.2); }

@media (max-width: 900px) {
  .layout { grid-template-columns: 1fr; }
}
</style>

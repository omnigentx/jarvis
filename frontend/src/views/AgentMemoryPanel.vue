<script setup>
/**
 * Agent Memory tab — per-agent durable memory dashboard.
 *
 * Read surface over the memory REST API (routes/memory.py): active memories
 * (filter + manual search), pending candidates (approve/reject), recent
 * retrieval runs, and index status. Every call is scoped to this agent's name,
 * so it can only ever show this agent's memory. Copy via i18n (useLang().t).
 *
 * Layout mirrors the other Agent Detail tabs: every section is a `.panel`
 * card (var(--bg-1) + border + radius 12), matching AgentDetail.vue's `.panel`
 * so this tab doesn't look bolted-on. Inputs/buttons use the design tokens
 * (--bg-4, --primary, --border-strong).
 */
import { computed, onMounted, ref, watch } from 'vue'
import { apiFetch } from '../api'
import { useLang } from '../composables/useLang.js'
import { useToast } from '../composables/useToast'
import { useMemoryStore } from '../stores/memory'
import MemoryGraph from '../components/memory/MemoryGraph.vue'

const props = defineProps({ agentName: { type: String, required: true } })
const { t } = useLang()
const toast = useToast()
const memStore = useMemoryStore()

const loading = ref(false)
const error = ref('')
const memories = ref([])
const total = ref(0)
const candidates = ref([])
const runs = ref([])
const indexStatus = ref(null)

const typeFilter = ref('')
const statusFilter = ref('active')
const searchQuery = ref('')
const searchResult = ref(null)

// How many search results each retrieval lane surfaced (fts/dense/graph) — lets
// you SEE the GraphRAG (MENTIONS) lane's contribution at a glance.
const laneCounts = computed(() => {
  const c = { fts: 0, dense: 0, graph: 0 }
  for (const e of searchResult.value?.evidence || []) {
    for (const ln of e.lanes || []) if (ln in c) c[ln]++
  }
  return c
})

const TYPES = ['', 'pinned', 'episodic', 'semantic', 'procedural']
const base = computed(() => `/api/agents/${encodeURIComponent(props.agentName)}`)

async function loadAll() {
  loading.value = true
  error.value = ''
  try {
    const q = new URLSearchParams({ status: statusFilter.value })
    if (typeFilter.value) q.set('memory_type', typeFilter.value)
    const [mem, cand, rr, idx] = await Promise.all([
      apiFetch(`${base.value}/memories?${q}`),
      apiFetch(`${base.value}/memory-candidates?status=pending`),
      apiFetch(`${base.value}/retrieval-runs?limit=20`),
      apiFetch('/api/memory/index-status'),
    ])
    memories.value = mem.items
    total.value = mem.total
    candidates.value = cand.items
    runs.value = rr.items
    indexStatus.value = idx
  } catch (err) {
    error.value = err?.message || String(err)
  } finally {
    loading.value = false
  }
}

async function runSearch() {
  if (!searchQuery.value.trim()) { searchResult.value = null; return }
  try {
    searchResult.value = await apiFetch(`${base.value}/memory-search`, {
      method: 'POST', body: JSON.stringify({ query: searchQuery.value, limit: 8 }),
    })
  } catch (err) { error.value = err?.message || String(err) }
}

// Per-row actions mirror bulk(): track an in-flight set so the button can
// disable (visible <200ms feedback) and surface errors instead of swallowing
// them — a failed approve/delete used to do nothing on screen.
const rowBusy = ref(new Set())
const isRowBusy = (id) => rowBusy.value.has(id)

async function rowAction(id, fn) {
  if (rowBusy.value.has(id)) return
  rowBusy.value = new Set(rowBusy.value).add(id) // reassign so Vue tracks it
  try {
    await fn()
    await loadAll()
  } catch (err) {
    error.value = err?.message || String(err)
    toast.error(t('memory.bulkError'), { description: err?.message || String(err) })
  } finally {
    const next = new Set(rowBusy.value)
    next.delete(id)
    rowBusy.value = next
  }
}

const approve = (id) =>
  rowAction(id, () => apiFetch(`${base.value}/memory-candidates/${id}/approve`, { method: 'POST' }))
const reject = (id) =>
  rowAction(id, () => apiFetch(`${base.value}/memory-candidates/${id}/reject`, { method: 'POST' }))
const archive = (id) =>
  rowAction(id, () => apiFetch(`${base.value}/memories/${id}/archive`, { method: 'POST' }))
const restore = (id) =>
  rowAction(id, () => apiFetch(`${base.value}/memories/${id}/restore`, { method: 'POST' }))
function remove(id) {
  if (!confirm(t('memory.confirmDelete'))) return
  return rowAction(id, () => apiFetch(`${base.value}/memories/${id}`, { method: 'DELETE' }))
}

// Fail-loud recovery: when the dense index is DEAD (index-status.dense.broken —
// rows present but not searchable, e.g. after an ungraceful kill), one click
// rebuilds the HNSW index + re-projects from SQLite. Reloads so the red banner
// clears the moment recall is live again.
const repairing = ref(false)
async function repairIndex() {
  if (repairing.value) return
  repairing.value = true
  try {
    const res = await apiFetch('/api/memory/repair', { method: 'POST' })
    if (res.index_rebuilt) toast.success(t('memory.repairOk'))
    else toast.error(t('memory.repairFailed'), { description: res.error || '' })
    await loadAll()
  } catch (err) {
    error.value = err?.message || String(err)
    toast.error(t('memory.repairFailed'), { description: err?.message || String(err) })
  } finally {
    repairing.value = false
  }
}

// Per-memory version history — lets you audit a conflict-replaced (superseded)
// memory and roll it back to any prior version. Lazy-loaded + cached, so opening
// a row is a single fetch; rolling back drops the cache so the reopened list is
// fresh.
const openVersions = ref(new Set())
const versionsById = ref({})
async function toggleVersions(id) {
  const next = new Set(openVersions.value)
  if (next.has(id)) { next.delete(id); openVersions.value = next; return }
  next.add(id); openVersions.value = next
  if (!versionsById.value[id]) {
    try {
      const res = await apiFetch(`${base.value}/memories/${id}/versions`)
      versionsById.value = { ...versionsById.value, [id]: res.items || [] }
    } catch (err) { error.value = err?.message || String(err) }
  }
}
const rollback = (id, version) =>
  rowAction(id, async () => {
    await apiFetch(`${base.value}/memories/${id}/rollback`, {
      method: 'POST', body: JSON.stringify({ to_version: version }),
    })
    const next = { ...versionsById.value }; delete next[id]; versionsById.value = next
  })

// Bulk approval — with 20+ pending candidates, one-by-one is painful. Select a
// subset (or all) and resolve in a single round-trip via the bulk endpoint.
const selected = ref(new Set())
const bulkBusy = ref(false)
const candidateIds = computed(() => candidates.value.map((c) => c.id))
const selectedCount = computed(() => selected.value.size)
const allSelected = computed(
  () => candidates.value.length > 0 && candidateIds.value.every((id) => selected.value.has(id)),
)

function toggleSelect(id) {
  const next = new Set(selected.value) // reassign so Vue tracks the change
  next.has(id) ? next.delete(id) : next.add(id)
  selected.value = next
}
function toggleSelectAll() {
  selected.value = allSelected.value ? new Set() : new Set(candidateIds.value)
}
async function bulk(action, ids) {
  if (!ids.length || bulkBusy.value) return
  bulkBusy.value = true
  try {
    const res = await apiFetch(`${base.value}/memory-candidates/bulk-${action}`, {
      method: 'POST', body: JSON.stringify({ ids }),
    })
    selected.value = new Set()
    await loadAll()
    const done = (action === 'approve' ? res.approved : res.rejected)?.length ?? ids.length
    const failed = res.failed?.length || 0
    const key = action === 'approve' ? 'memory.bulkApproved' : 'memory.bulkRejected'
    toast.success(t(key, { n: done }), failed ? { description: t('memory.bulkFailed', { n: failed }) } : undefined)
  } catch (err) {
    error.value = err?.message || String(err)
    toast.error(t('memory.bulkError'), { description: err?.message || String(err) })
  } finally {
    bulkBusy.value = false
  }
}
const approveAll = () => bulk('approve', candidateIds.value)
const approveSelected = () => bulk('approve', [...selected.value])
const rejectSelected = () => bulk('reject', [...selected.value])

function payloadContent(c) {
  try { return JSON.parse(c.payload).content || '' } catch { return c.payload }
}

// Localized explanation of why a candidate needs review despite auto-save being
// on. The backend sends a stable reason code; unknown codes fall back to the key.
function reasonNote(c) {
  return c.reason ? t('memory.approvalReason.' + c.reason) : ''
}

// ---- Recent searches (retrieval telemetry) ----------------------------------
// Each row is one recall. The status pill reflects the PER-QUERY signal (fast vs
// agentic level, degraded, cached) — not the static `mode` config, which is shown
// once in the header. Rows expand to reveal the per-lane latency + recalled ids.
const expandedRuns = ref(new Set())
function toggleRun(id) {
  const next = new Set(expandedRuns.value) // reassign so Vue tracks it
  next.has(id) ? next.delete(id) : next.add(id)
  expandedRuns.value = next
}

// Map a run to its status pill {label, cls}. Precedence: degraded > deep > fast —
// a degraded turn is the one worth surfacing even if it escalated.
function runStatus(r) {
  if (r.degraded) return { label: t('memory.runs.degraded'), cls: 'st-degraded' }
  if (r.level >= 2) return { label: t('memory.runs.levelDeep'), cls: 'st-deep' }
  return { label: t('memory.runs.levelFast'), cls: 'st-fast' }
}

// "3 recalled" / "no match" (0 results is the legible form of the old "0 tok").
const runRecall = (r) =>
  r.result_count > 0 ? t('memory.runs.recalled', { n: r.result_count }) : t('memory.runs.noMatch')

// Lane breakdown string; a lane that didn't run (None → null) renders as "—".
const off = () => t('memory.runs.off')
function runLanes(r) {
  const ms = (v) => (v == null ? off() : `${v}ms`)
  return t('memory.runs.lanes', { bm25: ms(r.bm25_ms), dense: ms(r.dense_ms), rerank: ms(r.rerank_ms) })
}

// Relative time from an epoch-seconds timestamp. Coarse buckets are enough here.
function timeAgo(ts) {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - (ts || 0)))
  if (s < 60) return t('memory.runs.now')
  if (s < 3600) return t('memory.runs.minutesAgo', { n: Math.floor(s / 60) })
  if (s < 86400) return t('memory.runs.hoursAgo', { n: Math.floor(s / 3600) })
  return t('memory.runs.daysAgo', { n: Math.floor(s / 86400) })
}

watch([typeFilter, statusFilter], loadAll)
watch(() => memStore.indexTick, loadAll)
onMounted(loadAll)
</script>

<template>
  <div class="mem-panel">
    <p v-if="error" class="error">{{ error }}</p>

    <!-- FAIL LOUD: dense index is dead (rows present but not searchable, e.g. after
         an ungraceful kill). Never hide behind a green status — shout + offer a
         one-click rebuild instead of silently degrading to keyword-only recall. -->
    <div v-if="indexStatus?.dense?.broken" class="index-broken" role="alert">
      <span class="ib-icon" aria-hidden="true">⚠</span>
      <span class="ib-text">{{ t('memory.denseBroken') }}</span>
      <button class="btn restore" :disabled="repairing" @click="repairIndex">
        <span v-if="repairing" class="spin" aria-hidden="true"></span>
        {{ repairing ? t('memory.repairing') : t('memory.restoreIndex') }}
      </button>
    </div>

    <!-- Index status strip -->
    <div class="status-bar">
      <span v-if="memStore.isDegraded(agentName)" class="degraded">⚠ {{ t('memory.degraded') }}</span>
      <template v-if="indexStatus">
        <span class="stat">{{ t('memory.episodic') }}: {{ indexStatus.episodic_documents }}</span>
        <span class="stat" v-if="indexStatus.outbox?.pending">{{ t('memory.indexPending') }}: {{ indexStatus.outbox.pending }}</span>
        <span class="stat danger" v-if="indexStatus.outbox?.dead">{{ t('memory.indexDead') }}: {{ indexStatus.outbox.dead }}</span>
      </template>
    </div>

    <!-- Memory graph (LadybugDB GraphRAG view) -->
    <section class="panel">
      <div class="panel-header">
        <h3>{{ t('memory.graph') }}</h3>
      </div>
      <MemoryGraph :agent-name="agentName" />
    </section>

    <!-- Search -->
    <section class="panel">
      <div class="search-row">
        <input v-model="searchQuery" class="search-input" type="text"
               :placeholder="t('memory.searchPlaceholder')" @keyup.enter="runSearch" />
        <button class="btn primary" @click="runSearch">{{ t('common.search') }}</button>
      </div>
      <div v-if="searchResult" class="search-result">
        <div class="muted" v-if="searchResult.degraded">{{ t('memory.degradedShort') }}</div>
        <!-- Lane provenance summary: how each retrieval lane contributed -->
        <div v-if="searchResult.evidence.length" class="lane-summary muted">
          {{ searchResult.evidence.length }} {{ t('memory.results') }} —
          <span class="lane lane-fts">fts {{ laneCounts.fts }}</span>
          <span class="lane lane-dense">dense {{ laneCounts.dense }}</span>
          <span class="lane lane-graph">graph {{ laneCounts.graph }}</span>
        </div>
        <div v-for="e in searchResult.evidence" :key="e.evidence_id" class="evidence">
          <span class="badge">{{ e.memory_type }}</span>
          <span v-for="ln in (e.lanes || [])" :key="ln" class="lane" :class="'lane-' + ln"
                :title="t('memory.lane.' + ln)">{{ ln }}</span>
          <span class="excerpt">{{ e.excerpt }}</span>
        </div>
        <p v-if="!searchResult.evidence.length" class="muted">{{ t('memory.noResults') }}</p>
      </div>
    </section>

    <!-- Awaiting approval -->
    <section v-if="candidates.length" class="panel">
      <div class="panel-header">
        <h3>{{ t('memory.awaitingApproval') }} <span class="count">{{ candidates.length }}</span></h3>
        <div class="bulk-actions">
          <span v-if="bulkBusy" class="spin" aria-hidden="true"></span>
          <button class="btn primary" :disabled="bulkBusy" @click="approveAll">{{ t('memory.approveAll') }}</button>
          <template v-if="selectedCount">
            <button class="btn" :disabled="bulkBusy" @click="approveSelected">{{ t('memory.approveSelected', { n: selectedCount }) }}</button>
            <button class="btn danger" :disabled="bulkBusy" @click="rejectSelected">{{ t('memory.rejectSelected', { n: selectedCount }) }}</button>
          </template>
        </div>
      </div>
      <label class="select-all">
        <input type="checkbox" :checked="allSelected" @change="toggleSelectAll" />
        {{ t('memory.selectAll') }}
      </label>
      <div v-for="c in candidates" :key="c.id" class="row-card" :class="{ sel: selected.has(c.id) }">
        <label class="pick">
          <input type="checkbox" :checked="selected.has(c.id)" @change="toggleSelect(c.id)" />
        </label>
        <div class="card-body">
          <span class="badge">{{ c.candidate_type }}</span>
          <span class="content">{{ payloadContent(c) }}</span>
          <!-- Why this still needs review even under auto-save (fail-loud). -->
          <div v-if="c.reason" class="reason-note">⚠ {{ reasonNote(c) }}</div>
        </div>
        <div class="card-actions">
          <button class="btn primary" :disabled="isRowBusy(c.id)" @click="approve(c.id)">{{ t('memory.approve') }}</button>
          <button class="btn ghost" :disabled="isRowBusy(c.id)" @click="reject(c.id)">{{ t('memory.reject') }}</button>
        </div>
      </div>
    </section>

    <!-- Stored memories -->
    <section class="panel">
      <div class="panel-header">
        <h3>{{ t('memory.memories') }} <span class="count">{{ total }}</span></h3>
        <div class="filters">
          <select v-model="typeFilter" class="select">
            <option v-for="ty in TYPES" :key="ty" :value="ty">{{ ty || t('memory.allTypes') }}</option>
          </select>
          <select v-model="statusFilter" class="select">
            <option value="active">{{ t('memory.active') }}</option>
            <option value="archived">{{ t('memory.archived') }}</option>
            <option value="superseded">{{ t('memory.superseded') }}</option>
          </select>
        </div>
      </div>
      <p v-if="loading" class="muted">{{ t('common.loading') }}</p>
      <p v-else-if="!memories.length" class="muted">{{ t('memory.noMemories') }}</p>
      <div v-for="m in memories" :key="m.id" class="mem-item">
        <div class="row-card">
          <div class="card-body">
            <span class="badge">{{ m.memory_type }}</span>
            <span v-if="m.pinned" class="pin">📌</span>
            <span class="content">{{ m.content }}</span>
            <span class="meta">{{ m.authority }} · {{ Math.round(m.confidence * 100) }}%</span>
          </div>
          <div class="card-actions">
            <button class="btn ghost" :class="{ on: openVersions.has(m.id) }"
                    @click="toggleVersions(m.id)">{{ t('memory.versions') }}</button>
            <button v-if="statusFilter === 'active'" class="btn ghost" :disabled="isRowBusy(m.id)"
                    @click="archive(m.id)">{{ t('memory.archive') }}</button>
            <button v-else class="btn ghost" :disabled="isRowBusy(m.id)"
                    @click="restore(m.id)">{{ t('memory.restore') }}</button>
            <button class="btn danger" :disabled="isRowBusy(m.id)" @click="remove(m.id)">{{ t('memory.delete') }}</button>
          </div>
        </div>
        <!-- Version history: audit + roll a conflict-replaced memory back to any
             prior version (the "manage superseded by version" surface). -->
        <div v-if="openVersions.has(m.id)" class="versions">
          <p v-if="!(versionsById[m.id] || []).length" class="muted">{{ t('memory.versionsEmpty') }}</p>
          <div v-for="v in (versionsById[m.id] || [])" :key="v.version" class="ver-row">
            <span class="badge">v{{ v.version }}</span>
            <span class="ver-type">{{ v.change_type }}</span>
            <span class="ver-content">{{ v.content }}</span>
            <span class="ver-ago">{{ timeAgo(v.created_at) }}</span>
            <button v-if="v.version !== m.current_version" class="btn ghost" :disabled="isRowBusy(m.id)"
                    @click="rollback(m.id, v.version)">{{ t('memory.rollbackTo') }}</button>
          </div>
        </div>
      </div>
    </section>

    <!-- Recent searches — per-query retrieval telemetry -->
    <section v-if="runs.length" class="panel">
      <div class="panel-header">
        <h3>{{ t('memory.recentRetrievals') }}</h3>
        <!-- Static config mode, shown ONCE here — not per row (every row is the
             same mode; the per-row pill carries the adaptive signal instead). -->
        <span class="mode-chip">{{ t('memory.runs.modeLabel') }}: {{ runs[0].mode }}</span>
      </div>
      <ul class="runs-list">
        <li v-for="r in runs" :key="r.id" class="run">
          <button class="run-head" :class="{ open: expandedRuns.has(r.id) }"
                  @click="toggleRun(r.id)" :aria-expanded="expandedRuns.has(r.id)">
            <span class="run-caret">▸</span>
            <span class="hash">#{{ r.query_hash.slice(0, 8) }}</span>
            <span class="st" :class="runStatus(r).cls">{{ runStatus(r).label }}</span>
            <span v-if="r.cache_hit" class="st st-cached">⚡ {{ t('memory.runs.cached') }}</span>
            <span class="recall" :class="{ none: r.result_count === 0 }">{{ runRecall(r) }}</span>
            <span v-if="r.evidence_tokens" class="tok">· {{ t('memory.runs.tokens', { n: r.evidence_tokens }) }}</span>
            <span class="spacer"></span>
            <span class="ms">{{ r.total_ms }}ms</span>
            <span class="ago">{{ timeAgo(r.created_at) }}</span>
          </button>
          <div v-if="expandedRuns.has(r.id)" class="run-detail">
            <div class="lanes">{{ runLanes(r) }}</div>
            <div v-if="r.result_ids.length" class="ids">
              {{ t('memory.runs.idsLabel') }}: {{ r.result_ids.join(', ') }}
            </div>
          </div>
        </li>
      </ul>
    </section>
    <section v-else class="panel">
      <div class="panel-header"><h3>{{ t('memory.recentRetrievals') }}</h3></div>
      <p class="muted">{{ t('memory.runs.empty') }}</p>
    </section>
  </div>
</template>

<style scoped>
.mem-panel { max-width: 880px; }
.error { color: var(--danger); margin: 0 0 12px; }

/* Fail-loud banner — dense recall is DOWN. Loud red, action to the right. */
.index-broken {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  background: color-mix(in srgb, var(--danger) 12%, transparent);
  border: 1px solid color-mix(in srgb, var(--danger) 45%, transparent);
  border-radius: var(--r-md); padding: 10px 14px; margin: 0 0 14px;
}
.index-broken .ib-icon { color: var(--danger); font-size: 15px; flex-shrink: 0; }
.index-broken .ib-text { color: var(--text); font-size: 13px; line-height: 1.45; flex: 1; min-width: 0; }
.index-broken .btn.restore {
  background: var(--danger); color: #fff; border-color: var(--danger);
  display: flex; align-items: center; gap: 7px; flex-shrink: 0; font-weight: 600;
}
.index-broken .btn.restore:hover:not(:disabled) { filter: brightness(1.08); background: var(--danger); }
.index-broken .btn.restore .spin { border-top-color: #fff; }
.muted { color: var(--text-dim); font-size: 13px; }

/* Section card — mirrors AgentDetail.vue's `.panel` so every tab matches */
.panel {
  background: var(--bg-1);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 16px;
  margin-bottom: 16px;
}
.panel-header {
  display: flex; align-items: center; justify-content: space-between;
  gap: 12px; margin-bottom: 12px; flex-wrap: wrap;
}
.panel-header h3 {
  font-size: 14px; font-weight: 600; color: var(--text); margin: 0;
  display: flex; align-items: center; gap: 8px;
}
.count {
  font-family: var(--font-mono); font-size: 11px; font-weight: 500;
  color: var(--text-dim); background: var(--bg-3);
  border-radius: 999px; padding: 1px 8px;
}

/* Status strip */
.status-bar { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin-bottom: 14px; font-size: 12px; }
.degraded { color: var(--warning); }
.stat { color: var(--text-dim); }
.stat.danger { color: var(--danger); }

/* Search */
.search-row { display: flex; gap: 8px; }
.search-input { flex: 1; min-width: 0; background: var(--bg-4); color: var(--text);
  border: 1px solid var(--border-strong); border-radius: var(--r-md); padding: 9px 12px; font-size: 13px; }
.search-input:focus { outline: none; border-color: var(--primary); box-shadow: 0 0 0 3px var(--primary-bg-strong); }
.search-result { margin-top: 12px; }
.lane-summary { font-size: 12px; margin-bottom: 6px; display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.evidence { display: flex; gap: 6px; padding: 6px 0; font-size: 13px; align-items: baseline; flex-wrap: wrap; }
.excerpt { color: var(--text-dim); }
/* Retrieval-lane provenance chips — graph is highlighted (the one to watch). */
.lane { font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: .04em;
  padding: 1px 6px; border-radius: 999px; border: 1px solid transparent; line-height: 1.5; }
.lane-fts { color: var(--text-dim); background: var(--bg-4); border-color: var(--border-strong); }
.lane-dense { color: var(--primary); background: var(--primary-bg); border-color: var(--primary-bg-strong); }
.lane-graph { color: #fff; background: var(--primary); border-color: var(--primary); }

/* Filters */
.filters { display: flex; gap: 8px; flex-wrap: wrap; }
.select { background: var(--bg-4); color: var(--text); border: 1px solid var(--border-strong);
  border-radius: var(--r-md); padding: 6px 10px; font-size: 12px; }
.select:focus { outline: none; border-color: var(--primary); }

/* Bulk approval controls */
.bulk-actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.spin { width: 14px; height: 14px; border: 2px solid var(--border-strong); border-top-color: var(--primary);
  border-radius: 50%; animation: spin .7s linear infinite; flex-shrink: 0; }
@keyframes spin { to { transform: rotate(360deg); } }
.select-all { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--text-dim);
  padding: 2px 0 6px; cursor: pointer; user-select: none; }
.pick { display: flex; align-items: center; flex-shrink: 0; cursor: pointer; }
.pick input, .select-all input { accent-color: var(--primary); width: 15px; height: 15px; cursor: pointer; margin: 0; }

/* List rows inside a panel (divider, last row flush) */
.row-card { display: flex; justify-content: space-between; gap: 12px; padding: 12px 0;
  border-bottom: 1px solid var(--border); }
.row-card.sel { box-shadow: inset 2px 0 0 var(--primary); }
.row-card:last-child { border-bottom: none; padding-bottom: 0; }

/* A memory + its (collapsible) version history share one divider so the panel
   reads as one unit. */
.mem-item { border-bottom: 1px solid var(--border); }
.mem-item:last-child { border-bottom: none; }
.mem-item .row-card { border-bottom: none; }
.btn.ghost.on { color: var(--text); background: rgba(255,255,255,0.06); }

/* Version history rows — same divider rhythm, indented under the memory. */
.versions { padding: 2px 0 12px 8px; display: flex; flex-direction: column; gap: 6px; }
.ver-row { display: flex; gap: 8px; align-items: baseline; font-size: 12px;
  color: var(--text-dim); flex-wrap: wrap; }
.ver-type { font-family: var(--font-mono); font-size: 10px; text-transform: uppercase;
  letter-spacing: .04em; color: var(--text-dim); }
.ver-content { flex: 1; min-width: 0; color: var(--text); line-height: 1.5; }
.ver-ago { white-space: nowrap; }
.card-body { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; flex: 1; min-width: 0; }
.content { flex: 1; min-width: 0; line-height: 1.5; }
/* Why review is still needed under auto-save — a calm warning, not an error. */
.reason-note { flex-basis: 100%; font-size: 11.5px; color: var(--warn, #b45309);
  background: var(--warn-bg, color-mix(in srgb, #f59e0b 12%, transparent));
  border-radius: var(--r-sm, 6px); padding: 3px 8px; margin-top: 4px; line-height: 1.45; }
.meta { color: var(--text-dim); font-size: 11px; }
.badge { font-size: 10px; text-transform: uppercase; background: var(--bg-3);
  border-radius: var(--r-sm); padding: 2px 6px; color: var(--text-dim); white-space: nowrap;
  font-family: var(--font-mono); letter-spacing: 0.04em; }
.card-actions { display: flex; gap: 6px; flex-shrink: 0; }

/* Buttons — design tokens */
.btn { background: transparent; color: var(--text-dim); border: 1px solid var(--border-strong);
  border-radius: var(--r-md); padding: 5px 12px; font-size: 12px; cursor: pointer; transition: all .15s; }
.btn:hover:not(:disabled) { color: var(--text); background: rgba(255,255,255,0.04); }
.btn:disabled { opacity: .55; cursor: not-allowed; }
.btn.primary { background: var(--primary); color: #fff; border-color: var(--primary); }
.btn.primary:hover { background: var(--primary-hover); border-color: var(--primary-hover); }
.btn.danger { background: transparent; color: var(--danger); border-color: var(--danger); }
.btn.danger:hover { background: var(--danger); color: #fff; }
.btn.ghost { background: transparent; }

/* Recent searches — telemetry list (mirrors .row-card divider rhythm) */
.mode-chip { font-family: var(--font-mono); font-size: 11px; color: var(--text-dim);
  background: var(--bg-3); border-radius: 999px; padding: 2px 9px; }
.runs-list { list-style: none; margin: 0; padding: 0; }
.run { border-bottom: 1px solid var(--border); }
.run:last-child { border-bottom: none; }

.run-head { width: 100%; display: flex; align-items: center; gap: 8px;
  background: transparent; border: none; padding: 9px 4px; cursor: pointer;
  font-size: 12px; color: var(--text-dim); text-align: left; transition: background .12s; }
.run-head:hover { background: rgba(255,255,255,0.03); }
.run-caret { color: var(--text-dim); font-size: 10px; transition: transform .15s; flex-shrink: 0; }
.run-head.open .run-caret { transform: rotate(90deg); }
.spacer { flex: 1 1 auto; min-width: 8px; }

.hash { font-family: var(--font-mono); font-size: 11px; color: var(--text-dim);
  opacity: .75; flex-shrink: 0; }

/* Status pills — reuse the .lane pill geometry for design consistency. */
.st { font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: .04em;
  padding: 1px 7px; border-radius: 999px; border: 1px solid transparent; line-height: 1.6;
  white-space: nowrap; flex-shrink: 0; }
.st-fast { color: var(--text-dim); background: var(--bg-4); border-color: var(--border-strong); }
.st-deep { color: #fff; background: var(--primary); border-color: var(--primary); }
.st-degraded { color: var(--warning); background: color-mix(in srgb, var(--warning) 14%, transparent);
  border-color: color-mix(in srgb, var(--warning) 40%, transparent); }
.st-cached { color: var(--primary); background: var(--primary-bg); border-color: var(--primary-bg-strong); }

.recall { color: var(--text); white-space: nowrap; }
.recall.none { color: var(--text-dim); font-style: italic; }
.tok { color: var(--text-dim); white-space: nowrap; }
.ms { font-family: var(--font-mono); color: var(--text); white-space: nowrap; flex-shrink: 0; }
.ago { color: var(--text-dim); white-space: nowrap; flex-shrink: 0; min-width: 56px; text-align: right; }

.run-detail { padding: 2px 4px 10px 22px; font-size: 11.5px; color: var(--text-dim);
  display: flex; flex-direction: column; gap: 4px; }
.run-detail .lanes { font-family: var(--font-mono); }
.run-detail .ids { font-family: var(--font-mono); word-break: break-all; opacity: .8; }

@media (max-width: 768px) {
  .search-row { flex-direction: column; }
  .search-row .btn { width: 100%; }
  .panel-header { flex-direction: column; align-items: stretch; }
  .filters .select { flex: 1; }
  .row-card { flex-direction: column; }
  .card-actions { width: 100%; }
  .card-actions .btn { flex: 1; }
}
</style>

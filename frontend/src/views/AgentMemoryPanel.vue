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
function remove(id) {
  if (!confirm(t('memory.confirmDelete'))) return
  return rowAction(id, () => apiFetch(`${base.value}/memories/${id}`, { method: 'DELETE' }))
}

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

watch([typeFilter, statusFilter], loadAll)
watch(() => memStore.indexTick, loadAll)
onMounted(loadAll)
</script>

<template>
  <div class="mem-panel">
    <p v-if="error" class="error">{{ error }}</p>

    <!-- Index status strip -->
    <div class="status-bar">
      <span v-if="memStore.isDegraded(agentName)" class="degraded">⚠ {{ t('memory.degraded') }}</span>
      <template v-if="indexStatus">
        <span class="stat">{{ t('memory.episodic') }}: {{ indexStatus.episodic_documents }}</span>
        <span class="stat" v-if="indexStatus.outbox?.pending">{{ t('memory.indexPending') }}: {{ indexStatus.outbox.pending }}</span>
        <span class="stat danger" v-if="indexStatus.outbox?.dead">{{ t('memory.indexDead') }}: {{ indexStatus.outbox.dead }}</span>
      </template>
    </div>

    <!-- Search -->
    <section class="panel">
      <div class="search-row">
        <input v-model="searchQuery" class="search-input" type="text"
               :placeholder="t('memory.searchPlaceholder')" @keyup.enter="runSearch" />
        <button class="btn primary" @click="runSearch">{{ t('common.search') }}</button>
      </div>
      <div v-if="searchResult" class="search-result">
        <div class="muted" v-if="searchResult.degraded">{{ t('memory.degradedShort') }}</div>
        <div v-for="e in searchResult.evidence" :key="e.evidence_id" class="evidence">
          <span class="badge">{{ e.memory_type }}</span>
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
          </select>
        </div>
      </div>
      <p v-if="loading" class="muted">{{ t('common.loading') }}</p>
      <p v-else-if="!memories.length" class="muted">{{ t('memory.noMemories') }}</p>
      <div v-for="m in memories" :key="m.id" class="row-card">
        <div class="card-body">
          <span class="badge">{{ m.memory_type }}</span>
          <span v-if="m.pinned" class="pin">📌</span>
          <span class="content">{{ m.content }}</span>
          <span class="meta">{{ m.authority }} · {{ Math.round(m.confidence * 100) }}%</span>
        </div>
        <div class="card-actions" v-if="statusFilter === 'active'">
          <button class="btn ghost" :disabled="isRowBusy(m.id)" @click="archive(m.id)">{{ t('memory.archive') }}</button>
          <button class="btn danger" :disabled="isRowBusy(m.id)" @click="remove(m.id)">{{ t('memory.delete') }}</button>
        </div>
      </div>
    </section>

    <!-- Recent retrievals -->
    <section v-if="runs.length" class="panel">
      <div class="panel-header"><h3>{{ t('memory.recentRetrievals') }}</h3></div>
      <div class="runs-scroll">
        <table class="runs">
          <tr v-for="r in runs" :key="r.id">
            <td>{{ r.mode }}</td>
            <td>{{ r.total_ms }}ms</td>
            <td>{{ r.evidence_tokens }} tok</td>
            <td>{{ r.cache_hit ? '⚡' : '' }}</td>
          </tr>
        </table>
      </div>
    </section>
  </div>
</template>

<style scoped>
.mem-panel { max-width: 880px; }
.error { color: var(--danger); margin: 0 0 12px; }
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
.evidence { display: flex; gap: 8px; padding: 6px 0; font-size: 13px; align-items: baseline; }
.excerpt { color: var(--text-dim); }

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
.card-body { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; flex: 1; min-width: 0; }
.content { flex: 1; min-width: 0; line-height: 1.5; }
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

/* Retrievals table */
.runs-scroll { overflow-x: auto; }
.runs { width: 100%; font-size: 12px; color: var(--text-dim); border-collapse: collapse; }
.runs td { padding: 4px 8px; white-space: nowrap; }

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

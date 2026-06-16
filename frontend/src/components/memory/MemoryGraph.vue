<script setup>
/**
 * MemoryGraph — visualises the agent's LadybugDB memory graph (GraphRAG view).
 *
 * Renders the owner-scoped snapshot from GET /api/agents/{name}/memory-graph:
 * Memory nodes + the Entity nodes they MENTION + those edges. Memories that
 * share an entity are connected through it, so clusters of related memories are
 * visible at a glance. cytoscape gives pan / zoom / drag + a force layout.
 *
 * Self-contained: lazy-loads its data on mount (and on demand via Refresh) so
 * opening the Memory tab doesn't pay for the graph unless this section renders.
 * Copy is bilingual off useLang().lang (project English-first rule #7).
 */
import { onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import cytoscape from 'cytoscape'
import { apiFetch } from '../../api'
import { useLang } from '../../composables/useLang.js'

const props = defineProps({ agentName: { type: String, required: true } })
const { lang } = useLang()
const L = (vi, en) => (lang.value === 'vi' ? vi : en)

const container = ref(null)
const loading = ref(false)
const error = ref('')
const available = ref(true)
const counts = ref({ memories: 0, entities: 0 })
const selected = ref(null)          // { kind, label, detail } of the clicked node
let cy = null

function tokenColor(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return v || fallback
}

function render(data) {
  const memColor = tokenColor('--primary', '#6c8cff')
  const entColor = '#d9a13b'
  const edgeColor = tokenColor('--border-strong', '#5a5a6a')
  const textColor = tokenColor('--text-1', '#e8e8ef')

  const elements = [
    ...data.memories.map((m) => ({
      data: { id: m.id, label: m.content || '(memory)', kind: 'memory',
              detail: m.content, sub: m.memory_type },
    })),
    ...data.entities.map((e) => ({
      data: { id: e.id, label: e.name, kind: 'entity', detail: e.etype, sub: e.etype },
    })),
    ...data.edges.map((e, i) => ({
      data: { id: `e${i}`, source: e.source, target: e.target },
    })),
  ]

  if (cy) { cy.destroy(); cy = null }
  cy = cytoscape({
    container: container.value,
    elements,
    style: [
      { selector: 'node', style: {
        label: 'data(label)', color: textColor, 'font-size': 9,
        'text-wrap': 'wrap', 'text-max-width': 90, 'text-valign': 'bottom',
        'text-margin-y': 3, 'background-color': memColor, width: 18, height: 18 } },
      { selector: 'node[kind="entity"]', style: {
        'background-color': entColor, shape: 'round-rectangle', width: 14, height: 14 } },
      { selector: 'node:selected', style: { 'border-width': 3, 'border-color': textColor } },
      { selector: 'edge', style: {
        width: 1, 'line-color': edgeColor, 'curve-style': 'bezier', opacity: 0.6 } },
    ],
    layout: { name: 'cose', animate: false, padding: 20, nodeRepulsion: 6000 },
    minZoom: 0.2, maxZoom: 3,
  })
  cy.on('tap', 'node', (evt) => {
    const d = evt.target.data()
    selected.value = { kind: d.kind, label: d.label, detail: d.detail, sub: d.sub }
  })
  cy.on('tap', (evt) => { if (evt.target === cy) selected.value = null })
}

async function load() {
  loading.value = true
  error.value = ''
  selected.value = null
  try {
    const data = await apiFetch(
      `/api/agents/${encodeURIComponent(props.agentName)}/memory-graph`)
    available.value = data.available !== false
    counts.value = { memories: data.memories.length, entities: data.entities.length }
    if (available.value && data.memories.length) render(data)
    else if (cy) { cy.destroy(); cy = null }
  } catch (e) {
    error.value = e?.message || String(e)
  } finally {
    loading.value = false
  }
}

onMounted(load)
onBeforeUnmount(() => { if (cy) { cy.destroy(); cy = null } })
defineExpose({ reload: load })
</script>

<template>
  <div class="graph-wrap">
    <div class="graph-head">
      <span class="muted">
        {{ counts.memories }} {{ L('ký ức', 'memories') }} ·
        {{ counts.entities }} {{ L('thực thể', 'entities') }}
      </span>
      <button class="btn" :disabled="loading" @click="load">
        {{ loading ? L('Đang tải…', 'Loading…') : L('Làm mới', 'Refresh') }}
      </button>
    </div>

    <p v-if="error" class="muted err">{{ error }}</p>
    <p v-else-if="!available" class="muted">
      {{ L('Đồ thị chưa sẵn sàng (memory tắt hoặc backend không phải LadybugDB).',
            'Graph unavailable (memory off or backend is not LadybugDB).') }}
    </p>
    <p v-else-if="!loading && !counts.memories" class="muted">
      {{ L('Chưa có ký ức nào trong đồ thị.', 'No memories in the graph yet.') }}
    </p>

    <div class="graph-body" v-show="available && counts.memories">
      <div ref="container" class="cy" />
      <div v-if="selected" class="node-detail">
        <div class="nd-kind">{{ selected.kind === 'memory'
          ? L('Ký ức', 'Memory') : L('Thực thể', 'Entity') }} · {{ selected.sub }}</div>
        <div class="nd-text">{{ selected.detail || selected.label }}</div>
      </div>
    </div>
    <div class="legend" v-show="available && counts.memories">
      <span><i class="dot mem" /> {{ L('Ký ức', 'Memory') }}</span>
      <span><i class="dot ent" /> {{ L('Thực thể', 'Entity') }}</span>
      <span class="muted">{{ L('Kéo để di chuyển · cuộn để zoom', 'Drag to pan · scroll to zoom') }}</span>
    </div>
  </div>
</template>

<style scoped>
.graph-wrap { display: flex; flex-direction: column; gap: 8px; }
.graph-head { display: flex; align-items: center; justify-content: space-between; }
.graph-body { position: relative; }
.cy {
  width: 100%; height: 420px;
  background: var(--bg-0, #14141c);
  border: 1px solid var(--border, #2a2a38); border-radius: 10px;
}
.node-detail {
  position: absolute; left: 10px; bottom: 10px; max-width: 60%;
  background: var(--bg-2, #1e1e2a); border: 1px solid var(--border-strong, #3a3a4a);
  border-radius: 8px; padding: 8px 10px; font-size: 12px;
}
.nd-kind { color: var(--text-2, #9a9ab0); font-size: 10px; margin-bottom: 3px; }
.nd-text { color: var(--text-1, #e8e8ef); line-height: 1.4; }
.legend { display: flex; gap: 14px; align-items: center; font-size: 11px; flex-wrap: wrap; }
.dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 4px; vertical-align: middle; }
.dot.mem { background: var(--primary, #6c8cff); }
.dot.ent { background: #d9a13b; border-radius: 2px; }
.err { color: var(--danger, #e0607a); }
.muted { color: var(--text-2, #9a9ab0); }
</style>

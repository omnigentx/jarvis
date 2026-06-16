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

// Short node caption: first few words, so 17 nodes don't turn into text soup.
// The full content shows in the detail panel on click.
function shortLabel(s) {
  const t = (s || '').replace(/\s+/g, ' ').trim()
  return t.length > 26 ? `${t.slice(0, 24)}…` : t || '(memory)'
}

function render(data) {
  const memColor = tokenColor('--primary', '#6c8cff')
  const entColor = '#d9a13b'
  const edgeColor = tokenColor('--border-strong', '#5a5a6a')
  const simColor = tokenColor('--primary', '#6c8cff')
  const textColor = tokenColor('--text-1', '#e8e8ef')

  const elements = [
    ...data.memories.map((m) => ({
      data: { id: m.id, label: shortLabel(m.content), kind: 'memory',
              detail: m.content, sub: m.memory_type },
    })),
    ...data.entities.map((e) => ({
      data: { id: e.id, label: e.name, kind: 'entity', detail: e.etype, sub: e.etype },
    })),
    ...data.edges.map((e, i) => ({
      data: { id: `e${i}`, source: e.source, target: e.target, kind: e.kind || 'mentions' },
    })),
  ]

  if (cy) { cy.destroy(); cy = null }
  cy = cytoscape({
    container: container.value,
    elements,
    style: [
      { selector: 'node', style: {
        label: 'data(label)', color: textColor, 'font-size': 10,
        'text-wrap': 'wrap', 'text-max-width': 110, 'text-valign': 'bottom',
        'text-margin-y': 4, 'background-color': memColor,
        'border-width': 2, 'border-color': tokenColor('--bg-0', '#14141c'),
        width: 26, height: 26 } },
      { selector: 'node[kind="entity"]', style: {
        'background-color': entColor, shape: 'round-rectangle', width: 20, height: 20 } },
      { selector: 'node:selected', style: {
        'border-width': 3, 'border-color': textColor, width: 32, height: 32 } },
      { selector: 'edge', style: {
        width: 1.5, 'line-color': edgeColor, 'curve-style': 'bezier', opacity: 0.7 } },
      { selector: 'edge[kind="similar"]', style: {
        'line-color': simColor, 'line-style': 'dashed', width: 1, opacity: 0.45 } },
    ],
    layout: {
      name: 'cose', animate: false, fit: true, padding: 36,
      nodeRepulsion: 7000, idealEdgeLength: 60,
      componentSpacing: 70, nodeOverlap: 18, gravity: 0.7,
    },
    minZoom: 0.2, maxZoom: 3, wheelSensitivity: 0.3,
  })
  cy.ready(() => cy.fit(undefined, 36))     // ensure every node sits inside the viewport
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
      <span><i class="line sim" /> {{ L('Liên quan (ngữ nghĩa)', 'Related (semantic)') }}</span>
      <span class="muted">{{ L('Bấm node để xem · kéo để di chuyển · cuộn để zoom',
                              'Click a node · drag to pan · scroll to zoom') }}</span>
    </div>
  </div>
</template>

<style scoped>
.graph-wrap { display: flex; flex-direction: column; gap: 8px; }
.graph-head { display: flex; align-items: center; justify-content: space-between; }
.graph-body { position: relative; }
.cy {
  width: 100%; height: 480px;
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
.line { display: inline-block; width: 16px; height: 0; vertical-align: middle; margin-right: 4px;
  border-top: 1.5px dashed var(--primary, #6c8cff); opacity: 0.7; }
.err { color: var(--danger, #e0607a); }
.muted { color: var(--text-2, #9a9ab0); }
</style>

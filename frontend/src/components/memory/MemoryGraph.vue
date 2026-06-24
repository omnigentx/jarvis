<script setup>
/**
 * MemoryGraph — the agent's memory as a KNOWLEDGE GRAPH.
 *
 * Renders GET /api/agents/{name}/memory-graph: entity nodes connected by typed
 * RELATES edges — (User)-[likes]->(tea), (User)-[works at]->
 * (AcmeCorp). The user hub ('subject') sits at the centre; leaves are
 * 'object' entities. Each edge is labelled with its predicate, so the graph
 * reads like a property-graph DB rather than a blob of memory text.
 *
 * Triples are extracted by the backend (LLM) and projected as RELATES edges;
 * the "Trích xuất quan hệ" button forces a rebuild from the current memories.
 * Copy is bilingual off useLang().lang (project English-first rule #7).
 */
import { onBeforeUnmount, onMounted, ref } from 'vue'
import cytoscape from 'cytoscape'
import { apiFetch } from '../../api'
import { useLang } from '../../composables/useLang.js'

const props = defineProps({ agentName: { type: String, required: true } })
const { lang } = useLang()
const L = (vi, en) => (lang.value === 'vi' ? vi : en)

const container = ref(null)
const loading = ref(false)
const rebuilding = ref(false)
const error = ref('')
const available = ref(true)
const counts = ref({ nodes: 0, edges: 0 })
const selected = ref(null)
let cy = null

function tokenColor(name, fallback) {
  // Read off the themed container so we get the ACTIVE theme's value.
  const el = container.value || document.documentElement
  const v = getComputedStyle(el).getPropertyValue(name).trim()
  return v || fallback
}

function render(data) {
  const hubColor = tokenColor('--primary', '#6c8cff')
  const objColor = '#d9a13b'
  // --text-dim contrasts on BOTH themes (dark slate on light, light grey on dark);
  // --border-strong was too faint on the light theme's white canvas.
  const edgeColor = tokenColor('--text-dim', '#6b7185')
  const textColor = tokenColor('--text', '#0e1019')   // resolves light/dark per theme
  const halo = tokenColor('--bg-0', '#ffffff')        // thin transparent halo, no filled box

  const elements = [
    ...data.nodes.map((n) => ({
      data: { id: n.id, label: n.label, kind: n.kind || 'object' },
    })),
    ...data.edges.map((e, i) => ({
      data: { id: `r${i}`, source: e.source, target: e.target, label: e.predicate || '' },
    })),
  ]

  if (cy) { cy.destroy(); cy = null }
  cy = cytoscape({
    container: container.value,
    elements,
    style: [
      { selector: 'node', style: {
        label: 'data(label)', color: textColor, 'font-size': 11, 'font-weight': 600,
        'text-wrap': 'wrap', 'text-max-width': 130, 'text-valign': 'bottom', 'text-margin-y': 4,
        'background-color': objColor, shape: 'round-rectangle',
        'text-outline-color': halo, 'text-outline-width': 1, 'text-outline-opacity': 0.55,
        'border-width': 2, 'border-color': halo, width: 22, height: 22 } },
      // The user hub stands out: primary colour, round, larger.
      { selector: 'node[kind="subject"]', style: {
        'background-color': hubColor, shape: 'ellipse', width: 40, height: 40,
        'font-size': 13 } },
      { selector: 'node:selected', style: {
        'border-width': 3, 'border-color': textColor } },
      { selector: 'edge', style: {
        width: 1, 'line-color': edgeColor, 'curve-style': 'bezier', opacity: 0.9,
        'target-arrow-color': edgeColor, 'target-arrow-shape': 'triangle', 'arrow-scale': 0.8,
        // predicate ON the edge — this is what makes it read as a knowledge graph.
        label: 'data(label)', 'font-size': 9, color: textColor,
        'text-rotation': 'autorotate', 'text-outline-color': halo,
        'text-outline-width': 2, 'text-outline-opacity': 0.85 } },
    ],
    layout: {
      name: 'cose', animate: false, fit: true, padding: 36,
      nodeRepulsion: 9000, idealEdgeLength: 95, componentSpacing: 80,
      nodeOverlap: 20, gravity: 0.6,
    },
    minZoom: 0.2, maxZoom: 3, wheelSensitivity: 0.3,
  })
  cy.ready(() => cy.fit(undefined, 36))
  cy.on('tap', 'node', (evt) => {
    const d = evt.target.data()
    selected.value = { kind: d.kind, label: d.label }
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
    counts.value = { nodes: data.nodes.length, edges: data.edges.length }
    if (available.value && data.nodes.length) render(data)
    else if (cy) { cy.destroy(); cy = null }
  } catch (e) {
    error.value = e?.message || String(e)
  } finally {
    loading.value = false
  }
}

async function rebuild() {
  rebuilding.value = true
  error.value = ''
  try {
    await apiFetch(`/api/agents/${encodeURIComponent(props.agentName)}/memory-graph/rebuild`,
                   { method: 'POST' })
    // projection is async (the worker drains the re-index); give it a moment.
    await new Promise((r) => setTimeout(r, 2500))
    await load()
  } catch (e) {
    error.value = e?.message || String(e)
  } finally {
    rebuilding.value = false
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
        {{ counts.nodes }} {{ L('thực thể', 'entities') }} ·
        {{ counts.edges }} {{ L('quan hệ', 'relations') }}
      </span>
      <div class="head-actions">
        <button class="btn" :disabled="rebuilding" @click="rebuild">
          {{ rebuilding ? L('Đang trích xuất…', 'Extracting…') : L('Trích xuất quan hệ', 'Extract relations') }}
        </button>
        <button class="btn" :disabled="loading" @click="load">
          {{ loading ? L('Đang tải…', 'Loading…') : L('Làm mới', 'Refresh') }}
        </button>
      </div>
    </div>

    <p v-if="error" class="muted err">{{ error }}</p>
    <p v-else-if="!available" class="muted">
      {{ L('Đồ thị chưa sẵn sàng (memory tắt hoặc backend không phải LadybugDB).',
            'Graph unavailable (memory off or backend is not LadybugDB).') }}
    </p>
    <p v-else-if="!loading && !counts.nodes" class="muted">
      {{ L('Chưa có quan hệ nào. Bấm “Trích xuất quan hệ” để dựng đồ thị từ ký ức.',
            'No relations yet. Click “Extract relations” to build the graph from memories.') }}
    </p>

    <div class="graph-body" v-show="available && counts.nodes">
      <div ref="container" class="cy" />
      <div v-if="selected" class="node-detail">
        <div class="nd-kind">{{ selected.kind === 'subject'
          ? L('Chủ thể', 'Subject') : L('Thực thể', 'Entity') }}</div>
        <div class="nd-text">{{ selected.label }}</div>
      </div>
    </div>
    <div class="legend" v-show="available && counts.nodes">
      <span><i class="dot hub" /> {{ L('Người dùng (chủ thể)', 'User (subject)') }}</span>
      <span><i class="dot obj" /> {{ L('Thực thể', 'Entity') }}</span>
      <span class="muted">{{ L('Nhãn trên mũi tên = quan hệ · bấm node để xem · kéo/zoom',
                              'Arrow label = relation · click a node · drag/zoom') }}</span>
    </div>
  </div>
</template>

<style scoped>
.graph-wrap { display: flex; flex-direction: column; gap: 8px; }
.graph-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.head-actions { display: flex; gap: 6px; }
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
.nd-kind { color: var(--text-dim, #9a9ab0); font-size: 10px; margin-bottom: 3px; }
.nd-text { color: var(--text, #e8e8ef); line-height: 1.4; }
.legend { display: flex; gap: 14px; align-items: center; font-size: 11px; flex-wrap: wrap; }
.dot { display: inline-block; width: 11px; height: 11px; border-radius: 50%; margin-right: 4px; vertical-align: middle; }
.dot.hub { background: var(--primary, #6c8cff); }
.dot.obj { background: #d9a13b; border-radius: 3px; }
.err { color: var(--danger, #e0607a); }
.muted { color: var(--text-dim, #9a9ab0); }
</style>

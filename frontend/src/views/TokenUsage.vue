<script setup>
/**
 * Token Usage — LLM token consumption + cost dashboard.
 *
 * Logic preserved verbatim from the previous TokenUsage view (REST fetch
 * on period change, SSE merge from store.tokenMetrics, mono-numeric table
 * + simple SVG bar chart). Only the visual shell + tokens changed.
 */
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { apiFetch } from '../api'
import { useAgentsStore } from '../stores/agents'

const route = useRoute()
const store = useAgentsStore()

// ─── State ───
const period = ref('24h')
const isLoading = ref(false)
const metricsData = ref(null)
const breakdownTab = ref('agent') // agent | model | tool

const periods = [
  { value: '1h', label: '1H' },
  { value: '24h', label: '24H' },
  { value: '7d', label: '7D' },
  { value: '30d', label: '30D' },
  { value: 'all', label: 'All' },
]

async function fetchMetrics() {
  isLoading.value = true
  try {
    const agentFilter = route.query.agent || ''
    const params = `?period=${period.value}` + (agentFilter ? `&agent=${agentFilter}` : '')
    const data = await apiFetch(`/api/metrics/tokens${params}`)
    metricsData.value = data
  } catch (e) {
    console.error('[TokenUsage] Failed to fetch metrics:', e)
  } finally {
    isLoading.value = false
  }
}

watch(period, fetchMetrics)

onMounted(() => {
  fetchMetrics()
})

// SSE merge — same algorithm as the previous implementation.
watch(
  () => store.tokenMetrics,
  (newMetrics) => {
    if (!metricsData.value || !newMetrics.size) return
    mergeSSEIntoMetrics(newMetrics)
  },
  { deep: true }
)

function mergeSSEIntoMetrics(sseMap) {
  const data = metricsData.value
  if (!data) return

  for (const [agentName, sse] of sseMap) {
    const mergedKey = `_merged_${agentName}`
    const prevMerged = data[mergedKey] || {
      total_tokens: 0, input_tokens: 0, output_tokens: 0,
      cached_tokens: 0, est_cost: 0, llm_calls: 0,
    }

    const delta = {
      total_tokens: (sse.total_tokens || 0) - prevMerged.total_tokens,
      input_tokens: (sse.input_tokens || 0) - prevMerged.input_tokens,
      output_tokens: (sse.output_tokens || 0) - prevMerged.output_tokens,
      cached_tokens: (sse.cached_tokens || 0) - prevMerged.cached_tokens,
      est_cost: (sse.est_cost || 0) - prevMerged.est_cost,
      llm_calls: (sse.llm_calls || 0) - prevMerged.llm_calls,
    }

    if (delta.total_tokens <= 0 && delta.llm_calls <= 0) continue

    if (!data.totals) data.totals = {}
    data.totals.total_tokens = (data.totals.total_tokens || 0) + delta.total_tokens
    data.totals.input_tokens = (data.totals.input_tokens || 0) + delta.input_tokens
    data.totals.output_tokens = (data.totals.output_tokens || 0) + delta.output_tokens
    data.totals.cached_tokens = (data.totals.cached_tokens || 0) + delta.cached_tokens
    data.totals.est_cost = (data.totals.est_cost || 0) + delta.est_cost
    data.totals.llm_calls = (data.totals.llm_calls || 0) + delta.llm_calls

    if (!data.agents) data.agents = []
    let agent = data.agents.find(a => a.agent_name === agentName)
    if (!agent) {
      agent = { agent_name: agentName, total_tokens: 0, input_tokens: 0, output_tokens: 0, cached_tokens: 0, est_cost: 0, llm_calls: 0 }
      data.agents.push(agent)
    }
    agent.total_tokens += delta.total_tokens
    agent.input_tokens += delta.input_tokens
    agent.output_tokens += delta.output_tokens
    agent.cached_tokens = (agent.cached_tokens || 0) + delta.cached_tokens
    agent.est_cost += delta.est_cost
    agent.llm_calls += delta.llm_calls

    if (sse.model) {
      if (!data.models) data.models = []
      let model = data.models.find(m => m.model === sse.model)
      if (!model) {
        model = { model: sse.model, total_tokens: 0, input_tokens: 0, output_tokens: 0, est_cost: 0, llm_calls: 0 }
        data.models.push(model)
      }
      model.total_tokens += delta.total_tokens
      model.input_tokens += delta.input_tokens
      model.output_tokens += delta.output_tokens
      model.est_cost += delta.est_cost
      model.llm_calls += delta.llm_calls
    }

    data[mergedKey] = { ...sse }
  }

  if (data.agents) data.agents.sort((a, b) => b.total_tokens - a.total_tokens)
  if (data.models) data.models.sort((a, b) => b.total_tokens - a.total_tokens)

  metricsData.value = { ...data }
}

// Formatters
function fmtTokens(n) {
  if (!n || n === 0) return '0'
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`
  return String(n)
}
function fmtCost(c) {
  if (!c || c === 0) return '$0.00'
  if (c < 0.01) return `$${c.toFixed(4)}`
  return `$${c.toFixed(2)}`
}
function fmtPercent(n) {
  if (!n || n === 0) return '0%'
  return `${n.toFixed(1)}%`
}

const totals = computed(() => metricsData.value?.totals || {})
const agentsList = computed(() => metricsData.value?.agents || [])
const modelsList = computed(() => metricsData.value?.models || [])
const toolsList = computed(() => metricsData.value?.tools || [])

const cacheHitRate = computed(() => {
  const t = totals.value
  if (!t.input_tokens) return 0
  return (t.cached_tokens || 0) / t.input_tokens * 100
})

const chartMax = computed(() => {
  const agents = agentsList.value
  if (!agents.length) return 1
  return Math.max(...agents.map(a => a.total_tokens || 0), 1)
})

const kpis = computed(() => [
  {
    label: 'TOTAL TOKENS',
    value: fmtTokens(totals.value.total_tokens),
    sub: `${totals.value.llm_calls || 0} llm calls`,
  },
  {
    label: 'EST COST',
    value: fmtCost(totals.value.est_cost),
    sub: period.value === 'all' ? 'all time' : `last ${period.value}`,
  },
  {
    label: 'INPUT',
    value: fmtTokens(totals.value.input_tokens),
    sub: 'prompt context',
  },
  {
    label: 'OUTPUT',
    value: fmtTokens(totals.value.output_tokens),
    sub: 'model responses',
  },
  {
    label: 'CACHED',
    value: fmtPercent(cacheHitRate.value),
    sub: `${fmtTokens(totals.value.cached_tokens || 0)} cached`,
  },
])
</script>

<template>
  <div class="tokens jv">
    <!-- ─── Header ─── -->
    <div class="tokens__header">
      <div class="tokens__heading">
        <div class="eyebrow">OPERATIONS · TOKEN USAGE</div>
        <h1 class="tokens__title">
          <span class="grad" style="font-style: italic;">{{ fmtTokens(totals.total_tokens) }}</span>
          tokens · {{ fmtCost(totals.est_cost) }}
          <span class="tokens__title-sub">
            · {{ period === 'all' ? 'all time' : `last ${period}` }} · live
          </span>
        </h1>
        <p class="tokens__desc">
          Aggregated from <code class="tokens__inline-code">token_usage</code> SSE events.
          Mono-numeric breakdown by agent / model / tool.
        </p>
      </div>
      <div class="seg tokens__period">
        <button
          v-for="p in periods"
          :key="p.value"
          :class="{ 'is-active': period === p.value }"
          @click="period = p.value"
        >{{ p.label }}</button>
      </div>
    </div>

    <!-- ─── KPI cards ─── -->
    <div class="tokens__kpi">
      <div v-for="k in kpis" :key="k.label" class="card tokens__kpi-card">
        <div class="mono-label">{{ k.label }}</div>
        <div class="tokens__kpi-value">{{ isLoading ? '…' : k.value }}</div>
        <div class="tokens__kpi-sub">{{ k.sub }}</div>
      </div>
    </div>

    <!-- ─── Chart + model breakdown ─── -->
    <div class="tokens__body">
      <div class="card tokens__chart-card">
        <div class="tokens__panel-head">
          <h2 class="tokens__panel-title">Agent token distribution</h2>
          <div class="tokens__legend">
            <span><i class="tokens__legend-dot" style="background: var(--primary);"></i> Input</span>
            <span><i class="tokens__legend-dot" style="background: var(--accent);"></i> Output</span>
            <span><i class="tokens__legend-dot" style="background: var(--success);"></i> Cached</span>
          </div>
        </div>

        <div v-if="!agentsList.length && !isLoading" class="tokens__empty">
          <span>📉 No token data for this period</span>
        </div>

        <div v-else class="tokens__bars">
          <div v-for="agent in agentsList.slice(0, 10)" :key="agent.agent_name" class="tokens__bar-row">
            <span class="tokens__bar-name">{{ agent.agent_name }}</span>
            <div class="tokens__bar-track">
              <div
                class="tokens__bar-input"
                :style="{
                  width: `${((agent.input_tokens - (agent.cached_tokens || 0)) / chartMax) * 100}%`,
                  minWidth: agent.input_tokens > 0 ? '2px' : 0,
                }"
              ></div>
              <div
                class="tokens__bar-output"
                :style="{
                  width: `${(agent.output_tokens / chartMax) * 100}%`,
                  minWidth: agent.output_tokens > 0 ? '2px' : 0,
                }"
              ></div>
              <div
                class="tokens__bar-cached"
                :style="{
                  width: `${((agent.cached_tokens || 0) / chartMax) * 100}%`,
                  minWidth: (agent.cached_tokens || 0) > 0 ? '2px' : 0,
                }"
              ></div>
            </div>
            <span class="tokens__bar-total">{{ fmtTokens(agent.total_tokens) }}</span>
          </div>
        </div>
      </div>

      <!-- By model -->
      <div class="card tokens__model-card">
        <h2 class="tokens__panel-title">By model</h2>
        <div v-if="!modelsList.length && !isLoading" class="tokens__empty tokens__empty--small">No data</div>
        <div v-for="(model, i) in modelsList" :key="model.model" class="tokens__model-row">
          <div class="tokens__model-head">
            <span class="tokens__model-name">{{ model.model }}</span>
            <span class="tokens__model-cost">{{ fmtCost(model.est_cost) }}</span>
          </div>
          <div class="tokens__model-bar">
            <div
              class="tokens__model-fill"
              :style="{
                width: `${modelsList.length ? (model.total_tokens / (modelsList[0]?.total_tokens || 1)) * 100 : 0}%`,
                background: i === 0
                  ? 'var(--primary)'
                  : i === 1
                  ? 'var(--accent)'
                  : 'var(--accent-warm)',
              }"
            ></div>
          </div>
          <div class="tokens__model-meta">
            <span>{{ fmtTokens(model.total_tokens) }} tokens</span>
            <span>{{ model.llm_calls }} calls</span>
          </div>
        </div>
      </div>
    </div>

    <!-- ─── Breakdown tabs + table ─── -->
    <div class="card tokens__table-card">
      <div class="tokens__panel-head">
        <h2 class="tokens__panel-title">Breakdown</h2>
        <div class="seg">
          <button :class="{ 'is-active': breakdownTab === 'agent' }" @click="breakdownTab = 'agent'">By agent</button>
          <button :class="{ 'is-active': breakdownTab === 'model' }" @click="breakdownTab = 'model'">By model</button>
          <button :class="{ 'is-active': breakdownTab === 'tool' }" @click="breakdownTab = 'tool'">By tool</button>
        </div>
      </div>

      <!-- By agent -->
      <template v-if="breakdownTab === 'agent'">
        <div class="tokens__row tokens__row--head">
          <span>Agent</span>
          <span class="tokens__num">Input</span>
          <span class="tokens__num">Output</span>
          <span class="tokens__num">Total</span>
          <span class="tokens__num">Cached</span>
          <span class="tokens__num">Cost</span>
        </div>
        <div v-if="!agentsList.length && !isLoading" class="tokens__empty">No agent usage for this period.</div>
        <div v-for="agent in agentsList" :key="agent.agent_name" class="tokens__row">
          <span class="tokens__row-name">
            {{ agent.agent_name }}
            <span class="tokens__calls">{{ agent.llm_calls }} calls</span>
          </span>
          <span class="tokens__num">{{ fmtTokens(agent.input_tokens) }}</span>
          <span class="tokens__num">{{ fmtTokens(agent.output_tokens) }}</span>
          <span class="tokens__num tokens__num--strong">{{ fmtTokens(agent.total_tokens) }}</span>
          <span class="tokens__num tokens__num--ok">{{ fmtTokens(agent.cached_tokens || 0) }}</span>
          <span class="tokens__num tokens__num--warm">{{ fmtCost(agent.est_cost) }}</span>
        </div>
      </template>

      <!-- By model -->
      <template v-else-if="breakdownTab === 'model'">
        <div class="tokens__row tokens__row--head">
          <span>Model</span>
          <span class="tokens__num">Input</span>
          <span class="tokens__num">Output</span>
          <span class="tokens__num">Total</span>
          <span class="tokens__num">Calls</span>
          <span class="tokens__num">Cost</span>
        </div>
        <div v-if="!modelsList.length && !isLoading" class="tokens__empty">No model usage for this period.</div>
        <div v-for="model in modelsList" :key="model.model" class="tokens__row">
          <span class="tokens__row-name">{{ model.model }}</span>
          <span class="tokens__num">{{ fmtTokens(model.input_tokens) }}</span>
          <span class="tokens__num">{{ fmtTokens(model.output_tokens) }}</span>
          <span class="tokens__num tokens__num--strong">{{ fmtTokens(model.total_tokens) }}</span>
          <span class="tokens__num">{{ model.llm_calls }}</span>
          <span class="tokens__num tokens__num--warm">{{ fmtCost(model.est_cost) }}</span>
        </div>
      </template>

      <!-- By tool -->
      <template v-else>
        <div class="tokens__row tokens__row--head">
          <span>Tool</span>
          <span class="tokens__num">Calls</span>
          <span class="tokens__num">Cost</span>
        </div>
        <div v-if="!toolsList.length && !isLoading" class="tokens__empty">No per-tool breakdown available for this period.</div>
        <div v-for="tool in toolsList" :key="tool.name" class="tokens__row">
          <span class="tokens__row-name tokens__row-name--mono">{{ tool.name }}</span>
          <span class="tokens__num">{{ tool.calls || 0 }}</span>
          <span class="tokens__num tokens__num--warm">{{ fmtCost(tool.est_cost) }}</span>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.tokens {
  max-width: 1200px;
  display: flex;
  flex-direction: column;
  gap: 14px;
  color: var(--text);
}

/* Header */
.tokens__header {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  gap: 14px;
  padding-bottom: 14px;
  border-bottom: 1px solid var(--border);
}
.tokens__heading { display: flex; flex-direction: column; gap: 4px; }
.tokens__title {
  font-family: var(--font-display);
  font-size: 22px;
  letter-spacing: -0.02em;
  margin: 4px 0 0;
}
.tokens__title-sub {
  color: var(--text-muted);
  font-size: 14px;
  font-weight: 400;
  margin-left: 6px;
}
.tokens__desc { font-size: 12.5px; color: var(--text-dim); }
.tokens__inline-code {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--accent);
}
.tokens__period { flex-shrink: 0; }

/* KPI */
.tokens__kpi {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 12px;
}
.tokens__kpi-card {
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.tokens__kpi-value {
  font-family: var(--font-mono);
  font-size: 22px;
  color: var(--text);
  line-height: 1.1;
}
.tokens__kpi-sub {
  font-family: var(--font-mono);
  font-size: 10.5px;
  letter-spacing: 0.04em;
  color: var(--text-muted);
}

/* Body row */
.tokens__body {
  display: grid;
  grid-template-columns: 1.6fr 1fr;
  gap: 14px;
}

/* Panels */
.tokens__chart-card,
.tokens__model-card,
.tokens__table-card { padding: 18px; }

.tokens__panel-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  margin-bottom: 14px;
  flex-wrap: wrap;
}
.tokens__panel-title { font-size: 14px; font-weight: 600; margin: 0; }
.tokens__legend {
  display: flex;
  gap: 12px;
  font-family: var(--font-mono);
  font-size: 10.5px;
  color: var(--text-muted);
}
.tokens__legend-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 2px;
  margin-right: 4px;
  vertical-align: middle;
}

.tokens__empty {
  padding: 30px 12px;
  text-align: center;
  color: var(--text-muted);
  font-size: 13px;
}
.tokens__empty--small { padding: 18px 12px; font-size: 12.5px; }

/* Bars */
.tokens__bars {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.tokens__bar-row {
  display: grid;
  grid-template-columns: 140px 1fr 70px;
  align-items: center;
  gap: 12px;
}
.tokens__bar-name {
  font-size: 12.5px;
  color: var(--text-dim);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.tokens__bar-track {
  display: flex;
  height: 18px;
  background: var(--bg-3);
  border-radius: 999px;
  overflow: hidden;
}
.tokens__bar-input { background: var(--primary); height: 100%; transition: width 0.4s var(--ease-out); }
.tokens__bar-output { background: var(--accent); height: 100%; transition: width 0.4s var(--ease-out); }
.tokens__bar-cached { background: var(--success); height: 100%; transition: width 0.4s var(--ease-out); }

.tokens__bar-total {
  font-family: var(--font-mono);
  font-size: 12px;
  font-weight: 600;
  color: var(--text);
  text-align: right;
}

/* Model breakdown */
.tokens__model-row {
  margin-bottom: 14px;
}
.tokens__model-row:last-child { margin-bottom: 0; }
.tokens__model-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 5px;
}
.tokens__model-name {
  font-family: var(--font-mono);
  font-size: 12.5px;
  color: var(--text);
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tokens__model-cost {
  font-family: var(--font-mono);
  font-size: 11.5px;
  color: var(--text-muted);
}
.tokens__model-bar {
  height: 6px;
  background: var(--bg-3);
  border-radius: 999px;
  overflow: hidden;
}
.tokens__model-fill {
  height: 100%;
  border-radius: 999px;
  transition: width 0.4s var(--ease-out);
}
.tokens__model-meta {
  display: flex;
  justify-content: space-between;
  margin-top: 4px;
  font-family: var(--font-mono);
  font-size: 10.5px;
  color: var(--text-subtle);
}

/* Breakdown table */
.tokens__row {
  display: grid;
  grid-template-columns: 1.6fr 1fr 1fr 1fr 1fr 0.8fr;
  gap: 12px;
  padding: 10px 0;
  align-items: center;
  border-bottom: 1px solid var(--border);
  font-size: 12.5px;
}
.tokens__row:last-child { border-bottom: 0; }
.tokens__row--head {
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  border-bottom-color: var(--border-strong);
}
.tokens__row--head .tokens__num { color: var(--text-muted); }

.tokens__row-name {
  color: var(--text);
  display: flex;
  align-items: center;
  gap: 8px;
}
.tokens__row-name--mono {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--accent-warm);
}
.tokens__calls {
  padding: 1px 6px;
  border-radius: 3px;
  background: var(--bg-3);
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-muted);
}
.tokens__num {
  font-family: var(--font-mono);
  font-size: 12.5px;
  color: var(--text-dim);
  text-align: right;
}
.tokens__num--strong { color: var(--text); font-weight: 600; }
.tokens__num--ok { color: var(--success); }
.tokens__num--warm { color: var(--accent-warm); }

/* Mobile */
@media (max-width: 900px) {
  .tokens__kpi { grid-template-columns: repeat(2, 1fr); }
  .tokens__body { grid-template-columns: 1fr; }
}
@media (max-width: 768px) {
  .tokens__header { flex-direction: column; align-items: flex-start; }
  .tokens__row,
  .tokens__row--head {
    grid-template-columns: 1.4fr 0.9fr 0.7fr;
  }
  .tokens__row > :nth-child(2),
  .tokens__row > :nth-child(3),
  .tokens__row > :nth-child(5),
  .tokens__row--head > :nth-child(2),
  .tokens__row--head > :nth-child(3),
  .tokens__row--head > :nth-child(5) {
    display: none;
  }
  .tokens__bar-row { grid-template-columns: 110px 1fr 60px; }
}
</style>

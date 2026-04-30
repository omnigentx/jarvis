<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { apiFetch } from '../api'
import { useAgentsStore } from '../stores/agents'

const route = useRoute()
const store = useAgentsStore()

// --- State ---
const period = ref('24h')
const isLoading = ref(false)
const metricsData = ref(null)

// Period tabs
const periods = [
  { value: '1h', label: '1H' },
  { value: '24h', label: '24H' },
  { value: '7d', label: '7D' },
  { value: '30d', label: '30D' },
  { value: 'all', label: 'All' },
]

// --- Fetch (REST — only on mount + period change) ---
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

// Refetch on period change (user-initiated)
watch(period, fetchMetrics)

onMounted(() => {
  fetchMetrics()
})

// --- SSE: merge token_usage events directly into metricsData ---
// No polling, no refetch. Pure realtime merge.
watch(
  () => store.tokenMetrics,
  (newMetrics) => {
    if (!metricsData.value || !newMetrics.size) return
    mergeSSEIntoMetrics(newMetrics)
  },
  { deep: true }
)

function mergeSSEIntoMetrics(sseMap) {
  // The store accumulates per-agent SSE data in tokenMetrics Map.
  // We merge the latest event delta into metricsData in-place.
  // The store re-creates the Map on each event, so we diff against what we last merged.
  const data = metricsData.value
  if (!data) return

  for (const [agentName, sse] of sseMap) {
    // --- Merge into totals ---
    // We use a marker to track what we've already merged
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

    // Skip if no new delta
    if (delta.total_tokens <= 0 && delta.llm_calls <= 0) continue

    // Update totals
    if (!data.totals) data.totals = {}
    data.totals.total_tokens = (data.totals.total_tokens || 0) + delta.total_tokens
    data.totals.input_tokens = (data.totals.input_tokens || 0) + delta.input_tokens
    data.totals.output_tokens = (data.totals.output_tokens || 0) + delta.output_tokens
    data.totals.cached_tokens = (data.totals.cached_tokens || 0) + delta.cached_tokens
    data.totals.est_cost = (data.totals.est_cost || 0) + delta.est_cost
    data.totals.llm_calls = (data.totals.llm_calls || 0) + delta.llm_calls

    // --- Upsert agent breakdown ---
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

    // --- Upsert model breakdown ---
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

    // Track what we merged
    data[mergedKey] = { ...sse }
  }

  // Sort agents by total_tokens descending
  if (data.agents) data.agents.sort((a, b) => b.total_tokens - a.total_tokens)
  if (data.models) data.models.sort((a, b) => b.total_tokens - a.total_tokens)

  // Trigger Vue reactivity
  metricsData.value = { ...data }
}

// --- Formatters ---
function fmtTokens(n) {
  if (!n || n === 0) return '0'
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
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

// --- Computed ---
const totals = computed(() => metricsData.value?.totals || {})
const agentsList = computed(() => metricsData.value?.agents || [])
const modelsList = computed(() => metricsData.value?.models || [])

const cacheHitRate = computed(() => {
  const t = totals.value
  if (!t.input_tokens) return 0
  return (t.cached_tokens || 0) / t.input_tokens * 100
})

// Bar chart: max is the highest total_tokens among agents
const chartMax = computed(() => {
  const agents = agentsList.value
  if (!agents.length) return 1
  return Math.max(...agents.map(a => a.total_tokens || 0), 1)
})

// Metric cards data
const cards = computed(() => [
  {
    label: 'Total Tokens',
    value: fmtTokens(totals.value.total_tokens),
    sub: `${totals.value.llm_calls || 0} LLM calls`,
    color: '#3b82f6',
    icon: '📊',
  },
  {
    label: 'Input Tokens',
    value: fmtTokens(totals.value.input_tokens),
    sub: 'Prompt context',
    color: '#3b82f6',
    icon: '📥',
  },
  {
    label: 'Output Tokens',
    value: fmtTokens(totals.value.output_tokens),
    sub: 'Model responses',
    color: '#54b6ff',
    icon: '📤',
  },
  {
    label: 'Cache Hit Rate',
    value: fmtPercent(cacheHitRate.value),
    sub: `${fmtTokens(totals.value.cached_tokens || 0)} cached`,
    color: '#22c55e',
    icon: '⚡',
  },
  {
    label: 'Est. Cost',
    value: fmtCost(totals.value.est_cost),
    sub: period.value === 'all' ? 'All time' : `Last ${period.value}`,
    color: '#ffb547',
    icon: '💰',
  },
])
</script>

<template>
  <div class="tu-page">

    <!-- ═══ Header ═══ -->
    <div class="tu-header">
      <div style="display: flex; flex-direction: column; gap: 4px;">
        <h1 style="font-size: 24px; font-weight: 700; color: #f3f6fc; line-height: 29px; margin: 0;">
          Token Usage
        </h1>
        <p style="font-size: 13px; font-weight: 400; color: #b8c0d4; line-height: 16px; margin: 0;">
          Monitor LLM token consumption, caching efficiency, and estimated costs.
        </p>
      </div>

      <!-- Period Filter Tabs -->
      <div class="tu-period-tabs">
        <button
          v-for="p in periods"
          :key="p.value"
          @click="period = p.value"
          :style="{
            height: '30px',
            padding: '0 14px',
            borderRadius: '8px',
            fontSize: '12px',
            fontWeight: '600',
            fontFamily: 'Inter, sans-serif',
            cursor: 'pointer',
            border: 'none',
            background: period === p.value ? '#3b82f6' : 'transparent',
            color: period === p.value ? '#ffffff' : '#8b8fa3',
            transition: 'all 0.2s ease',
          }"
        >
          {{ p.label }}
        </button>
      </div>
    </div>

    <!-- ═══ Metric Cards ═══ -->
    <div class="tu-metric-cards">
      <div
        v-for="card in cards"
        :key="card.label"
        style="flex: 1; background: #111318; border: 1px solid #1e2030; border-radius: 12px; padding: 16px; display: flex; flex-direction: column; gap: 6px; transition: border-color 0.2s ease;"
        class="metric-card"
      >
        <div style="display: flex; align-items: center; gap: 6px;">
          <span style="font-size: 14px;">{{ card.icon }}</span>
          <span style="font-size: 12px; font-weight: 500; color: #b8c0d4; line-height: 15px;">
            {{ card.label }}
          </span>
        </div>
        <span style="font-size: 28px; font-weight: 700; color: #f3f6fc; line-height: 34px;">
          {{ isLoading ? '...' : card.value }}
        </span>
        <div style="display: flex; align-items: center; gap: 6px; margin-top: auto;">
          <span
            :style="{
              width: '6px', height: '6px', borderRadius: '50%',
              background: card.color, flexShrink: 0,
            }"
          ></span>
          <span style="font-size: 11px; font-weight: 500; color: #8b8fa3; line-height: 13px;">
            {{ card.sub }}
          </span>
        </div>
      </div>
    </div>

    <!-- ═══ Charts Row ═══ -->
    <div class="tu-charts-row">

      <!-- Bar Chart: Agent Token Distribution -->
      <div style="flex: 2; background: #111318; border: 1px solid #1e2030; border-radius: 12px; padding: 20px; display: flex; flex-direction: column; gap: 16px;">
        <div style="display: flex; justify-content: space-between; align-items: center;">
          <span style="font-size: 14px; font-weight: 600; color: #f3f6fc;">Agent Token Distribution</span>
          <!-- Legend -->
          <div style="display: flex; gap: 12px;">
            <div v-for="item in [
              { color: '#3b82f6', label: 'Input' },
              { color: '#54b6ff', label: 'Output' },
              { color: '#22c55e', label: 'Cached' },
              { color: '#ffb547', label: 'Reasoning' },
            ]" :key="item.label" style="display: flex; align-items: center; gap: 4px;">
              <span :style="{ width: '8px', height: '8px', borderRadius: '2px', background: item.color }"></span>
              <span style="font-size: 11px; color: #8b8fa3;">{{ item.label }}</span>
            </div>
          </div>
        </div>

        <!-- Empty state -->
        <div v-if="!agentsList.length && !isLoading" style="display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 40px 0; color: #64748b;">
          <span style="font-size: 32px; margin-bottom: 8px;">📉</span>
          <span style="font-size: 13px;">No token data for this period</span>
        </div>

        <!-- Bars -->
        <div v-else style="display: flex; flex-direction: column; gap: 10px;">
          <div
            v-for="agent in agentsList.slice(0, 10)"
            :key="agent.agent_name"
            style="display: flex; align-items: center; gap: 12px;"
          >
            <!-- Agent name -->
            <span style="width: 120px; font-size: 12px; font-weight: 500; color: #b8c0d4; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex-shrink: 0;">
              {{ agent.agent_name }}
            </span>

            <!-- Stacked bar -->
            <div style="flex: 1; height: 24px; background: #0a0d14; border-radius: 6px; overflow: hidden; display: flex;">
              <!-- Input (non-cached) -->
              <div
                :style="{
                  width: `${((agent.input_tokens - (agent.cached_tokens || 0)) / chartMax) * 100}%`,
                  background: '#3b82f6',
                  height: '100%',
                  transition: 'width 0.5s ease',
                  minWidth: agent.input_tokens > 0 ? '2px' : '0',
                }"
              ></div>
              <!-- Output -->
              <div
                :style="{
                  width: `${(agent.output_tokens / chartMax) * 100}%`,
                  background: '#54b6ff',
                  height: '100%',
                  transition: 'width 0.5s ease',
                  minWidth: agent.output_tokens > 0 ? '2px' : '0',
                }"
              ></div>
              <!-- Cached -->
              <div
                :style="{
                  width: `${((agent.cached_tokens || 0) / chartMax) * 100}%`,
                  background: '#22c55e',
                  height: '100%',
                  transition: 'width 0.5s ease',
                  minWidth: (agent.cached_tokens || 0) > 0 ? '2px' : '0',
                }"
              ></div>
            </div>

            <!-- Total count -->
            <span style="width: 60px; font-size: 11px; font-weight: 600; color: #f3f6fc; text-align: right; flex-shrink: 0;">
              {{ fmtTokens(agent.total_tokens) }}
            </span>
          </div>
        </div>
      </div>

      <!-- Model Breakdown -->
      <div style="flex: 1; background: #111318; border: 1px solid #1e2030; border-radius: 12px; padding: 20px; display: flex; flex-direction: column; gap: 12px;">
        <span style="font-size: 14px; font-weight: 600; color: #f3f6fc;">By Model</span>

        <div v-if="!modelsList.length && !isLoading" style="display: flex; align-items: center; justify-content: center; padding: 30px 0; color: #64748b; font-size: 13px;">
          No data
        </div>

        <div
          v-for="model in modelsList"
          :key="model.model"
          style="display: flex; flex-direction: column; gap: 4px;"
        >
          <div style="display: flex; justify-content: space-between; align-items: center;">
            <span style="font-size: 12px; font-weight: 500; color: #b8c0d4; max-width: 160px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
              {{ model.model }}
            </span>
            <span style="font-size: 11px; font-weight: 600; color: #f3f6fc;">
              {{ fmtCost(model.est_cost) }}
            </span>
          </div>
          <!-- Progress bar -->
          <div style="width: 100%; height: 6px; background: #0a0d14; border-radius: 3px; overflow: hidden;">
            <div
              :style="{
                width: `${modelsList.length ? (model.total_tokens / (modelsList[0]?.total_tokens || 1)) * 100 : 0}%`,
                height: '100%',
                background: 'linear-gradient(90deg, #3b82f6, #54b6ff)',
                borderRadius: '3px',
                transition: 'width 0.5s ease',
              }"
            ></div>
          </div>
          <div style="display: flex; justify-content: space-between;">
            <span style="font-size: 10px; color: #64748b;">{{ fmtTokens(model.total_tokens) }} tokens</span>
            <span style="font-size: 10px; color: #64748b;">{{ model.llm_calls }} calls</span>
          </div>
        </div>
      </div>
    </div>

    <!-- ═══ Agent Breakdown Table ═══ -->
    <div class="tu-agent-table">
      <!-- Header -->
      <div class="tu-agent-table-title">
        <span style="font-size: 14px; font-weight: 600; color: #f3f6fc;">Agent Breakdown</span>
        <span style="margin-left: auto; font-size: 11px; color: #8b8fa3;">
          {{ agentsList.length }} agent{{ agentsList.length !== 1 ? 's' : '' }}
        </span>
      </div>

      <!-- Table header -->
      <div class="tu-col-header">
        <span class="tu-col-agent">Agent</span>
        <span class="tu-col-input">Input</span>
        <span class="tu-col-output">Output</span>
        <span class="tu-col-total">Total</span>
        <span class="tu-col-cached">Cached</span>
        <span class="tu-col-cost">Cost</span>
      </div>

      <!-- Empty -->
      <div v-if="!agentsList.length && !isLoading" style="padding: 40px 20px; text-align: center; color: #64748b; font-size: 13px;">
        No token usage recorded for this period.
      </div>

      <!-- Rows -->
      <div
        v-for="agent in agentsList"
        :key="agent.agent_name"
        class="agent-row tu-agent-row"
      >
        <div class="tu-col-agent">
          <span style="font-size: 13px; font-weight: 500; color: #f3f6fc;">{{ agent.agent_name }}</span>
          <span class="tu-calls-badge">{{ agent.llm_calls }} calls</span>
        </div>
        <span class="tu-col-input" style="text-align: right;">{{ fmtTokens(agent.input_tokens) }}</span>
        <span class="tu-col-output" style="text-align: right;">{{ fmtTokens(agent.output_tokens) }}</span>
        <span class="tu-col-total" style="text-align: right; font-weight: 600; color: #f3f6fc;">{{ fmtTokens(agent.total_tokens) }}</span>
        <span class="tu-col-cached" style="text-align: right; color: #22c55e;">{{ fmtTokens(agent.cached_tokens || 0) }}</span>
        <span class="tu-col-cost" style="text-align: right; font-weight: 600; color: #ffb547;">{{ fmtCost(agent.est_cost) }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* ─── Desktop Base Styles ─────────────────────────── */
.tu-page {
  display: flex;
  flex-direction: column;
  gap: 24px;
  max-width: 1148px;
}

.tu-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
}

.tu-period-tabs {
  display: flex;
  gap: 4px;
  background: #111318;
  border: 1px solid #1e2030;
  border-radius: 10px;
  padding: 3px;
  flex-shrink: 0;
}

.tu-metric-cards {
  display: flex;
  gap: 16px;
}

.tu-charts-row {
  display: flex;
  gap: 16px;
}

.metric-card:hover {
  border-color: #2a3556 !important;
}

.agent-row:hover {
  background: rgba(59, 130, 246, 0.04);
}

/* ─── Agent Breakdown Table Base ──────────────────── */
.tu-agent-table {
  background: #111318;
  border: 1px solid #1e2030;
  border-radius: 12px;
  overflow: hidden;
}

.tu-agent-table-title {
  display: flex;
  align-items: center;
  padding: 14px 20px;
  border-bottom: 1px solid #1e2030;
}

/* Grid: header + rows share same column template */
.tu-col-header,
.tu-agent-row {
  display: grid;
  grid-template-columns: 1.5fr 1fr 1fr 1fr 1fr 0.8fr;
  gap: 8px;
  padding: 10px 20px;
}

.tu-col-header {
  border-bottom: 1px solid #1a1d2e;
}

.tu-agent-row {
  padding: 12px 20px;
  border-bottom: 1px solid rgba(26, 29, 46, 0.5);
  transition: background 0.15s ease;
  align-items: center;
}

/* Column header text */
.tu-col-header span {
  font-size: 11px;
  font-weight: 600;
  color: #64748b;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.tu-col-header .tu-col-input,
.tu-col-header .tu-col-output,
.tu-col-header .tu-col-total,
.tu-col-header .tu-col-cached,
.tu-col-header .tu-col-cost {
  text-align: right;
}

/* Data cell colors */
.tu-col-input,
.tu-col-output { font-size: 13px; color: #b8c0d4; }
.tu-col-total  { font-size: 13px; color: #f3f6fc; font-weight: 600; }
.tu-col-cached { font-size: 13px; }
.tu-col-cost   { font-size: 13px; }

/* Agent name cell */
.tu-col-agent {
  display: flex;
  align-items: center;
  gap: 8px;
}

.tu-calls-badge {
  font-size: 10px;
  font-weight: 500;
  color: #64748b;
  background: #1a1d2e;
  padding: 2px 6px;
  border-radius: 4px;
  white-space: nowrap;
}

/* ═══ RESPONSIVE — Mobile ≤768px ══════════════════════════════════ */
@media (max-width: 768px) {
  .tu-page {
    gap: 14px;
    padding-bottom: 80px;
  }

  /* Header: stack on mobile */
  .tu-header {
    flex-direction: column;
    gap: 12px;
    align-items: stretch;
  }

  /* Period tabs: full width horizontal scroll */
  .tu-period-tabs {
    width: 100%;
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
    scrollbar-width: none;
    justify-content: stretch;
  }
  .tu-period-tabs::-webkit-scrollbar { display: none; }
  .tu-period-tabs button {
    flex: 1;
    white-space: nowrap;
    min-width: 44px;
    height: 32px !important;
  }

  /* Metric cards: 3 columns grid on mobile */
  .tu-metric-cards {
    display: grid !important;
    grid-template-columns: repeat(3, 1fr);
    gap: 8px;
    overflow: visible;
  }
  .tu-metric-cards > div {
    flex: unset !important;
    min-width: unset !important;
    padding: 10px !important;
    gap: 4px !important;
    overflow: hidden;
  }
  /* Card label text */
  .tu-metric-cards > div > div:first-child span:last-child {
    font-size: 9px !important;
    line-height: 1.2 !important;
  }
  /* Card value */
  .tu-metric-cards > div > span {
    font-size: 18px !important;
    line-height: 1.2 !important;
    word-break: break-all;
  }
  /* Card sub text */
  .tu-metric-cards > div > div:last-child span:last-child {
    font-size: 9px !important;
  }

  /* Charts row: stack vertically */
  .tu-charts-row {
    flex-direction: column;
    gap: 12px;
  }
  .tu-charts-row > div {
    flex: unset !important;
    width: 100% !important;
  }

  /* Agent Breakdown: 3 cols (Agent, Total, Cost) */
  .tu-col-header,
  .tu-agent-row {
    grid-template-columns: 1fr 0.7fr 0.6fr;
    padding: 10px 14px;
    gap: 6px;
  }

  /* Hide Input, Output, Cached on mobile */
  .tu-col-input,
  .tu-col-output,
  .tu-col-cached {
    display: none !important;
  }

  /* Tighter font for mobile table */
  .tu-col-header span {
    font-size: 10px !important;
  }
  .tu-col-total,
  .tu-col-cost,
  .tu-col-agent span:first-child {
    font-size: 12px !important;
  }

  .tu-calls-badge {
    font-size: 9px;
    padding: 1px 5px;
  }

  .tu-agent-table-title {
    padding: 12px 14px;
  }
}
</style>


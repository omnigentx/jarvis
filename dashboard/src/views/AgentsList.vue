<script setup>
import { ref, computed, onMounted } from 'vue'
import { useAgentsStore } from '../stores/agents'
import AgentCard from '../components/AgentCard.vue'

const store = useAgentsStore()
const searchQuery = ref('')
const activeFilter = ref('all')

onMounted(() => { store.fetchAgents() })

const totalTokens = computed(() => {
  let sum = 0
  store.tokenMetrics.forEach(m => { sum += m.total_tokens || 0 })
  return store.formatTokenCount(sum)
})

const totalCost = computed(() => {
  let sum = 0
  store.tokenMetrics.forEach(m => { sum += m.est_cost || 0 })
  if (!sum) return '≈ estimated'
  return `≈ $${sum.toFixed(2)}`
})

const filteredAgents = computed(() => {
  let list = store.agentsList
  if (activeFilter.value !== 'all') {
    list = list.filter(a => (a.status || 'idle') === activeFilter.value)
  }
  if (searchQuery.value) {
    const q = searchQuery.value.toLowerCase()
    list = list.filter(a =>
      a.name.toLowerCase().includes(q) ||
      (a.model || '').toLowerCase().includes(q) ||
      (a.role || '').toLowerCase().includes(q)
    )
  }
  return list
})

const statusFilters = [
  { value: 'all', label: 'All' },
  { value: 'running', label: 'Running' },
  { value: 'idle', label: 'Idle' },
  { value: 'blocked', label: 'Blocked' },
  { value: 'error', label: 'Error' },
]

const runningNames = computed(() => {
  const r = store.agentsList.filter(a => a.status === 'running')
  return r.length ? r.map(a => a.name.replace(/ Agent|_agent/g, '')).join(' · ') : '—'
})

const blockedReason = computed(() => {
  const b = store.agentsList.filter(a => a.status === 'blocked' || a.status === 'error')
  return b.length ? b.map(a => a.name.replace(/ Agent/g, '')).join(' · ') : '—'
})

const statCards = computed(() => [
  { label: 'Total Agents', value: store.stats.total || 0, sub: 'Agile team', dotColor: '#8b8fa3' },
  { label: 'Running', value: store.stats.running || 0, sub: runningNames.value, dotColor: '#22c55e' },
  { label: 'Idle', value: store.stats.idle || 0, sub: 'Ready to wake', dotColor: '#64748b' },
  { label: 'Blocked', value: store.stats.error || 0, sub: blockedReason.value, dotColor: '#ef4444' },
])
</script>

<template>
  <div class="agents-page">

    <!-- ═══ Desktop: header with search + filter pills ═══ -->
    <div class="page-header">
      <div class="header-text">
        <h1 class="page-title">Agents</h1>
        <p class="page-subtitle">Real-time agent health, performance metrics, and intervention controls.</p>
      </div>

      <!-- Search + filter row -->
      <div class="search-filter-row">
        <input
          v-model="searchQuery"
          type="text"
          placeholder="Search agents by name, role, status..."
          class="search-input"
        />
        <!-- Mobile: filter chips scroll horizontally -->
        <div class="filter-chips">
          <button
            v-for="f in statusFilters"
            :key="f.value"
            @click="activeFilter = f.value"
            class="filter-chip"
            :class="{ active: activeFilter === f.value }"
          >
            {{ f.label }}
          </button>
        </div>
      </div>
    </div>

    <!-- ═══ Stat cards ═══ -->
    <!-- Desktop: 5 columns flex row | Mobile: 2×2 grid -->
    <div class="stat-grid">
      <div
        v-for="stat in statCards"
        :key="stat.label"
        class="stat-card"
      >
        <span class="stat-label">{{ stat.label }}</span>
        <span class="stat-value">{{ stat.value }}</span>
        <div class="stat-sub">
          <span class="stat-dot" :style="{ background: stat.dotColor }"></span>
          <span class="stat-sub-text">{{ stat.sub }}</span>
        </div>
      </div>
    </div>

    <!-- ═══ Agent list section ═══ -->
    <div class="agents-section">
      <div class="section-title-row">
        <h2 class="section-title">Active agents</h2>
        <span v-if="filteredAgents.length" class="section-count">{{ filteredAgents.length }} total</span>
      </div>

      <!-- Loading -->
      <div v-if="store.isLoading && !store.agentsList.length" class="state-center">
        <span class="loading-text animate-pulse-dot">Loading agents...</span>
      </div>

      <!-- Empty state -->
      <div v-else-if="!filteredAgents.length" class="empty-state">
        <div class="empty-icon">🤖</div>
        <div class="empty-title">No agents found</div>
        <div class="empty-sub">
          {{ searchQuery ? 'Try a different search term' : 'Agents will appear here when configured' }}
        </div>
      </div>

      <!-- Agent grid: 2 cols desktop, 1 col mobile (handled by AgentCard itself) -->
      <div v-else class="agent-grid">
        <AgentCard
          v-for="agent in filteredAgents"
          :key="agent.name"
          :agent="agent"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
.agents-page {
  display: flex;
  flex-direction: column;
  gap: 24px;
  max-width: 1148px;
}

/* ─── Page Header ─── */
.page-header {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.page-title {
  font-size: 24px;
  font-weight: 700;
  color: #f3f6fc;
  line-height: 29px;
  margin: 0;
}

.page-subtitle {
  font-size: 13px;
  font-weight: 400;
  color: #b8c0d4;
  line-height: 16px;
  margin: 0;
}

/* ─── Search + filter row ─── */
.search-filter-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding-top: 8px;
  flex-wrap: wrap;
}

.search-input {
  width: 380px;
  height: 36px;
  background: #111318;
  border: 1px solid #1e2030;
  border-radius: 8px;
  padding: 0 12px;
  font-size: 13px;
  color: #f3f6fc;
  outline: none;
  font-family: Inter, sans-serif;
  transition: border-color 0.2s;
  flex-shrink: 0;
}

.search-input:focus {
  border-color: #2a3556;
}

.filter-chips {
  display: flex;
  align-items: center;
  gap: 8px;
}

.filter-chip {
  height: 30px;
  padding: 0 16px;
  border-radius: 16px;
  font-size: 12px;
  font-weight: 600;
  font-family: Inter, sans-serif;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #111318;
  color: #b8c0d4;
  border: 1px solid #1e2030;
  transition: all 0.15s;
  white-space: nowrap;
}

.filter-chip.active {
  background: #3b82f6;
  color: #ffffff;
  border-color: transparent;
}

.filter-chip:hover:not(.active) {
  background: #1e2233;
  color: #f0f2f5;
}

/* ─── Stat Grid ─── */
.stat-grid {
  display: flex;
  gap: 16px;
}

.stat-card {
  flex: 1;
  height: 100px;
  background: #111318;
  border: 1px solid #1e2030;
  border-radius: 12px;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.stat-label {
  font-size: 12px;
  font-weight: 500;
  color: #b8c0d4;
  line-height: 15px;
}

.stat-value {
  font-size: 28px;
  font-weight: 700;
  color: #f3f6fc;
  line-height: 34px;
}

.stat-sub {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: auto;
}

.stat-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex-shrink: 0;
}

.stat-sub-text {
  font-size: 11px;
  font-weight: 600;
  color: #8b8fa3;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ─── Agents section ─── */
.agents-section {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.section-title-row {
  display: flex;
  align-items: center;
  gap: 10px;
}

.section-title {
  font-size: 16px;
  font-weight: 600;
  color: #f3f6fc;
  line-height: 19px;
  margin: 0;
}

.section-count {
  font-size: 13px;
  color: #8b8fa3;
}

.agent-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

/* ─── State helpers ─── */
.state-center {
  display: flex;
  justify-content: center;
  padding: 80px 0;
}

.loading-text {
  font-size: 13px;
  color: #64748b;
}

.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 80px 0;
  text-align: center;
}

.empty-icon { font-size: 40px; margin-bottom: 12px; }
.empty-title { font-size: 16px; font-weight: 500; color: #b8c0d4; margin-bottom: 4px; }
.empty-sub { font-size: 13px; color: #64748b; }

/* ═══ Mobile responsive ═══ */
@media (max-width: 767px) {
  .agents-page {
    gap: 0;           /* remove gaps — sections flow as full-width blocks */
    max-width: 100%;
  }

  /* Header text hidden on mobile (AppLayout shows page title in header) */
  .header-text { display: none; }

  .search-filter-row {
    flex-direction: column;
    align-items: stretch;
    gap: 0;
    padding-top: 0;
  }

  .search-input {
    width: 100%;
    border-radius: 0;
    border-left: none;
    border-right: none;
    height: 44px;
    font-size: 14px;
    border-top: 1px solid #1a1d2e;
    border-bottom: 1px solid #1a1d2e;
    background: #111318;
  }

  .filter-chips {
    overflow-x: auto;
    gap: 8px;
    padding: 10px 12px;
    scrollbar-width: none;
    background: #0c0e15;
    border-bottom: 1px solid #1a1d2e;
  }

  .filter-chips::-webkit-scrollbar { display: none; }

  .filter-chip {
    flex-shrink: 0;
    height: 28px;
    font-size: 12px;
    padding: 0 14px;
  }

  /* Stat grid: 2 × 2 */
  .stat-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0;
    border-bottom: 1px solid #1a1d2e;
  }

  .stat-card {
    height: auto;
    min-height: 72px;
    border-radius: 0;
    border: none;
    border-right: 1px solid #1a1d2e;
    border-bottom: 1px solid #1a1d2e;
    padding: 10px 12px;
    background: #0c0e15;
  }

  .stat-card:nth-child(2n) { border-right: none; }  /* remove right border on even cards */
  .stat-card:nth-last-child(-n+2) { border-bottom: none; }  /* remove bottom border on last row */

  .stat-value { font-size: 20px; line-height: 26px; }
  .stat-label { font-size: 11px; }

  /* Agents section */
  .agents-section {
    gap: 0;
    padding-top: 12px;
  }

  .section-title-row {
    padding: 0 14px 8px;
  }

  /* Single column list on mobile */
  .agent-grid {
    grid-template-columns: 1fr;
    gap: 0;
  }
}
</style>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useChatStore } from '../stores/chat'
import { useAgentsStore } from '../stores/agents'
import StatusBadge from './StatusBadge.vue'

const props = defineProps({
  agent: { type: Object, required: true },
})

const router = useRouter()
const chatStore = useChatStore()
const agentsStore = useAgentsStore()
const pauseLoading = ref(false)

function formatTimestamp(ts) {
  if (!ts) return ''
  const date = new Date(ts * 1000)
  const diff = (Date.now() - date) / 1000
  if (diff < 60) return 'just now'
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  return date.toLocaleDateString()
}

function getRoleLabel(agent) {
  if (agent.role) return agent.role
  const m = { static:'Core Agent', dynamic:'Dynamic Agent', team:'Team Lead', spawn:'Spawned Agent', team_member:'Team Member' }
  return m[agent.type] || 'Agent'
}

function handleIntervene(e) {
  e.preventDefault()
  chatStore.setActiveAgent(props.agent.name)
  router.push('/chat')
}

function handleViewLogs(e) {
  e.preventDefault()
  router.push(`/agents/${props.agent.name}?tab=activity`)
}

async function handlePauseToggle(e) {
  e.preventDefault()
  if (pauseLoading.value) return
  // Disable during the transitional pausing/resuming states — the agent is
  // mid-transition, an extra click would either no-op or fire a contradictory
  // request before the first one's hook completes.
  if (props.agent.status === 'pausing' || props.agent.status === 'resuming') return
  pauseLoading.value = true
  try {
    if (props.agent.status === 'paused') {
      await agentsStore.resumeAgent(props.agent.name)
    } else {
      await agentsStore.pauseAgent(props.agent.name)
    }
  } catch (err) {
    console.error('[AgentCard] Pause/resume failed:', err)
  } finally {
    pauseLoading.value = false
  }
}

const statusAccent = {
  running: '#10b981',
  error: '#ef4444',
  blocked: '#f59e0b',
  // Same amber as paused so the accent doesn't flicker between
  // pausing→paused or paused→resuming (low-contrast UX).
  pausing: '#f59e0b',
  paused: '#f59e0b',
  resuming: '#10b981',
}
</script>

<template>
  <!-- Desktop: vertical card. Mobile: horizontal row with accent bar -->
  <RouterLink
    :to="`/agents/${agent.name}`"
    class="agent-card"
    :class="`status-${agent.status || 'idle'}`"
  >
    <!-- Mobile only: left accent bar -->
    <span class="accent-bar" :style="{ background: statusAccent[agent.status] || 'transparent' }"></span>

    <!-- Mobile: avatar circle -->
    <div class="card-avatar" :style="{ background: agentsStore.getAgentColor?.(agent.name) || '#2a3556' }">
      {{ (agent.name || 'A').charAt(0).toUpperCase() }}
    </div>

    <!-- Main content -->
    <div class="card-body">
      <!-- Row 1: Name + Status Badge -->
      <div class="card-title-row">
        <div class="card-name-group">
          <span class="card-name">{{ agent.name }}</span>
          <!-- Role label hidden on mobile to save space -->
          <span class="card-role">{{ getRoleLabel(agent) }}</span>
        </div>
        <StatusBadge :status="agent.status || 'idle'" />
      </div>

      <!-- Row 2: Model + last action (desktop only) -->
      <div class="card-meta desktop-only">
        <div class="card-metric">
          <span class="metric-label">Tokens</span>
          <span class="metric-value">{{ agent.tokenCount || '—' }}</span>
        </div>
        <div class="card-metric">
          <span class="metric-label">Last action</span>
          <span class="metric-value metric-truncate">
            {{ agent.lastAction?.message ? `${agent.lastAction.message} · ${formatTimestamp(agent.lastAction.timestamp)}` : '—' }}
          </span>
        </div>
        <div class="card-metric">
          <span class="metric-label">Progress</span>
          <span class="metric-value">
            {{ agent.currentTurn ? `Turn ${agent.currentTurn}/∞` : (agent.status === 'idle' ? 'Ready' : '—') }}
          </span>
        </div>
      </div>

      <!-- Mobile: compact model + desc row -->
      <div class="card-mobile-sub mobile-only">
        <span class="mobile-model">{{ agent.model || getRoleLabel(agent) }}</span>
        <span v-if="agent.lastAction?.message" class="mobile-action">{{ agent.lastAction.message }}</span>
      </div>

      <!-- Row 3: Action buttons (desktop) -->
      <div class="card-actions desktop-only">
        <button
          v-if="['running', 'paused', 'pausing', 'resuming'].includes(agent.status)"
          @click="handlePauseToggle"
          :disabled="pauseLoading || agent.status === 'pausing' || agent.status === 'resuming'"
          class="btn-pause"
          :class="{ 'is-paused': agent.status === 'paused', 'is-transition': ['pausing','resuming'].includes(agent.status) }"
          :title="
            agent.status === 'pausing' ? 'Pausing… (waiting for current step to finish)'
            : agent.status === 'resuming' ? 'Resuming…'
            : agent.status === 'paused' ? 'Resume agent'
            : 'Pause agent'
          "
        >
          {{ agent.status === 'paused' ? '▶' : '⏸' }}
        </button>
        <button @click="handleIntervene" class="btn-intervene">Intervene</button>
        <button @click="handleViewLogs" class="btn-secondary">View logs</button>
        <button @click.prevent="$router.push(`/token-usage?agent=${agent.name}`)" class="btn-secondary" title="View token usage">
          💰 Tokens
        </button>
      </div>
    </div>
  </RouterLink>
</template>

<style scoped>
/* ─── Base card (Desktop) ─── */
.agent-card {
  display: flex;
  flex-direction: column;
  gap: 12px;
  background: #111318;
  border: 1px solid #1e2030;
  border-radius: 12px;
  padding: 16px 20px;
  text-decoration: none;
  transition: filter 0.15s;
  position: relative;
  overflow: hidden;
}

.agent-card:hover { filter: brightness(1.1); }

/* Accent bar — hidden on desktop */
.accent-bar { display: none; }

/* Avatar — hidden on desktop */
.card-avatar { display: none; }

/* Card body fills card */
.card-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
  flex: 1;
  min-width: 0;
}

/* Title row */
.card-title-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 28px;
}

.card-name-group {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.card-name {
  font-size: 16px;
  font-weight: 600;
  color: #f3f6fc;
  line-height: 19px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.card-role {
  font-size: 12px;
  font-weight: 400;
  color: #64748b;
  white-space: nowrap;
}

/* Metrics row */
.card-meta {
  display: flex;
  align-items: center;
  gap: 32px;
  min-height: 36px;
}

.card-metric {
  display: flex;
  flex-direction: column;
  gap: 2px;
  width: 100px;
}

.metric-label {
  font-size: 11px;
  font-weight: 500;
  color: #64748b;
  line-height: 13px;
}

.metric-value {
  font-size: 14px;
  font-weight: 600;
  color: #f3f6fc;
  line-height: 17px;
}

.metric-truncate {
  font-size: 12px;
  font-weight: 400;
  color: #b8c0d4;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* Action buttons */
.card-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 32px;
}

.btn-pause {
  width: 36px;
  height: 30px;
  border-radius: 8px;
  font-size: 14px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 0.2s;
  background: rgba(245, 158, 11, 0.15);
  border: 1px solid rgba(245, 158, 11, 0.3);
  color: #f59e0b;
}

.btn-pause.is-paused {
  background: rgba(34, 197, 94, 0.15);
  border-color: rgba(34, 197, 94, 0.3);
  color: #22c55e;
}

.btn-pause:disabled { opacity: 0.5; cursor: wait; }

.btn-intervene {
  width: 110px; height: 30px;
  background: #3b82f6; border: none; border-radius: 8px;
  font-size: 12px; font-weight: 600; color: #fff;
  cursor: pointer; display: flex; align-items: center; justify-content: center;
}

.btn-secondary {
  height: 30px; padding: 0 12px;
  background: #111318; border: 1px solid #1e2030; border-radius: 8px;
  font-size: 12px; font-weight: 500; color: #b8c0d4;
  cursor: pointer; display: flex; align-items: center; justify-content: center;
  transition: all 0.2s;
}

.btn-secondary:hover { background: #1e2233; color: #f0f2f5; }

/* Desktop-only / mobile-only helpers */
.mobile-only { display: none; }
.desktop-only { display: flex; }

/* ═══ Mobile ═══ */
@media (max-width: 767px) {
  .agent-card {
    flex-direction: row;      /* horizontal row layout on mobile */
    align-items: center;
    gap: 0;
    border-radius: 0;         /* flush full-width rows */
    padding: 12px 14px 12px 0;
    border-left: none;
    border-right: none;
    border-top: none;
    border-bottom: 1px solid #1a1d2e;
  }

  /* Remove hover filter — use background change instead */
  .agent-card:active { background: #0f1117; }

  /* Left accent bar (3px colored strip) */
  .accent-bar {
    display: block;
    width: 3px;
    align-self: stretch;
    border-radius: 0;
    flex-shrink: 0;
    margin-right: 12px;
  }

  /* Show avatar */
  .card-avatar {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 40px;
    height: 40px;
    border-radius: 20px;
    font-size: 16px;
    font-weight: 700;
    color: #ffffff;
    flex-shrink: 0;
    margin-right: 12px;
  }

  /* Card body fills remaining width */
  .card-body {
    gap: 3px;
    flex: 1;
    min-width: 0;
  }

  /* Title row — smaller font on mobile */
  .card-title-row {
    min-height: unset;
    align-items: flex-start;
  }

  .card-name {
    font-size: 13px;
    line-height: 18px;
  }

  /* Hide role label on mobile */
  .card-role { display: none; }

  /* Show mobile sub-row */
  .mobile-only { display: flex; flex-direction: column; gap: 1px; }

  .mobile-model {
    font-size: 11px;
    color: #555872;
    line-height: 15px;
  }

  .mobile-action {
    font-size: 11px;
    color: #8b8fa3;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 200px;
  }

  /* Hide desktop-specific rows */
  .desktop-only { display: none !important; }
}
</style>

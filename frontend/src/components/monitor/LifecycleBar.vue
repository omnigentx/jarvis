<script setup>
/**
 * LifecycleBar — narrow live ticker showing recent lifecycle events.
 *
 * Reads from the agentsStore's recentEvents (capped at 50) and surfaces
 * only the lifecycle transitions a user cares about while watching the
 * monitor — started / paused / resumed / errored / completed.
 *
 * Visual: pulsing cyan dot + last 3 events, oldest fades out.
 */
import { computed } from 'vue'
import { useAgentsStore } from '../../stores/agents'

const store = useAgentsStore()

const LIFECYCLE_TYPES = new Set([
  'started', 'agent_paused', 'agent_resumed',
  'agent_pausing', 'agent_resuming',
  'error', 'result', 'agent_added', 'agent_removed',
])

const TYPE_LABEL = {
  started: 'started',
  agent_paused: 'paused',
  agent_resumed: 'resumed',
  agent_pausing: 'pausing',
  agent_resuming: 'resuming',
  error: 'errored',
  result: 'completed',
  agent_added: 'spawned',
  agent_removed: 'removed',
}

const TYPE_COLOR = {
  started: 'var(--success)',
  agent_paused: 'var(--warning)',
  agent_resumed: 'var(--success)',
  agent_pausing: 'var(--warning)',
  agent_resuming: 'var(--success)',
  error: 'var(--danger)',
  result: 'var(--info)',
  agent_added: 'var(--accent)',
  agent_removed: 'var(--text-muted)',
}

const items = computed(() => {
  // Newest first, take 5.
  const out = []
  for (const e of store.recentEvents || []) {
    if (LIFECYCLE_TYPES.has(e.event_type)) out.push(e)
    if (out.length >= 5) break
  }
  return out
})

function ts(t) {
  if (!t) return ''
  const ms = typeof t === 'number' ? t * 1000 : Date.parse(t)
  if (!ms || isNaN(ms)) return ''
  return new Date(ms).toLocaleTimeString([], { hour12: false })
}
</script>

<template>
  <div class="lifecycle-bar" aria-label="Lifecycle events">
    <span class="lc-header">
      <span class="lc-pulse" />
      <span class="lc-label">LIFECYCLE</span>
    </span>
    <div v-if="!items.length" class="lc-empty">no recent events</div>
    <div v-else class="lc-ticker">
      <span
        v-for="(e, i) in items"
        :key="i"
        class="lc-item"
        :style="{ opacity: 1 - i * 0.15 }"
      >
        <span class="lc-time">{{ ts(e.timestamp) }}</span>
        <span class="lc-agent">{{ e.agent_name }}</span>
        <span class="lc-type" :style="{ color: TYPE_COLOR[e.event_type] || 'var(--text-muted)' }">
          {{ TYPE_LABEL[e.event_type] || e.event_type }}
        </span>
      </span>
    </div>
  </div>
</template>

<style scoped>
.lifecycle-bar {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 6px 14px;
  background: var(--bg-1);
  border: 1px solid var(--border);
  border-radius: var(--r-sm);
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-muted);
  overflow: hidden;
  min-height: 28px;
}

.lc-header {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}
.lc-pulse {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 8px var(--accent);
  animation: lcPulse 1.4s ease-in-out infinite;
}
@keyframes lcPulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50%      { opacity: 0.45; transform: scale(1.3); }
}
.lc-label {
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--accent);
  font-size: 9.5px;
}

.lc-empty {
  font-style: italic;
  color: var(--text-subtle);
}

.lc-ticker {
  display: flex;
  align-items: center;
  gap: 16px;
  overflow: hidden;
  white-space: nowrap;
}
.lc-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.lc-time { color: var(--text-subtle); }
.lc-agent { color: var(--text-dim); }
.lc-type { font-weight: 500; }

@media (max-width: 767px) {
  .lifecycle-bar { font-size: 10px; padding: 4px 10px; gap: 10px; }
  .lc-ticker { gap: 10px; }
  /* Hide older items so newest stays visible on narrow screens. */
  .lc-ticker .lc-item:nth-child(n+3) { display: none; }
}
</style>

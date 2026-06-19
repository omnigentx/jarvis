<script setup>
/**
 * StatusBadge — agent run-state pill (Running / Idle / Paused / …).
 *
 * Props (preserved):
 *   status — agent state key
 *   size   — kept for API parity; visual is single-size for now
 *
 * Visual: token-driven role colours, pulsing dot for transitional
 * states (pausing/resuming/spawning) and live states (running).
 */
import { computed } from 'vue'
import { useLang } from '../composables/useLang'

const { t } = useLang()

defineProps({
  status: { type: String, default: 'idle' },
  size: { type: String, default: 'md' },
})

// Status → (role-class, label, pulse). Aligned with the canonical
// palette in `agentMeta.js::statusColor` and the `--status-*` tokens
// in `tokens.css`. Roles map 1:1 to `.sb-pill--*` CSS classes below.
const statusConfig = computed(() => ({
  running:   { label: t('statusBadge.running'),   role: 'success', pulse: true },
  completed: { label: t('statusBadge.completed'), role: 'success', pulse: false },
  resuming:  { label: t('statusBadge.resuming'),  role: 'success', pulse: true },
  pausing:   { label: t('statusBadge.pausing'),   role: 'warning', pulse: true },
  paused:    { label: t('statusBadge.paused'),    role: 'paused',  pulse: false },
  thinking:  { label: t('statusBadge.thinking'),  role: 'info',    pulse: true },
  spawning:  { label: t('statusBadge.spawning'),  role: 'info',    pulse: true },
  starting:  { label: t('statusBadge.starting'),  role: 'info',    pulse: true },
  idle:      { label: t('statusBadge.idle'),      role: 'muted',   pulse: false },
  blocked:   { label: t('statusBadge.blocked'),   role: 'danger',  pulse: false },
  error:     { label: t('statusBadge.error'),     role: 'danger',  pulse: false },
}))
</script>

<template>
  <span
    class="sb-pill"
    :class="`sb-pill--${statusConfig[status]?.role || 'muted'}`"
  >
    <span
      class="sb-pill__dot"
      :class="statusConfig[status]?.pulse ? 'pulse-dot' : ''"
    ></span>
    <span class="sb-pill__label">{{ statusConfig[status]?.label || status }}</span>
  </span>
</template>

<style scoped>
.sb-pill {
  display: inline-flex;
  align-items: center;
  flex-shrink: 0;
  height: 24px;
  padding: 0 8px;
  gap: 6px;
  border-radius: 12px;
  font-family: var(--font-body);
  font-size: 11px;
  font-weight: 600;
  line-height: 13px;
  letter-spacing: 0.02em;
}

.sb-pill__dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
  flex-shrink: 0;
}

.sb-pill--success { background: var(--success-bg); color: var(--success); }
.sb-pill--warning { background: var(--warning-bg); color: var(--warning); }
.sb-pill--danger  { background: var(--danger-bg);  color: var(--danger);  }
.sb-pill--info    { background: var(--info-bg);    color: var(--info);    }
.sb-pill--paused  { background: var(--paused-bg);  color: var(--status-paused); }
.sb-pill--muted   { background: var(--bg-3);       color: var(--text-muted); }

/* Pulse animation — re-declared scoped so we don't depend on .jv
   ancestor. ``pulse-dot`` class exists in tokens.css but is scoped
   under .jv, which not every consumer (e.g. table rows in TeamMonitor)
   wraps. */
@keyframes sbPulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50%      { opacity: .55; transform: scale(1.15); }
}
.sb-pill__dot.pulse-dot {
  animation: sbPulse 1.4s ease-in-out infinite;
}
</style>

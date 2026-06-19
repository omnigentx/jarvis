<script setup>
/**
 * AgentTreeRow — single row in the Tree view.
 *
 * Renders a role avatar, name, status badge, last-message timestamp and
 * a tiny sparkline of recent activity. Click → router-link to detail.
 *
 * Indentation/tree-line drawing is controlled by the parent — this row
 * cares only about its agent. Connection lines come in via `depth` +
 * `isLast` props (mirrors the JSX reference).
 */
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { avatarGlyph, roleAvaClass, roleColorToken, statusColor } from './agentMeta.js'
import { useBreakpoint } from '../../composables/useBreakpoint'
import { useLang } from '../../composables/useLang'

const { t } = useLang()

const props = defineProps({
  agent: { type: Object, required: true },
  depth: { type: Number, default: 0 },
  // last sibling at this depth? draws an L-shaped joint instead of a T.
  isLast: { type: Boolean, default: true },
  // Boolean[] of length depth: for each ancestor level, whether a
  // vertical line should still be drawn (i.e. that ancestor was NOT last).
  parentLines: { type: Array, default: () => [] },
  isOrchestrator: { type: Boolean, default: false },
  // Whether this row has children that can be folded under it.
  hasChildren: { type: Boolean, default: false },
  // Whether the children are currently hidden.
  isCollapsed: { type: Boolean, default: false },
  // Optional sparkline data (last N values). Defaults to a flat row.
  sparkline: { type: Array, default: () => [] },
})
const emit = defineEmits(['toggle', 'chat', 'pause-toggle', 'delete'])
function onToggle(e) {
  e.preventDefault()
  e.stopPropagation()
  emit('toggle')
}

// Per-row actions emit upward — the parent owns API calls + confirm
// modals + toasts because those depend on the surrounding store/router
// context. ``.stop.prevent`` on the button @click is what keeps the
// row's <a> navigation from firing alongside the action.
function rowAction(name, e) {
  e.preventDefault()
  e.stopPropagation()
  emit(name, props.agent)
}

// Pause is a no-op on already-paused agents (the backend would return
// 409 on resume if it's pause-locked by an approval; we let it through
// and surface the error in the parent rather than guessing here).
// Delete is forbidden by the backend for static agents (returns 403);
// disable client-side so the user doesn't click a button that's
// guaranteed to fail. Jarvis is_default doubles as a static check.
const canDelete = computed(() => {
  const t = (props.agent.type || '').toLowerCase()
  if (props.agent.is_default) return false
  // ``builtin`` agents are code-defined and rejected by DELETE /api/agents.
  return t !== 'builtin'
})
const isPaused = computed(() => (props.agent.status || '') === 'paused')

// Pause is only meaningful for agents that have an in-flight turn OR are
// already paused (resume case). For ``idle`` / ``completed`` / ``error`` /
// ``blocked`` agents there is no LLM call to cancel and no scheduled work
// to block — calling pause() would just mark the agent paused in memory
// without any visible effect, which confused users (issue 2026-05-27:
// "clicked pause on idle agent, backend reported success but nothing
// changed"). Enable for transitional + active states; the canonical
// status palette in agentMeta.js is the source of truth for the set.
const ACTIVE_STATUSES = new Set([
  'running', 'thinking', 'resuming', 'spawning', 'starting', 'pausing', 'paused',
])
const canPauseToggle = computed(() => ACTIVE_STATUSES.has(props.agent.status || ''))

const router = useRouter()

const { isMobile } = useBreakpoint()
// Tighten indent on mobile: nested team members at depth 2 ate 80px
// out of a 375px viewport with the desktop 24px step. 14px keeps the
// hierarchy visible without crushing the name column.
const indent = computed(() => props.depth * (isMobile.value ? 14 : 24))
const ROLE_TOKEN = computed(() => roleColorToken(props.agent))
const STATUS_C = computed(() => statusColor(props.agent.status))

// Build a tiny SVG polyline for the sparkline.
const sparkPoints = computed(() => {
  const data = props.sparkline.length ? props.sparkline : [1, 2, 3, 2, 4, 3, 5, 4]
  const w = 60, h = 16
  const max = Math.max(1, ...data)
  return data
    .map((v, i) => {
      const x = (i / Math.max(1, data.length - 1)) * w
      const y = h - (v / max) * h
      return `${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')
})

function formatTokens(t) {
  if (!t) return '—'
  // Already a formatted string ("12.4k") — pass through.
  if (typeof t === 'string') return t
  if (t >= 1_000_000) return (t / 1_000_000).toFixed(1) + 'M'
  if (t >= 1_000) return (t / 1_000).toFixed(1) + 'k'
  return String(t)
}

function timeAgo(ts) {
  if (!ts) return '—'
  const ms = typeof ts === 'number' ? ts * 1000 : Date.parse(ts)
  if (!ms || isNaN(ms)) return '—'
  const diff = (Date.now() - ms) / 1000
  if (diff < 60) return `${Math.max(1, Math.floor(diff))}s`
  if (diff < 3600) return `${Math.floor(diff / 60)}m`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h`
  return `${Math.floor(diff / 86400)}d`
}

function onClick(e) {
  e.preventDefault()
  router.push(`/agents/${encodeURIComponent(props.agent.name)}`)
}
</script>

<template>
  <a
    class="tree-row"
    :class="{ 'tree-row-orchestrator': isOrchestrator }"
    :href="`/agents/${encodeURIComponent(agent.name)}`"
    @click="onClick"
  >
    <!-- Tree lines column — width derived from depth -->
    <span class="tree-lines" :style="{ width: (indent + 32) + 'px' }">
      <span
        v-for="(draw, i) in parentLines"
        :key="i"
        class="tree-v-line"
        :style="{ left: (i * 24 + 12) + 'px', opacity: draw ? 0.6 : 0 }"
      />
      <template v-if="depth > 0">
        <span
          class="tree-v-line tree-elbow-v"
          :style="{
            left: ((depth - 1) * 24 + 12) + 'px',
            height: isLast ? '14px' : '100%',
          }"
        />
        <span
          class="tree-h-line"
          :style="{ left: ((depth - 1) * 24 + 12) + 'px' }"
        />
      </template>
    </span>

    <!-- Expand/collapse chevron (only on parent rows with children) -->
    <button
      v-if="hasChildren"
      type="button"
      class="tree-toggle"
      :aria-label="isCollapsed ? t('tree.expandTeam') : t('tree.collapseTeam')"
      :aria-expanded="!isCollapsed"
      @click="onToggle"
    >
      <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
        <path
          d="M3 2l4 3-4 3z"
          fill="currentColor"
          :style="{ transform: isCollapsed ? 'none' : 'rotate(90deg)', transformOrigin: '5px 5px', transition: 'transform 0.18s' }"
        />
      </svg>
    </button>
    <span v-else class="tree-toggle tree-toggle-spacer" aria-hidden="true" />

    <!-- Status dot -->
    <span class="status-dot" :style="{ background: STATUS_C }" :class="{ pulse: agent.status === 'running' }" />

    <!-- Avatar + name -->
    <span class="row-main">
      <span class="ava-circle" :class="`jv ${roleAvaClass(agent)}`" :style="{ borderColor: `var(${ROLE_TOKEN})` }">
        {{ avatarGlyph(agent) }}
      </span>
      <span class="row-name" :title="agent.name">{{ agent.name }}</span>
      <span v-if="agent.team_name" class="row-team">{{ agent.team_name }}</span>
    </span>

    <!-- Model -->
    <span class="row-mono row-model">{{ agent.model || '—' }}</span>

    <!-- Status label -->
    <span class="row-mono row-status" :style="{ color: STATUS_C }">{{ (agent.status || 'idle').toUpperCase() }}</span>

    <!-- Tokens -->
    <span class="row-mono row-tokens">{{ formatTokens(agent.tokenCount) }}</span>

    <!-- Last activity timestamp -->
    <span class="row-mono row-time">{{ timeAgo(agent.lastAction?.timestamp) }}</span>

    <!-- Sparkline -->
    <svg class="spark" viewBox="0 0 60 16" preserveAspectRatio="none" aria-hidden="true">
      <polyline
        :points="sparkPoints"
        fill="none"
        :stroke="agent.status === 'error' ? 'var(--danger)' : 'var(--success)'"
        stroke-width="1.25"
        stroke-linecap="round"
        stroke-linejoin="round"
      />
    </svg>

    <!-- Per-row actions — hover-reveal so the dense tree view stays
         readable when scanning. Buttons stop propagation so the row's
         navigation <a> doesn't fire. -->
    <span class="row-actions" @click.stop>
      <button
        type="button"
        class="row-action"
        :title="t('tree.chatWith', { name: agent.name })"
        :aria-label="t('tree.chatWith', { name: agent.name })"
        @click="rowAction('chat', $event)"
      >
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
          <path d="M2 4a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2H7l-3 3v-3H4a2 2 0 0 1-2-2z"
                stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" fill="none"/>
        </svg>
      </button>
      <button
        type="button"
        class="row-action"
        :disabled="!canPauseToggle"
        :title="canPauseToggle
          ? (isPaused ? t('tree.resume', { name: agent.name }) : t('tree.pause', { name: agent.name }))
          : t('tree.nothingToPause', { name: agent.name, status: (agent.status || 'idle').toLowerCase() })"
        :aria-label="isPaused ? t('tree.resume', { name: agent.name }) : t('tree.pause', { name: agent.name })"
        @click="rowAction('pause-toggle', $event)"
      >
        <!-- ▶ when paused (action = resume); ❚❚ otherwise -->
        <svg v-if="isPaused" width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
          <path d="M4 3l7 4-7 4z" fill="currentColor"/>
        </svg>
        <svg v-else width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
          <rect x="3.5" y="3" width="2.5" height="8" rx="0.5" fill="currentColor"/>
          <rect x="8" y="3" width="2.5" height="8" rx="0.5" fill="currentColor"/>
        </svg>
      </button>
      <button
        type="button"
        class="row-action row-action-danger"
        :title="canDelete ? t('tree.delete', { name: agent.name }) : t('tree.staticCannotDelete')"
        :aria-label="t('tree.delete', { name: agent.name })"
        :disabled="!canDelete"
        @click="rowAction('delete', $event)"
      >
        <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
          <polyline points="2,3.5 12,3.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>
          <path d="M3.5 3.5v8a1 1 0 0 0 1 1h5a1 1 0 0 0 1-1v-8m-4-1h2a1 1 0 0 1 1 1v0H5v0a1 1 0 0 1 1-1z"
                stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" fill="none"/>
        </svg>
      </button>
    </span>
  </a>
</template>

<style scoped>
.tree-row {
  position: relative;
  display: grid;
  grid-template-columns:
    /* tree-lines */ auto
    /* toggle */     16px
    /* dot */        14px
    /* main */       minmax(220px, 1fr)
    /* model */      130px
    /* status */     86px
    /* tokens */     60px
    /* time */       46px
    /* spark */      60px
    /* actions */    96px;
  align-items: center;
  gap: 10px;
  padding: 7px 18px;
  font-family: var(--font-body);
  font-size: 12.5px;
  border-bottom: 1px solid var(--border);
  color: var(--text);
  text-decoration: none;
  transition: background 0.12s var(--ease-out);
}
.tree-row:hover { background: var(--bg-2); }
.tree-row-orchestrator { background: rgba(99, 102, 241, 0.04); }

/* Tree connection lines */
.tree-lines {
  position: relative;
  height: 28px;
  display: block;
}
.tree-v-line {
  position: absolute;
  top: 0;
  bottom: 0;
  width: 1px;
  background: var(--border-strong);
}
.tree-elbow-v {
  top: 0;
}
.tree-h-line {
  position: absolute;
  top: 14px;
  width: 12px;
  height: 1px;
  background: var(--border-strong);
}

/* Expand/collapse chevron — reserves a fixed slot so rows without
   children keep the same horizontal alignment as rows with the toggle. */
.tree-toggle {
  width: 16px;
  height: 16px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  border: 0;
  border-radius: 4px;
  color: var(--text-muted);
  cursor: pointer;
  padding: 0;
  flex-shrink: 0;
}
.tree-toggle:hover {
  background: var(--bg-3);
  color: var(--text);
}
.tree-toggle-spacer {
  pointer-events: none;
  cursor: default;
  background: transparent !important;
}

/* Status dot */
.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
  justify-self: center;
}
.status-dot.pulse { animation: dotPulse 1.6s ease-in-out infinite; }
@keyframes dotPulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50%      { opacity: 0.5; transform: scale(1.25); }
}

/* Main: avatar + name + team pill */
.row-main {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.ava-circle {
  width: 22px;
  height: 22px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-family: var(--font-mono);
  font-size: 9.5px;
  font-weight: 700;
  letter-spacing: 0.04em;
  flex-shrink: 0;
  border: 1.5px solid var(--border-strong);
  background: var(--bg-3);
  color: var(--text);
}
/* role-tinted variants — re-declared here without the .jv scope so
   they apply inside this scoped component. */
.ava-jarvis { background: linear-gradient(135deg, var(--primary), var(--accent)); color: white; border-color: transparent; }
.ava-pm  { background: var(--role-pm);  color: #0B0D12; }
.ava-sa  { background: var(--role-sa);  color: #0B0D12; }
.ava-ba  { background: var(--role-ba);  color: #0B0D12; }
.ava-dev { background: var(--role-dev); color: #0B0D12; }
.ava-qe  { background: var(--role-qe);  color: #0B0D12; }
.ava-des { background: var(--role-des); color: #0B0D12; }
.ava-dso { background: var(--role-dso); color: #0B0D12; }

.row-name {
  font-weight: 500;
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  min-width: 0;
}
.tree-row-orchestrator .row-name { font-weight: 600; }
.row-team {
  flex-shrink: 0;
  padding: 0 6px;
  height: 16px;
  border-radius: 4px;
  background: var(--primary-bg);
  color: var(--primary-hover);
  font-family: var(--font-mono);
  font-size: 9.5px;
  letter-spacing: 0.04em;
  border: 1px solid var(--primary-bg-strong);
  display: inline-flex;
  align-items: center;
  white-space: nowrap;
  max-width: 130px;
  overflow: hidden;
  text-overflow: ellipsis;
}

.row-mono {
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
}
.row-model {
  font-size: 11px;
  color: var(--text-dim);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.row-status {
  font-size: 10px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.row-tokens {
  font-size: 12px;
  text-align: right;
}
.row-time {
  font-size: 11px;
  color: var(--text-dim);
  text-align: right;
}

.spark {
  width: 60px;
  height: 16px;
  justify-self: end;
}

/* Per-row actions: hover-reveal so the dense tree stays readable when
   scanning. Buttons reserve their slot in the grid so the row layout
   doesn't reflow on hover (a width: 0 → auto would shift adjacent
   columns and trigger paint thrash on every hover). */
.row-actions {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  opacity: 0;
  transition: opacity 0.12s var(--ease-out);
  justify-self: end;
}
.tree-row:hover .row-actions,
.tree-row:focus-within .row-actions { opacity: 1; }
.row-action {
  width: 26px;
  height: 26px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  border: 1px solid transparent;
  border-radius: 6px;
  color: var(--text-muted);
  cursor: pointer;
  padding: 0;
  transition: background 0.12s, color 0.12s, border-color 0.12s;
}
.row-action:hover:not(:disabled) {
  background: var(--bg-3);
  color: var(--text);
  border-color: var(--border-strong);
}
.row-action:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}
.row-action-danger:hover:not(:disabled) {
  background: var(--danger-bg);
  color: var(--danger);
  border-color: rgba(239, 68, 68, 0.30);
}

/* Tablet (768–1023px): keep status + time columns visible, drop the
   high-density ones (model name + tokens + spark) since the row gets
   cramped before that. Actions still hover-reveal here — pointer
   devices still apply. */
@media (max-width: 1023px) and (min-width: 768px) {
  .tree-row {
    /* 7 visible items: tree-lines, toggle, dot, main, status, time, actions */
    grid-template-columns: auto 16px 14px minmax(0, 1fr) 86px 46px 96px;
    padding: 8px 14px;
  }
  .row-model, .row-tokens, .spark { display: none; }
}

/* Phone (<768px): drop everything except the essentials — at 375px wide
   the row only fits: tree-lines | toggle | dot | name | actions. Status
   info is already conveyed by the colored dot, so the STATUS / LAST text
   columns are redundant on phone. Actions shrink + always-on (no hover
   on touch). Previous breakpoint (900px) declared 6 columns but rendered
   7 items, which is why the action row wrapped below the agent on phone
   — that was the user-reported "very ugly mobile" bug. */
@media (max-width: 767px) {
  .tree-row {
    /* 5 visible items: tree-lines, toggle, dot, main, actions */
    grid-template-columns: auto 16px 14px minmax(0, 1fr) auto;
    padding: 8px 12px;
    gap: 8px;
  }
  .row-model, .row-status, .row-tokens, .row-time, .spark { display: none; }
  .row-actions { opacity: 1; gap: 2px; }
  /* WCAG/iOS 40px tap target — was 30px which is below the floor and
     the row had three actions stacked, making mis-taps frequent. */
  .row-action { width: 40px; height: 40px; }
  .row-name { font-size: 13px; }
  /* Hide the team pill — its info is already conveyed by the parent
     team row above, and the 130px pill was stealing horizontal space
     from the agent name (truncating it to 2-3 chars on iPhone Mini). */
  .row-team { display: none; }
  .ava-circle { width: 24px; height: 24px; }
}
</style>

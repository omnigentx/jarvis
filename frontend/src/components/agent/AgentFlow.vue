<script setup>
/**
 * AgentFlow — Sankey-style horizontal flow.
 *
 * Lanes (left → right):
 *   1. Conductor (Jarvis)
 *   2. Core agents + spawned cards (direct workers)
 *   3. Team PMs (team orchestrators)
 *   4. Team members (managed by their PM)
 *
 * Edge brightness reflects whether the agent is active (running). Edge
 * thickness reflects token volume (capped by log-scale so a 100k vs 1k
 * difference doesn't draw a 100x thicker line).
 */
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { avatarGlyph, roleColorToken, statusColor, teamColor } from './agentMeta.js'
import { useLang } from '../../composables/useLang'

const { t } = useLang()

const props = defineProps({
  conductor: { type: Object, default: null },
  midNodes: { type: Array, default: () => [] }, // core + spawned (non-team)
  teams: { type: Array, default: () => [] },    // [{name, pm, members[]}]
})

const router = useRouter()

// Vertical Sankey: lanes stack TOP → BOTTOM so agents spread HORIZONTALLY
// across the (wide) screen instead of cramming into one tall column. The
// canvas width grows with the busiest lane, then the SVG scales to fit its
// container — so 30 agents shrink-to-fit rather than overlap.
const NODE_W = 170
const NODE_H = 52
const MEMBER_W = 160
const MEMBER_H = 44
const LANE_GAP = 24 // horizontal gap between sibling nodes in a lane
const laneStep = NODE_W + LANE_GAP
const memberStep = MEMBER_W + 14

// Y of each lane's node TOP edge (top → bottom).
const LANE_Y = { root: 44, mid: 176, pm: 336, leaf: 484 }

const hasTeams = computed(() => props.teams.length > 0)
// Collapse the empty team lanes (and their labels) when there are no teams,
// so the canvas isn't mostly blank space below the core agents.
const H = computed(() => (hasTeams.value ? 560 : LANE_Y.mid + NODE_H + 48))

const memberCount = computed(() =>
  props.teams.reduce(
    (s, t) => s + (t.members || []).filter((m) => m !== t.pm).length,
    0,
  ),
)

// Width tracks the busiest lane so siblings never overlap; the 1220 floor
// keeps small graphs from stretching edge-to-edge.
const W = computed(() => {
  const widest = Math.max(
    1,
    props.midNodes.length,
    props.teams.length,
    memberCount.value,
  )
  return Math.max(1220, widest * laneStep + 80)
})

const rootCx = computed(() => W.value / 2)

// Centre `count` nodes across the canvas; returns the CENTRE x of node `i`.
function centredX(count, i, step) {
  const start = W.value / 2 - ((count - 1) * step) / 2
  return start + i * step
}

const midNodesLaid = computed(() =>
  props.midNodes.map((agent, i) => ({
    agent,
    x: centredX(props.midNodes.length, i, laneStep), // centre x
    y: LANE_Y.mid, // top y
  })),
)

const teamsLaid = computed(() =>
  props.teams.map((team, i) => {
    const x = centredX(props.teams.length, i, laneStep)
    const mems = (team.members || []).filter((m) => m !== team.pm)
    return {
      team,
      pmX: x,
      pmY: LANE_Y.pm,
      members: mems.map((m, j, all) => ({
        agent: m,
        x: x - ((all.length - 1) * memberStep) / 2 + j * memberStep, // centre x
        y: LANE_Y.leaf,
      })),
    }
  }),
)

function edgeWeight(tokens) {
  // tokens is the human-formatted string or number. Use log-scale.
  let n = 1
  if (typeof tokens === 'number') n = tokens
  else if (typeof tokens === 'string') {
    const num = parseFloat(tokens) || 1
    if (/k/i.test(tokens)) n = num * 1000
    else if (/m/i.test(tokens)) n = num * 1_000_000
    else n = num
  }
  return Math.min(4, Math.max(1, Math.log10(Math.max(2, n)) - 1))
}

function isActive(agent) {
  return agent?.status === 'running' || agent?.status === 'thinking'
}

// SVG has no text-overflow:ellipsis; truncate long strings so they don't
// bleed past the node rect. Limits tuned for the 170/160-wide rects.
function truncate(s, n) {
  if (!s) return ''
  const str = String(s)
  return str.length > n ? str.slice(0, n - 1) + '…' : str
}

function bezier(x1, y1, x2, y2) {
  // Vertical flow: ease the control points along Y so edges curve top→bottom.
  const cy = y1 + (y2 - y1) * 0.5
  return `M ${x1} ${y1} C ${x1} ${cy}, ${x2} ${cy}, ${x2} ${y2}`
}

function handleClick(agent) {
  if (agent?.name) router.push(`/agents/${encodeURIComponent(agent.name)}`)
}

// ── Zoom & pan ───────────────────────────────────────────────────────────
// Same model as OrbitHero: zoom the content inside a fixed SVG stage and drag
// to pan, so a busy graph can be enlarged for readability instead of just
// shrinking to fit. Clamped so labels stay legible.
const ZOOM_MIN = 0.6
const ZOOM_MAX = 2.5
const zoom = ref(1)
const pan = ref({ x: 0, y: 0 })
const stageRef = ref(null)
const isDragging = ref(false)
let dragStart = null

function onWheel(e) {
  e.preventDefault()
  const delta = e.deltaY > 0 ? -0.1 : 0.1
  zoom.value = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, zoom.value + delta))
}
function zoomIn() { zoom.value = Math.min(ZOOM_MAX, zoom.value + 0.2) }
function zoomOut() { zoom.value = Math.max(ZOOM_MIN, zoom.value - 0.2) }
function zoomReset() { zoom.value = 1; pan.value = { x: 0, y: 0 } }

// Pointer px → SVG userspace: preserveAspectRatio meet ⇒ min(rectW/W, rectH/H).
function stageScale() {
  const rect = stageRef.value?.getBoundingClientRect()
  if (!rect || !rect.width || !rect.height) return 1
  return Math.min(rect.width / W.value, rect.height / H.value)
}
function onPointerDown(e) {
  if (e.button !== 0) return
  // Don't hijack clicks on a node or the zoom buttons.
  if (e.target.closest('.flow-node, .zoom-controls')) return
  isDragging.value = true
  dragStart = { mx: e.clientX, my: e.clientY, px: pan.value.x, py: pan.value.y }
  stageRef.value?.setPointerCapture?.(e.pointerId)
}
function onPointerMove(e) {
  if (!isDragging.value || !dragStart) return
  const s = stageScale() || 1
  pan.value = {
    x: dragStart.px + (e.clientX - dragStart.mx) / s,
    y: dragStart.py + (e.clientY - dragStart.my) / s,
  }
}
function onPointerUp(e) {
  if (!isDragging.value) return
  isDragging.value = false
  dragStart = null
  stageRef.value?.releasePointerCapture?.(e.pointerId)
}
const zoomTransform = computed(() =>
  `translate(${pan.value.x} ${pan.value.y}) translate(${W.value / 2} ${H.value / 2}) scale(${zoom.value}) translate(${-W.value / 2} ${-H.value / 2})`,
)
</script>

<template>
  <div class="flow-host">
    <div class="flow-stage">
    <svg
      ref="stageRef"
      :viewBox="`0 0 ${W} ${H}`"
      class="flow-svg"
      :class="{ 'is-dragging': isDragging }"
      preserveAspectRatio="xMidYMid meet"
      @wheel="onWheel"
      @pointerdown="onPointerDown"
      @pointermove="onPointerMove"
      @pointerup="onPointerUp"
      @pointerleave="onPointerUp"
    >
      <defs>
        <linearGradient id="flow-grad" x1="0%" y1="0%" x2="100%" y2="0%">
          <stop offset="0%" stop-color="#6366F1" stop-opacity="0.45" />
          <stop offset="100%" stop-color="#22D3EE" stop-opacity="0.7" />
        </linearGradient>
      </defs>

      <g :transform="zoomTransform">
      <!-- Lane labels — centred above each top→bottom lane. Team lanes are
           hidden when there are no teams so the canvas isn't half-empty. -->
      <text :x="rootCx" :y="LANE_Y.root - 14" class="lane-label">{{ t('flowLegend.laneOrchestrator') }}</text>
      <text :x="rootCx" :y="LANE_Y.mid - 14"  class="lane-label">{{ t('flowLegend.laneStaticCards') }}</text>
      <text v-if="hasTeams" :x="rootCx" :y="LANE_Y.pm - 14"   class="lane-label">{{ t('flowLegend.laneTeamPms') }}</text>
      <text v-if="hasTeams" :x="rootCx" :y="LANE_Y.leaf - 14" class="lane-label">{{ t('flowLegend.laneTeamMembers') }}</text>

      <!-- Edges: root → mid (conductor bottom-centre → mid top-centre) -->
      <path
        v-for="(node, i) in midNodesLaid" :key="`e-m-${i}`"
        :d="bezier(rootCx, LANE_Y.root + NODE_H, node.x, node.y)"
        fill="none"
        :stroke="isActive(node.agent) ? 'url(#flow-grad)' : 'var(--border-strong)'"
        :stroke-width="edgeWeight(node.agent.tokenCount)"
        :opacity="isActive(node.agent) ? 0.6 : 0.3"
      />

      <!-- Edges: root → team PMs -->
      <path
        v-for="(tl, i) in teamsLaid" :key="`e-pm-${i}`"
        :d="bezier(rootCx, LANE_Y.root + NODE_H, tl.pmX, tl.pmY)"
        fill="none"
        :stroke="isActive(tl.team.pm) ? 'url(#flow-grad)' : 'var(--border-strong)'"
        stroke-width="3"
        :opacity="isActive(tl.team.pm) ? 0.65 : 0.35"
      />

      <!-- Edges: PM → members -->
      <template v-for="(tl, i) in teamsLaid" :key="`e-mb-${i}`">
        <path
          v-for="(m, j) in tl.members" :key="`e-${i}-${j}`"
          :d="bezier(tl.pmX, tl.pmY + NODE_H, m.x, m.y)"
          fill="none"
          :stroke="isActive(m.agent) ? 'url(#flow-grad)' : 'var(--border-strong)'"
          stroke-width="1.5"
          :opacity="isActive(m.agent) ? 0.55 : 0.28"
        />
      </template>

      <!-- Conductor node -->
      <g
        v-if="conductor"
        :transform="`translate(${rootCx - NODE_W/2}, ${LANE_Y.root})`"
        class="flow-node flow-conductor"
        tabindex="0"
        role="button"
        @click="handleClick(conductor)"
        @keydown.enter="handleClick(conductor)"
      >
        <rect width="170" height="52" rx="8" />
        <circle cx="20" cy="26" r="11" fill="url(#flow-grad)" />
        <text x="20" y="30" class="conductor-glyph">J</text>
        <text x="40" y="22" class="node-name">{{ truncate(conductor.name, 18) }}</text>
        <text x="40" y="38" class="node-meta">{{ truncate((conductor.model || '—') + ' · ' + (conductor.tokenCount || '0'), 22) }}</text>
      </g>

      <!-- Mid nodes -->
      <g
        v-for="(node, i) in midNodesLaid" :key="`mid-${i}`"
        :transform="`translate(${node.x - NODE_W/2}, ${node.y})`"
        class="flow-node"
        tabindex="0"
        role="button"
        :style="{ '--bar': `var(${roleColorToken(node.agent)})` }"
        @click="handleClick(node.agent)"
        @keydown.enter="handleClick(node.agent)"
      >
        <rect class="node-rect" width="170" height="52" rx="8" />
        <rect class="node-bar" x="0" y="0" width="3" height="52" />
        <circle
          cx="158" cy="12" r="4"
          :style="{ fill: statusColor(node.agent.status) }"
          :class="{ 'status-pulse': isActive(node.agent) }"
        />
        <text x="14" y="22" class="node-name">{{ truncate(node.agent.name, 20) }}</text>
        <text x="14" y="38" class="node-meta">{{ truncate((node.agent.model || '—') + ' · ' + (node.agent.tokenCount || '0'), 24) }}</text>
      </g>

      <!-- Team PMs -->
      <g
        v-for="(tl, i) in teamsLaid" :key="`pm-${i}`"
        :transform="`translate(${tl.pmX - NODE_W/2}, ${tl.pmY})`"
        class="flow-node flow-team-pm"
        tabindex="0"
        role="button"
        :style="{ '--bar': teamColor(tl.team.name) }"
        @click="handleClick(tl.team.pm)"
        @keydown.enter="handleClick(tl.team.pm)"
      >
        <rect class="node-rect" width="170" height="52" rx="8" />
        <rect class="node-bar" x="0" y="0" width="3" height="52" />
        <circle
          cx="158" cy="12" r="4"
          :style="{ fill: statusColor(tl.team.pm?.status) }"
          :class="{ 'status-pulse': isActive(tl.team.pm) }"
        />
        <text x="14" y="22" class="node-name">{{ truncate(tl.team.pm?.name || tl.team.name, 20) }}</text>
        <text x="14" y="38" class="node-meta">{{ truncate('team · ' + tl.team.name, 24) }}</text>
      </g>

      <!-- Team members -->
      <template v-for="(tl, i) in teamsLaid" :key="`m-${i}`">
        <g
          v-for="(m, j) in tl.members" :key="`mm-${i}-${j}`"
          :transform="`translate(${m.x - MEMBER_W/2}, ${m.y})`"
          class="flow-node flow-member"
          tabindex="0"
          role="button"
          :style="{ '--bar': `var(${roleColorToken(m.agent)})` }"
          @click="handleClick(m.agent)"
          @keydown.enter="handleClick(m.agent)"
        >
          <rect class="node-rect" width="160" :height="MEMBER_H" rx="8" />
          <rect class="node-bar" x="0" y="0" width="3" :height="MEMBER_H" />
          <circle
            cx="148" cy="12" r="4"
            :style="{ fill: statusColor(m.agent.status) }"
            :class="{ 'status-pulse': isActive(m.agent) }"
          />
          <text x="14" y="20" class="node-name">{{ truncate(m.agent.name, 18) }}</text>
          <text x="14" y="34" class="node-meta">{{ truncate(m.agent.model || '—', 22) }}</text>
        </g>
      </template>
      </g>
    </svg>

      <div class="zoom-controls" :aria-label="t('flowLegend.zoomControls')">
        <button class="zoom-btn" type="button" @click="zoomOut" :disabled="zoom <= ZOOM_MIN" :title="t('flowLegend.zoomOut')">−</button>
        <button class="zoom-btn zoom-reset" type="button" @click="zoomReset" :title="t('flowLegend.zoomReset', { pct: Math.round(zoom * 100) })">{{ Math.round(zoom * 100) }}%</button>
        <button class="zoom-btn" type="button" @click="zoomIn" :disabled="zoom >= ZOOM_MAX" :title="t('flowLegend.zoomIn')">+</button>
      </div>
    </div>

    <div class="flow-legend">
      <span class="mono-label">{{ t('flowLegend.legend') }}</span>
      <span><span class="lk lk-builtin" />{{ t('flowLegend.builtin') }}</span>
      <span><span class="lk lk-card" />{{ t('flowLegend.card') }}</span>
      <span><span class="lk lk-pm" />{{ t('flowLegend.teamPm') }}</span>
      <span><span class="lk lk-member" />{{ t('flowLegend.teamMember') }}</span>
      <span class="legend-tip">{{ t('flowLegend.tip') }}</span>
    </div>
  </div>
</template>

<style scoped>
.flow-host {
  background: var(--bg-1);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.flow-stage {
  position: relative;
}
.flow-svg {
  /* Fixed-height viewport (like OrbitHero) so the stage is always a
     comfortable canvas; preserveAspectRatio="meet" fits + centres the
     content inside it. Letting height follow the viewBox aspect collapsed a
     wide single-row graph into a thin strip. */
  width: 100%;
  height: clamp(440px, 60vh, 660px);
  display: block;
  background: var(--bg-0);
  border-radius: var(--r-md);
  cursor: grab;
  touch-action: none; /* let pointer drag-pan work on touch without scrolling */
}
.flow-svg.is-dragging {
  cursor: grabbing;
}

.zoom-controls {
  position: absolute;
  right: 8px;
  bottom: 8px;
  display: flex;
  align-items: center;
  gap: 4px;
  background: var(--bg-2);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  padding: 4px;
  box-shadow: 0 4px 12px -6px rgba(0, 0, 0, 0.4);
}
.zoom-btn {
  min-width: 28px;
  height: 26px;
  padding: 0 8px;
  border: 0;
  background: transparent;
  color: var(--text-dim);
  font-family: var(--font-mono);
  font-size: 12px;
  border-radius: var(--r-sm);
  cursor: pointer;
  transition: background 0.15s var(--ease-out), color 0.15s var(--ease-out);
}
.zoom-btn:hover:not(:disabled) {
  background: var(--bg-3);
  color: var(--text);
}
.zoom-btn:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}
.zoom-reset {
  font-variant-numeric: tabular-nums;
}

.lane-label {
  font-family: var(--font-mono);
  font-size: 10px;
  fill: var(--text-subtle);
  letter-spacing: 0.16em;
  text-anchor: middle;
  text-transform: uppercase;
}

/* Nodes (svg <g>) */
.flow-node {
  cursor: pointer;
  transition: filter 0.15s var(--ease-out);
}
.flow-node:hover,
.flow-node:focus-visible {
  outline: none;
  filter: drop-shadow(0 0 6px var(--primary-glow));
}
.flow-node text { pointer-events: none; }

.node-rect {
  fill: var(--bg-2);
  stroke: var(--border-strong);
  stroke-width: 1;
}
.node-bar { fill: var(--bar, var(--border-strong)); }

.flow-conductor rect {
  fill: var(--bg-2);
  stroke: var(--primary);
  stroke-width: 1.5;
}
.flow-team-pm .node-rect {
  stroke: var(--primary-bg-strong);
}

.conductor-glyph {
  font-family: var(--font-display);
  font-size: 13px;
  font-weight: 700;
  fill: white;
  text-anchor: middle;
  dominant-baseline: middle;
}

.node-name {
  font-family: var(--font-body);
  font-size: 12px;
  font-weight: 500;
  fill: var(--text);
}
.node-meta {
  font-family: var(--font-mono);
  font-size: 9.5px;
  fill: var(--text-muted);
}

/* Legend */
.flow-legend {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 14px;
  padding: 8px 10px;
  font-size: 11px;
  color: var(--text-dim);
  background: var(--bg-2);
  border: 1px solid var(--border);
  border-radius: var(--r-sm);
}
.mono-label {
  font-family: var(--font-mono);
  font-size: 9.5px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--text-muted);
}
.flow-legend .lk {
  display: inline-block;
  width: 14px;
  height: 3px;
  margin-right: 6px;
  vertical-align: middle;
}
.lk-builtin { background: var(--text-muted); }
.lk-card    { background: var(--accent-warm); }
.lk-pm      { background: var(--primary-hover); }
.lk-member  { background: var(--accent); }
.legend-tip {
  margin-left: auto;
  font-family: var(--font-mono);
  color: var(--text-muted);
}

/* Status dot pulse for active states (running/thinking). Mirrors the
   .status-dot.pulse animation used in AgentTreeRow so both views read the
   same "live agent" cue. SVG <circle> accepts CSS transform-origin via
   the cx/cy attrs, but Safari needs the explicit transform-origin too. */
.status-pulse {
  transform-origin: center;
  animation: flowDotPulse 1.4s ease-in-out infinite;
}
@keyframes flowDotPulse {
  0%, 100% { opacity: 1; }
  50%      { opacity: 0.45; }
}
</style>

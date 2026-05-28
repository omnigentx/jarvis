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
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { avatarGlyph, roleColorToken, statusColor, teamColor } from './agentMeta.js'

const props = defineProps({
  conductor: { type: Object, default: null },
  midNodes: { type: Array, default: () => [] }, // core + spawned (non-team)
  teams: { type: Array, default: () => [] },    // [{name, pm, members[]}]
})

const router = useRouter()

const W = 1220
const H = 720
const COL_X = { root: 60, mid: 360, pm: 700, leaf: 1000 }
const NODE_W = 170
const NODE_H = 52
const MEMBER_H = 44
const MEMBER_GAP = 50

const rootY = computed(() => H / 2)

const midNodesLaid = computed(() => {
  const n = props.midNodes.length
  if (n === 0) return []
  const spacing = (H - 80) / Math.max(1, n)
  return props.midNodes.map((agent, i) => ({
    agent,
    x: COL_X.mid,
    y: 40 + spacing * i + spacing / 2,
  }))
})

const teamsLaid = computed(() => {
  const n = props.teams.length
  if (n === 0) return []
  const spacing = n > 1 ? (H - 160) / n : 0
  return props.teams.map((team, i) => {
    const y = n === 1 ? H / 2 : 80 + spacing * i + spacing / 2
    return {
      team,
      pmX: COL_X.pm,
      pmY: y,
      members: (team.members || [])
        .filter(m => m !== team.pm)
        .map((m, j, all) => ({
          agent: m,
          x: COL_X.leaf,
          y: y - ((all.length - 1) * MEMBER_GAP) / 2 + j * MEMBER_GAP,
        })),
    }
  })
})

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
  const cx = x1 + (x2 - x1) * 0.5
  return `M ${x1} ${y1} C ${cx} ${y1}, ${cx} ${y2}, ${x2} ${y2}`
}

function handleClick(agent) {
  if (agent?.name) router.push(`/agents/${encodeURIComponent(agent.name)}`)
}
</script>

<template>
  <div class="flow-host">
    <svg :viewBox="`0 0 ${W} ${H}`" class="flow-svg" preserveAspectRatio="xMidYMid meet">
      <defs>
        <linearGradient id="flow-grad" x1="0%" y1="0%" x2="100%" y2="0%">
          <stop offset="0%" stop-color="#6366F1" stop-opacity="0.45" />
          <stop offset="100%" stop-color="#22D3EE" stop-opacity="0.7" />
        </linearGradient>
      </defs>

      <!-- Lane labels -->
      <text :x="COL_X.root + NODE_W/2" y="22" class="lane-label">ORCHESTRATOR</text>
      <text :x="COL_X.mid + NODE_W/2"  y="22" class="lane-label">STATIC + CARDS</text>
      <text :x="COL_X.pm + NODE_W/2"   y="22" class="lane-label">TEAM PMs</text>
      <text :x="COL_X.leaf + 80"       y="22" class="lane-label">TEAM MEMBERS</text>

      <!-- Edges: root → mid -->
      <path
        v-for="(node, i) in midNodesLaid" :key="`e-m-${i}`"
        :d="bezier(COL_X.root + NODE_W, rootY, COL_X.mid, node.y)"
        fill="none"
        :stroke="isActive(node.agent) ? 'url(#flow-grad)' : 'var(--border-strong)'"
        :stroke-width="edgeWeight(node.agent.tokenCount)"
        :opacity="isActive(node.agent) ? 0.6 : 0.3"
      />

      <!-- Edges: root → team PMs -->
      <path
        v-for="(t, i) in teamsLaid" :key="`e-pm-${i}`"
        :d="bezier(COL_X.root + NODE_W, rootY, COL_X.pm, t.pmY)"
        fill="none"
        :stroke="isActive(t.team.pm) ? 'url(#flow-grad)' : 'var(--border-strong)'"
        stroke-width="3"
        :opacity="isActive(t.team.pm) ? 0.65 : 0.35"
      />

      <!-- Edges: PM → members -->
      <template v-for="(t, i) in teamsLaid" :key="`e-mb-${i}`">
        <path
          v-for="(m, j) in t.members" :key="`e-${i}-${j}`"
          :d="bezier(COL_X.pm + NODE_W, t.pmY, COL_X.leaf, m.y)"
          fill="none"
          :stroke="isActive(m.agent) ? 'url(#flow-grad)' : 'var(--border-strong)'"
          stroke-width="1.5"
          :opacity="isActive(m.agent) ? 0.55 : 0.28"
        />
      </template>

      <!-- Conductor node -->
      <g
        v-if="conductor"
        :transform="`translate(${COL_X.root}, ${rootY - NODE_H/2})`"
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
        :transform="`translate(${node.x}, ${node.y - NODE_H/2})`"
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
        v-for="(t, i) in teamsLaid" :key="`pm-${i}`"
        :transform="`translate(${t.pmX}, ${t.pmY - NODE_H/2})`"
        class="flow-node flow-team-pm"
        tabindex="0"
        role="button"
        :style="{ '--bar': teamColor(t.team.name) }"
        @click="handleClick(t.team.pm)"
        @keydown.enter="handleClick(t.team.pm)"
      >
        <rect class="node-rect" width="170" height="52" rx="8" />
        <rect class="node-bar" x="0" y="0" width="3" height="52" />
        <circle
          cx="158" cy="12" r="4"
          :style="{ fill: statusColor(t.team.pm?.status) }"
          :class="{ 'status-pulse': isActive(t.team.pm) }"
        />
        <text x="14" y="22" class="node-name">{{ truncate(t.team.pm?.name || t.team.name, 20) }}</text>
        <text x="14" y="38" class="node-meta">{{ truncate('team · ' + t.team.name, 24) }}</text>
      </g>

      <!-- Team members -->
      <template v-for="(t, i) in teamsLaid" :key="`m-${i}`">
        <g
          v-for="(m, j) in t.members" :key="`mm-${i}-${j}`"
          :transform="`translate(${m.x}, ${m.y - MEMBER_H/2})`"
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
    </svg>

    <div class="flow-legend">
      <span class="mono-label">LEGEND</span>
      <span><span class="lk lk-builtin" />builtin</span>
      <span><span class="lk lk-card" />card</span>
      <span><span class="lk lk-pm" />team PM</span>
      <span><span class="lk lk-member" />team member</span>
      <span class="legend-tip">edge thickness = token flow · brighter = active</span>
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
.flow-svg {
  width: 100%;
  height: auto;
  max-height: 720px;
  display: block;
  background: var(--bg-0);
  border-radius: var(--r-md);
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

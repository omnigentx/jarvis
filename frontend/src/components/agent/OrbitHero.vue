<script setup>
/**
 * OrbitHero — 3-tier orbit hero used at the top of AgentsList.
 *
 * - Center: Jarvis (conductor) with pulsing ring + "CONDUCTOR" label.
 * - Inner ring : built-in agents (Personal, IoT, Music, AudioReader, ...)
 * - Middle ring: spawned card agents (one-shot vs resumable)
 * - Outer ring : team templates (one node per spawned team)
 *
 * Clicking a node routes to /agents/<name> (or the team's PM for a team).
 * The ``activeRing`` prop dims inactive rings so the user's focus follows
 * the tab they picked on the tree below.
 */
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  avatarGlyph,
  roleColorToken,
  statusColor,
  teamColor,
} from './agentMeta.js'
import { useLang } from '../../composables/useLang'

const { t } = useLang()

const props = defineProps({
  conductor: { type: Object, default: null },
  core: { type: Array, default: () => [] },
  spawned: { type: Array, default: () => [] },
  teams: { type: Array, default: () => [] },
  // 'all' | 'core' | 'spawned' | 'teams'
  activeRing: { type: String, default: 'all' },
})

const router = useRouter()

const RADIUS_CORE = 110
const RADIUS_SPAWNED = 175
const RADIUS_TEAM = 240
const VIEW = 560 // svg viewBox

// Wheel-zoom around the center, drag-to-pan once zoomed. Clamped so labels
// never invert nor vanish.
const ZOOM_MIN = 0.6
const ZOOM_MAX = 2.5
const zoom = ref(1)
const pan = ref({ x: 0, y: 0 })
const stageRef = ref(null)
const isDragging = ref(false)
let dragStart = null   // { mx, my, px, py }
function onWheel(e) {
  e.preventDefault()
  const delta = e.deltaY > 0 ? -0.1 : 0.1
  zoom.value = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, zoom.value + delta))
}
function zoomIn()  { zoom.value = Math.min(ZOOM_MAX, zoom.value + 0.2) }
function zoomOut() { zoom.value = Math.max(ZOOM_MIN, zoom.value - 0.2) }
function zoomReset() { zoom.value = 1; pan.value = { x: 0, y: 0 } }

// Convert pointer pixels → SVG userspace units. SVG uses
// preserveAspectRatio="xMidYMid meet" so the scale is min(W,H) / VIEW.
function stageScale() {
  const rect = stageRef.value?.getBoundingClientRect()
  if (!rect || rect.width === 0 || rect.height === 0) return 1
  return Math.min(rect.width, rect.height) / VIEW
}
function onPointerDown(e) {
  // Only respond to primary button; ignore clicks on interactive nodes.
  if (e.button !== 0) return
  if (e.target.closest('.orbit-node, .team-node, .conductor-node, .zoom-controls')) return
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
  `translate(${pan.value.x} ${pan.value.y}) translate(${VIEW/2} ${VIEW/2}) scale(${zoom.value}) translate(${-VIEW/2} ${-VIEW/2})`
)

const coreNodes = computed(() => layoutRing(props.core, RADIUS_CORE))
const spawnedNodes = computed(() => layoutRing(props.spawned, RADIUS_SPAWNED))
const teamNodes = computed(() => layoutRing(props.teams, RADIUS_TEAM, true))

function layoutRing(items, r, isTeam = false) {
  const n = items.length
  if (n === 0) return []
  return items.map((item, i) => {
    // Rotate so the first node sits at the top.
    const angle = (-Math.PI / 2) + (i * 2 * Math.PI) / n
    return {
      item,
      x: VIEW / 2 + r * Math.cos(angle),
      y: VIEW / 2 + r * Math.sin(angle),
      isTeam,
    }
  })
}

function ringOpacity(key) {
  if (props.activeRing === 'all' || props.activeRing === key) return 1
  return 0.4
}

function shortName(name) {
  if (!name) return ''
  // Drop "[TAG]" suffix when a name like "Elliot [PM]" would overflow the
  // orbit label slot. Tag is already represented by the role color band.
  const clean = name.replace(/\s*\[[^\]]+\]\s*$/, '').replace(/Agent$/i, '')
  return clean.length > 12 ? clean.slice(0, 11) + '…' : clean
}

function handleClick(node) {
  if (node.isTeam) {
    // Team node — drill into PM if present, else first member.
    const target = node.item.pm || node.item.members?.[0]
    if (target?.name) router.push(`/agents/${encodeURIComponent(target.name)}`)
  } else if (node.item?.name) {
    router.push(`/agents/${encodeURIComponent(node.item.name)}`)
  }
}

function openConductor() {
  if (props.conductor?.name) {
    router.push(`/agents/${encodeURIComponent(props.conductor.name)}`)
  }
}
</script>

<template>
  <div class="orbit-hero" :data-ring="activeRing">
    <div
      ref="stageRef"
      class="orbit-stage"
      :class="{ 'is-dragging': isDragging }"
      @wheel="onWheel"
      @pointerdown="onPointerDown"
      @pointermove="onPointerMove"
      @pointerup="onPointerUp"
      @pointercancel="onPointerUp"
    >
      <svg :viewBox="`0 0 ${VIEW} ${VIEW}`" class="orbit-svg" preserveAspectRatio="xMidYMid meet">
      <defs>
        <linearGradient id="conductor-grad" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stop-color="#818CF8" />
          <stop offset="100%" stop-color="#22D3EE" />
        </linearGradient>
      </defs>

      <g :transform="zoomTransform">
      <!-- Ring guides -->
      <g class="ring-guides">
        <circle :cx="VIEW/2" :cy="VIEW/2" :r="RADIUS_CORE"    class="ring-guide" :style="{ opacity: ringOpacity('core')    }" />
        <circle :cx="VIEW/2" :cy="VIEW/2" :r="RADIUS_SPAWNED" class="ring-guide" :style="{ opacity: ringOpacity('spawned') }" />
        <circle :cx="VIEW/2" :cy="VIEW/2" :r="RADIUS_TEAM"    class="ring-guide" :style="{ opacity: ringOpacity('teams')   }" />
      </g>

      <!-- Connectors: thin radial lines from conductor to each ring node. -->
      <g class="connectors">
        <line
          v-for="(node, i) in coreNodes" :key="`lc-${i}`"
          :x1="VIEW/2" :y1="VIEW/2" :x2="node.x" :y2="node.y"
          class="connector" :style="{ opacity: ringOpacity('core') * 0.4 }"
        />
        <line
          v-for="(node, i) in spawnedNodes" :key="`ls-${i}`"
          :x1="VIEW/2" :y1="VIEW/2" :x2="node.x" :y2="node.y"
          class="connector" :style="{ opacity: ringOpacity('spawned') * 0.4 }"
        />
        <line
          v-for="(node, i) in teamNodes" :key="`lt-${i}`"
          :x1="VIEW/2" :y1="VIEW/2" :x2="node.x" :y2="node.y"
          class="connector" :style="{ opacity: ringOpacity('teams') * 0.4 }"
        />
      </g>

      <!-- Conductor (center) — clickable disc with CONDUCTOR + name
           stacked inside the circle. (Old layout had a redundant "J"
           glyph above the name; the name already identifies it.) -->
      <g
        v-if="conductor"
        :transform="`translate(${VIEW/2}, ${VIEW/2})`"
        class="conductor-node"
        tabindex="0"
        role="button"
        :aria-label="t('orbit.open', { name: conductor.name })"
        @click="openConductor"
        @keydown.enter="openConductor"
      >
        <circle r="62" class="conductor-glow" />
        <circle r="52" class="conductor-disc" />
        <text y="-6" class="conductor-label">{{ t('orbit.conductor') }}</text>
        <text y="14" class="conductor-name">{{ conductor.name }}</text>
      </g>

      <!-- Core (inner ring) -->
      <g class="ring-core" :style="{ opacity: ringOpacity('core') }">
        <g
          v-for="(node, i) in coreNodes"
          :key="`core-${i}`"
          :transform="`translate(${node.x}, ${node.y})`"
          class="orbit-node"
          tabindex="0"
          role="button"
          :aria-label="t('orbit.open', { name: node.item.name })"
          @click="handleClick(node)"
          @keydown.enter="handleClick(node)"
        >
          <circle r="22" class="node-disc" :style="{ stroke: `var(${roleColorToken(node.item)})` }" />
          <circle
            r="5" cx="14" cy="-14" class="node-status"
            :style="{ fill: statusColor(node.item.status) }"
          />
          <text class="node-glyph">{{ avatarGlyph(node.item) }}</text>
          <text y="38" class="node-name">{{ shortName(node.item.name) }}</text>
        </g>
      </g>

      <!-- Spawned (middle ring) -->
      <g class="ring-spawned" :style="{ opacity: ringOpacity('spawned') }">
        <g
          v-for="(node, i) in spawnedNodes"
          :key="`spawned-${i}`"
          :transform="`translate(${node.x}, ${node.y})`"
          class="orbit-node"
          tabindex="0"
          role="button"
          :aria-label="t('orbit.open', { name: node.item.name })"
          @click="handleClick(node)"
          @keydown.enter="handleClick(node)"
        >
          <circle r="20" class="node-disc" :style="{ stroke: `var(${roleColorToken(node.item)})` }" />
          <circle
            r="5" cx="13" cy="-13" class="node-status"
            :style="{ fill: statusColor(node.item.status) }"
          />
          <text class="node-glyph">{{ avatarGlyph(node.item) }}</text>
          <text y="36" class="node-name">{{ shortName(node.item.name) }}</text>
        </g>
      </g>

      <!-- Teams (outer ring) -->
      <g class="ring-teams" :style="{ opacity: ringOpacity('teams') }">
        <g
          v-for="(node, i) in teamNodes"
          :key="`team-${i}`"
          :transform="`translate(${node.x}, ${node.y})`"
          class="orbit-node team-node"
          tabindex="0"
          role="button"
          :aria-label="t('orbit.openTeam', { name: node.item.name })"
          @click="handleClick(node)"
          @keydown.enter="handleClick(node)"
        >
          <rect
            x="-34" y="-18" width="68" height="36" rx="8"
            class="team-pill"
            :style="{ stroke: teamColor(node.item.name) }"
          />
          <text class="team-glyph" :style="{ fill: teamColor(node.item.name) }">
            {{ node.item.members.length }}×
          </text>
          <text y="34" class="team-name">{{ shortName(node.item.name) }}</text>
        </g>
      </g>
      </g>
    </svg>

      <div class="zoom-controls" :aria-label="t('orbit.zoomControls')">
        <button class="zoom-btn" type="button" @click="zoomOut"  :disabled="zoom <= ZOOM_MIN" :title="t('orbit.zoomOut')">−</button>
        <button class="zoom-btn zoom-reset" type="button" @click="zoomReset" :title="t('orbit.zoomReset', { n: Math.round(zoom * 100) })">{{ Math.round(zoom * 100) }}%</button>
        <button class="zoom-btn" type="button" @click="zoomIn"   :disabled="zoom >= ZOOM_MAX" :title="t('orbit.zoomIn')">+</button>
      </div>
    </div>

    <!-- Sidebar legend — counts per ring -->
    <div class="orbit-legend">
      <div class="legend-row" :class="{ active: activeRing === 'core' || activeRing === 'all' }">
        <span class="legend-dot" style="background: var(--text-muted)"></span>
        <span class="legend-label">{{ t('orbit.legendCore') }}</span>
        <span class="legend-count">{{ core.length }}</span>
      </div>
      <div class="legend-row" :class="{ active: activeRing === 'spawned' || activeRing === 'all' }">
        <span class="legend-dot" style="background: var(--accent-warm)"></span>
        <span class="legend-label">{{ t('orbit.legendSpawned') }}</span>
        <span class="legend-count">{{ spawned.length }}</span>
      </div>
      <div class="legend-row" :class="{ active: activeRing === 'teams' || activeRing === 'all' }">
        <span class="legend-dot" style="background: var(--primary-hover)"></span>
        <span class="legend-label">{{ t('orbit.legendTeams') }}</span>
        <span class="legend-count">{{ teams.length }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.orbit-hero {
  position: relative;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 180px;
  gap: 24px;
  align-items: stretch;
  background: var(--bg-1);
  border: 1px solid var(--border);
  border-radius: var(--r-lg);
  padding: 18px 24px;
  /* Viewport-bounded height: tall enough that the conductor disc isn't
     squished, short enough that the tree below the hero remains
     above-the-fold on a typical laptop viewport (~900px tall). User
     can wheel-zoom for closer detail. */
  height: clamp(340px, 46vh, 460px);
}

.orbit-stage {
  position: relative;
  width: 100%;
  height: 100%;
  min-height: 0;
}

.orbit-svg {
  width: 100%;
  height: 100%;
  display: block;
  cursor: grab;
  touch-action: none;
  user-select: none;
}
.orbit-stage.is-dragging,
.orbit-stage.is-dragging .orbit-svg { cursor: grabbing; }

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
  box-shadow: 0 4px 12px -6px rgba(0,0,0,0.4);
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
.zoom-btn:hover:not(:disabled) { background: var(--bg-3); color: var(--text); }
.zoom-btn:disabled { opacity: 0.35; cursor: not-allowed; }
.zoom-reset { font-variant-numeric: tabular-nums; }

/* ring guides — dashed circles so the rings are visible even when sparse */
.ring-guide {
  fill: none;
  stroke: var(--border-strong);
  stroke-width: 1;
  stroke-dasharray: 2 6;
  transition: opacity 0.25s var(--ease-out);
}

.connector {
  stroke: var(--border-strong);
  stroke-width: 1;
  transition: opacity 0.25s var(--ease-out);
}

/* Conductor pulse — glow scaled with the larger disc. */
.conductor-node {
  cursor: pointer;
  transition: filter 0.2s var(--ease-out);
}
.conductor-node:hover,
.conductor-node:focus-visible {
  outline: none;
  filter: drop-shadow(0 0 12px var(--primary-glow));
}
.conductor-node:hover .conductor-disc { stroke-width: 3; }
.conductor-glow {
  fill: var(--primary-bg-strong);
  animation: conductorPulse 2.4s ease-in-out infinite;
}
@keyframes conductorPulse {
  0%, 100% { r: 60; opacity: 0.6; }
  50%      { r: 76; opacity: 0.2; }
}
.conductor-disc {
  fill: url(#conductor-grad);
  stroke: var(--primary);
  stroke-width: 2;
}
.conductor-node text { text-anchor: middle; pointer-events: none; }
.conductor-label {
  font-family: var(--font-mono);
  font-size: 8px;
  letter-spacing: 0.22em;
  /* On the saturated gradient disc, the dim purple hover tone read as
     bruised. White at 78% gives a calm eyebrow that still recedes
     behind the glyph + name. */
  fill: rgba(255, 255, 255, 0.78);
  text-transform: uppercase;
}
.conductor-name {
  font-family: var(--font-body);
  font-size: 11px;
  font-weight: 500;
  fill: white;
}

/* Orbit node — shared between core / spawned */
.orbit-node {
  cursor: pointer;
  transition: opacity 0.25s var(--ease-out);
}
.orbit-node:hover,
.orbit-node:focus-visible {
  outline: none;
  filter: drop-shadow(0 0 8px var(--primary-glow));
}
.orbit-node:hover .node-disc { stroke-width: 2.5; }

.node-disc {
  fill: var(--bg-2);
  stroke-width: 1.5;
}
.node-status {
  stroke: var(--bg-1);
  stroke-width: 2;
}
.orbit-node text { text-anchor: middle; pointer-events: none; }
.node-glyph {
  font-family: var(--font-mono);
  font-size: 11px;
  font-weight: 700;
  fill: var(--text);
  dominant-baseline: middle;
}
.node-name {
  font-family: var(--font-body);
  font-size: 10px;
  fill: var(--text-muted);
}

/* Team pill (outer ring) */
.team-pill {
  fill: var(--bg-2);
  stroke-width: 1.5;
}
.team-node:hover .team-pill { stroke-width: 2.5; }
.team-glyph {
  font-family: var(--font-mono);
  font-size: 13px;
  font-weight: 700;
  dominant-baseline: middle;
  text-anchor: middle;
  pointer-events: none;
}
.team-name {
  font-family: var(--font-body);
  font-size: 10px;
  fill: var(--text-muted);
  pointer-events: none;
  text-anchor: middle;
}

/* Legend column */
.orbit-legend {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 10px 0;
  align-self: stretch;
  justify-content: center;
  border-left: 1px dashed var(--border);
  padding-left: 18px;
}
.legend-row {
  display: flex;
  align-items: center;
  gap: 10px;
  font-family: var(--font-mono);
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--text-muted);
  transition: color 0.2s var(--ease-out);
}
.legend-row.active { color: var(--text); }
.legend-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.legend-label { flex: 1; }
.legend-count {
  font-variant-numeric: tabular-nums;
  color: var(--text);
  font-weight: 600;
}

@media (max-width: 767px) {
  .orbit-hero {
    grid-template-columns: 1fr;
    height: auto;
    padding: 12px;
  }
  /* Scale stage with viewport so labels at radius 240 stay inside the
     visible area instead of getting clipped against a fixed 320px box. */
  .orbit-stage { min-height: clamp(360px, 90vw, 480px); }
  .orbit-svg { height: clamp(360px, 90vw, 480px); }
  /* Move zoom controls to the top-right so they don't overlap the
     stacked legend row that now lives below the SVG on mobile. */
  .zoom-controls { bottom: auto; top: 8px; }
  .orbit-legend {
    flex-direction: row;
    border-left: none;
    border-top: 1px dashed var(--border);
    padding-left: 0;
    padding-top: 12px;
    justify-content: space-around;
  }
}
</style>

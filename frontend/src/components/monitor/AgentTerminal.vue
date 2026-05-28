<script setup>
/**
 * AgentTerminal — terminal-style live view of one agent's message_history.
 *
 * Reads turns from the ``useAgentTurns`` composable and renders them in
 * the order the agent saw them: user turns (with optional tool_results),
 * assistant turns (with optional tool_calls). Auto-scrolls to bottom
 * unless the user has scrolled up — a "↓ N new" pill jumps back.
 *
 * Props:
 *   agent:           AgentSummary  — { name, status, model, team_name, ... }
 *   turns:           Array<Turn>   — sorted ascending by turn_idx
 *   loading:         boolean       — true while initial fetch is in flight
 *   onFetchFull:     (turnIdx) => Promise<{message}|null>
 *   onPauseToggle:   () => void
 *   onDelete:        () => void  (optional — hidden if not provided)
 *   onInject:        ({text, files}) => Promise<{status, response?}>
 *
 * Emits navigation via the wrapping view; we just render + interact.
 */
import { ref, computed, nextTick, onMounted, watch } from 'vue'
import { formatTimestamp } from '../../composables/useActivityStream'
import {
  buildRenderRows,
  textContent,
  isTextTruncated,
  toolCallList,
  toolResultList,
  summarizeArgs,
  statusColor,
  statusLabel,
} from './agentTerminalUtils.js'

const props = defineProps({
  agent: { type: Object, required: true },
  turns: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
  onFetchFull: { type: Function, default: null },
  onPauseToggle: { type: Function, default: null },
  onDelete: { type: Function, default: null },
  onInject: { type: Function, default: null },
  onOpenFullscreen: { type: Function, default: null },
})

// ── Auto-scroll: stick to bottom unless user scrolled up ──
const scrollEl = ref(null)
const stickToBottom = ref(true)
const newCount = ref(0)

function isNearBottom(el) {
  if (!el) return true
  return el.scrollHeight - el.scrollTop - el.clientHeight < 40
}

function onScroll() {
  if (!scrollEl.value) return
  const near = isNearBottom(scrollEl.value)
  stickToBottom.value = near
  if (near) newCount.value = 0
}

function scrollToBottom() {
  if (!scrollEl.value) return
  scrollEl.value.scrollTop = scrollEl.value.scrollHeight
  stickToBottom.value = true
  newCount.value = 0
}

watch(
  () => props.turns.length,
  (cur, prev) => {
    const delta = cur - (prev || 0)
    if (delta <= 0) return
    if (stickToBottom.value) {
      nextTick(() => scrollToBottom())
    } else {
      newCount.value += delta
    }
  },
)

onMounted(() => nextTick(() => scrollToBottom()))

// statusColor / statusLabel live in agentTerminalUtils.js so they're
// covered by node:test (see agentTerminalUtils.test.js).

// ── Run separators: insert a marker whenever run_id changes ──
const renderRows = computed(() => buildRenderRows(props.turns))

function turnTime(turn) {
  return turn.ts ? formatTimestamp(turn.ts, { timeOnly: true }) : ''
}

// ── Expand truncated blocks: fetch full from backend on click ──
const expandedTurns = ref(new Set())
const fullCache = ref(new Map()) // turn_idx -> full message

async function expandTurn(turnIdx) {
  if (expandedTurns.value.has(turnIdx)) {
    expandedTurns.value.delete(turnIdx)
    expandedTurns.value = new Set(expandedTurns.value)
    return
  }
  if (!fullCache.value.has(turnIdx) && props.onFetchFull) {
    const full = await props.onFetchFull(turnIdx)
    if (full?.message) {
      fullCache.value.set(turnIdx, full.message)
      fullCache.value = new Map(fullCache.value)
    }
  }
  expandedTurns.value.add(turnIdx)
  expandedTurns.value = new Set(expandedTurns.value)
}

function fullTextContent(turnIdx) {
  const msg = fullCache.value.get(turnIdx)
  if (!msg) return ''
  const blocks = msg.content || []
  return blocks.filter(b => b?.type === 'text').map(b => b.text || '').join('\n')
}

function fullToolResultText(turnIdx, toolId) {
  const msg = fullCache.value.get(turnIdx)
  const res = msg?.tool_results?.[toolId]
  if (!res) return ''
  return (res.content || []).filter(b => b?.type === 'text').map(b => b.text || '').join('\n')
}

// ── Inject bar (collapsible) ──
const injectOpen = ref(false)
const injectText = ref('')
const injectFiles = ref([])
const injectBusy = ref(false)
const injectResult = ref(null)

function attachFile(kind = 'image') {
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = kind === 'audio' ? 'audio/*' : 'image/*'
  input.multiple = true
  input.onchange = (e) => {
    injectFiles.value = [...injectFiles.value, ...Array.from(e.target.files)]
  }
  input.click()
}

function removeFile(i) {
  injectFiles.value = injectFiles.value.filter((_, idx) => idx !== i)
}

async function submitInject() {
  if ((!injectText.value.trim() && !injectFiles.value.length) || injectBusy.value) return
  injectBusy.value = true
  injectResult.value = null
  try {
    const res = await props.onInject?.({ text: injectText.value.trim(), files: injectFiles.value })
    injectResult.value = res || null
    injectText.value = ''
    injectFiles.value = []
  } catch (e) {
    injectResult.value = { status: 'error', response: e?.message || String(e) }
  } finally {
    injectBusy.value = false
    setTimeout(() => { injectResult.value = null }, 8000)
  }
}

// ── Status badges for header ──
// Pause-cycle aware. Button shows ONLY when there's something to
// pause/resume — idle agents have nothing in flight so the button
// would be a no-op + visual noise. Transitional states (pausing /
// resuming) keep the button visible-but-disabled so the user sees
// their click was acknowledged.
const isRunning = computed(() => props.agent.status === 'running')
const isPaused = computed(() => props.agent.status === 'paused')
const isPauseTransitioning = computed(() =>
  props.agent.status === 'pausing' || props.agent.status === 'resuming'
)
const canTogglePause = computed(() =>
  ['running', 'paused', 'pausing', 'resuming'].includes(props.agent.status)
)
</script>

<template>
  <section
    class="agent-terminal"
    :class="{
      'is-running': isRunning,
      'is-paused': isPaused,
      'is-error': agent.status === 'error',
    }"
    :style="{ '--status-color': statusColor(agent.status) }"
  >
    <!-- Header -->
    <header class="term-header">
      <div class="term-title">
        <span class="status-dot" :class="{ pulse: isRunning }" />
        <span class="agent-name">{{ agent.name }}</span>
        <span v-if="agent.team_name" class="team-tag">{{ agent.team_name }}</span>
        <span class="agent-model">{{ agent.model || '—' }}</span>
      </div>
      <div class="term-controls">
        <span class="status-label">{{ statusLabel(agent.status) }}</span>
        <button
          v-if="onPauseToggle && canTogglePause"
          class="ctrl-btn"
          :class="{ 'is-paused': isPaused, 'is-transition': isPauseTransitioning }"
          :disabled="isPauseTransitioning"
          :title="
            agent.status === 'pausing'  ? 'Pausing… (waiting for current step to finish)' :
            agent.status === 'resuming' ? 'Resuming…' :
            isPaused                    ? 'Resume' :
            'Pause'
          "
          @click="onPauseToggle"
        >{{ isPaused ? '▶' : '⏸' }}</button>
        <button
          class="ctrl-btn"
          title="Toggle inject"
          :class="{ active: injectOpen }"
          @click="injectOpen = !injectOpen"
        >✎</button>
        <button
          v-if="onOpenFullscreen"
          class="ctrl-btn"
          title="Fullscreen"
          @click="onOpenFullscreen"
        >⤢</button>
        <button
          v-if="onDelete"
          class="ctrl-btn ctrl-danger"
          title="Remove agent"
          @click="onDelete"
        >🗑</button>
      </div>
    </header>

    <!-- Body -->
    <div class="term-body" ref="scrollEl" @scroll="onScroll">
      <div v-if="loading && !turns.length" class="term-empty">Loading history…</div>
      <div v-else-if="!turns.length" class="term-empty">No conversation yet.</div>

      <template v-else>
        <template v-for="row in renderRows" :key="row.key">
          <!-- Run separator -->
          <div v-if="row.kind === 'run'" class="run-divider">
            <span class="run-time">{{ row.ts ? formatTimestamp(row.ts, { timeOnly: true }) : '' }}</span>
            <span class="run-rule" />
            <span class="run-tag">run {{ String(row.run_id).slice(0, 8) }}</span>
            <span class="run-rule" />
          </div>

          <!-- Turn -->
          <article
            v-else
            class="turn-row"
            :class="{
              'role-user': row.turn.role === 'user',
              'role-assistant': row.turn.role === 'assistant',
            }"
          >
            <header class="turn-head">
              <span class="turn-glyph" :title="row.turn.role">{{
                row.turn.role === 'assistant' ? '◀' : '▷'
              }}</span>
              <span class="turn-role-label">{{
                row.turn.role === 'assistant' ? 'ASSISTANT' : 'USER'
              }}</span>
              <span class="turn-time">{{ turnTime(row.turn) }}</span>
            </header>
            <div class="turn-body">
              <!-- Text content (use full if expanded, else truncated) -->
              <pre
                v-if="textContent(row.turn) || expandedTurns.has(row.turn.turn_idx)"
                class="turn-text"
              >{{ expandedTurns.has(row.turn.turn_idx) && fullCache.get(row.turn.turn_idx)
                  ? fullTextContent(row.turn.turn_idx)
                  : textContent(row.turn) }}</pre>
              <button
                v-if="isTextTruncated(row.turn)"
                class="expand-btn"
                @click="expandTurn(row.turn.turn_idx)"
              >
                {{ expandedTurns.has(row.turn.turn_idx) ? 'Collapse' : '+ Show full' }}
              </button>

              <!-- Tool calls (assistant) -->
              <ul v-if="toolCallList(row.turn).length" class="tool-list">
                <li v-for="tc in toolCallList(row.turn)" :key="tc.id" class="tool-call">
                  <span class="tool-glyph">🔧</span>
                  <span class="tool-name">{{ tc.name }}</span>
                  <span v-if="summarizeArgs(tc.args)" class="tool-args">({{ summarizeArgs(tc.args) }})</span>
                </li>
              </ul>

              <!-- Tool results (user) -->
              <ul v-if="toolResultList(row.turn).length" class="tool-list">
                <li
                  v-for="tr in toolResultList(row.turn)"
                  :key="tr.id"
                  class="tool-result"
                  :class="{ 'tool-error': tr.isError }"
                >
                  <span class="tool-glyph">{{ tr.isError ? '✗' : '✓' }}</span>
                  <pre class="tool-result-text">{{
                    expandedTurns.has(row.turn.turn_idx) && fullCache.get(row.turn.turn_idx)
                      ? fullToolResultText(row.turn.turn_idx, tr.id)
                      : tr.text
                  }}</pre>
                  <button
                    v-if="tr.truncated"
                    class="expand-btn"
                    @click="expandTurn(row.turn.turn_idx)"
                  >
                    {{ expandedTurns.has(row.turn.turn_idx)
                        ? 'Collapse'
                        : `+ Show full (${Math.round(tr.fullSize / 1024)} KB)` }}
                  </button>
                </li>
              </ul>
            </div>
          </article>
        </template>
      </template>

      <!-- "↓ N new" pill — visible when user is scrolled up and new turns arrive -->
      <button
        v-if="newCount > 0 && !stickToBottom"
        class="new-pill"
        @click="scrollToBottom"
      >↓ {{ newCount }} new</button>
    </div>

    <!-- Inject bar (collapsible) -->
    <footer v-if="injectOpen && onInject" class="term-inject">
      <div v-if="injectFiles.length" class="inject-chips">
        <span v-for="(f, i) in injectFiles" :key="i" class="inject-chip">
          {{ f.type?.startsWith('image') ? '🖼' : '🎤' }} {{ f.name.slice(0, 24) }}
          <button class="chip-x" @click="removeFile(i)">×</button>
        </span>
      </div>
      <div class="inject-row">
        <button class="ctrl-btn" title="Image" @click="attachFile('image')">📎</button>
        <button class="ctrl-btn" title="Audio" @click="attachFile('audio')">🎤</button>
        <input
          v-model="injectText"
          class="inject-input"
          placeholder="Type a message…"
          :disabled="injectBusy"
          @keydown.enter="submitInject"
        />
        <button
          class="inject-send"
          :disabled="injectBusy || (!injectText.trim() && !injectFiles.length)"
          @click="submitInject"
        >{{ injectBusy ? '⏳' : '➤' }}</button>
      </div>
      <div v-if="injectResult" class="inject-feedback" :class="{ ok: injectResult.status !== 'error', err: injectResult.status === 'error' }">
        {{ injectResult.status === 'error' ? '⚠️ ' : '✓ ' }}
        {{ String(injectResult.response || injectResult.status || '') }}
      </div>
    </footer>
  </section>
</template>

<style scoped>
.agent-terminal {
  display: flex;
  flex-direction: column;
  background: #0a0d14;
  border: 1px solid #1a1d2e;
  border-radius: 10px;
  overflow: hidden;
  border-left: 3px solid var(--status-color, #555872);
  font-family: 'JetBrains Mono', 'SF Mono', Menlo, monospace;
  font-size: 12.5px;
  min-height: 360px;
  max-height: 560px;
}

/* Header */
.term-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background: #0c0f17;
  border-bottom: 1px solid #1a1d2e;
  flex-shrink: 0;
}

.term-title {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--status-color);
  flex-shrink: 0;
}
.status-dot.pulse { animation: pulse 1.6s ease-in-out infinite; }
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}

.agent-name {
  font-weight: 600;
  color: #f0f2f5;
  font-size: 13px;
  font-family: 'Inter', sans-serif;
}
.team-tag {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 6px;
  background: rgba(99, 102, 241, 0.15);
  color: #a5b4fc;
  font-family: 'Inter', sans-serif;
}
.agent-model {
  font-size: 10px;
  color: #555872;
}

.term-controls {
  display: flex;
  align-items: center;
  gap: 6px;
}
.status-label {
  font-size: 10px;
  color: var(--status-color);
  font-weight: 500;
  font-family: 'Inter', sans-serif;
}
.ctrl-btn {
  background: transparent;
  border: 1px solid #1a1d2e;
  color: #8b8fa3;
  font-size: 12px;
  width: 24px;
  height: 24px;
  border-radius: 5px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  line-height: 1;
}
.ctrl-btn:hover { background: #1a1d2e; color: #c4c8d4; }
.ctrl-btn.active { background: #1e2233; color: #fff; }
.ctrl-btn.is-paused { background: rgba(34, 197, 94, 0.15); color: #22c55e; border-color: rgba(34, 197, 94, 0.3); }
.ctrl-btn.ctrl-danger:hover { color: #ef4444; border-color: rgba(239,68,68,0.3); }

/* Body — terminal feed */
.term-body {
  position: relative;
  flex: 1;
  overflow-y: auto;
  padding: 8px 12px 12px;
  background: #0a0d14;
  scroll-behavior: smooth;
}
.term-body::-webkit-scrollbar { width: 6px; }
.term-body::-webkit-scrollbar-thumb { background: #1a1d2e; border-radius: 3px; }
.term-empty {
  color: #555872;
  text-align: center;
  padding: 32px;
  font-size: 12px;
}

/* Run separator */
.run-divider {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 8px 0;
  color: #3b82f6;
  font-size: 10px;
}
.run-time { color: #555872; }
.run-rule { flex: 1; height: 1px; background: rgba(59, 130, 246, 0.25); }
.run-tag { color: #6b7280; }

/* Turn row — compact log layout. The prefix is a tiny header row
   (timestamp + role glyph) above full-width content. We dropped the
   fixed 130px meta column because:
     - "USER"/"ASSISTANT" labels were visually redundant with the
       colored glyph + content tone, and ate ~30% of width on mobile.
     - Long Vietnamese assistant turns wrapped every 3-4 words at
       narrow viewports.
*/
.turn-row {
  padding: 6px 8px 6px 8px;
  border-bottom: 1px dotted rgba(26, 29, 46, 0.6);
  border-left: 2px solid transparent;
  border-radius: 0 4px 4px 0;
}
.turn-row:last-child { border-bottom: none; }
/* Visual differentiation: assistant turns get a stronger left bar +
   a subtle teal-tinted background; user turns are neutral. The role
   label in .turn-head is the primary signal — these are reinforcers
   so the eye can scan running/responding turns without reading. */
.role-assistant {
  border-left-color: #00d4aa;
  background: rgba(0, 212, 170, 0.04);
}
.role-user {
  border-left-color: rgba(196, 200, 212, 0.25);
}

.turn-head {
  display: flex;
  align-items: center;
  gap: 6px;
  color: #6b7280;
  font-size: 10px;
  margin-bottom: 3px;
  line-height: 1;
  letter-spacing: 0.4px;
}
.turn-time {
  font-variant-numeric: tabular-nums;
  margin-left: auto;     /* push timestamp to the right */
  color: #4b5060;
  font-size: 10px;
}
.turn-glyph {
  font-size: 11px;
  line-height: 1;
}
.turn-role-label {
  text-transform: uppercase;
  font-weight: 600;
  font-size: 10px;
}
.role-assistant .turn-glyph,
.role-assistant .turn-role-label { color: #00d4aa; }
.role-user .turn-glyph,
.role-user .turn-role-label { color: #8b8fa3; }

.turn-body { min-width: 0; }
.turn-text {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  color: #d4d8e3;
  font-size: 12.5px;
  line-height: 1.5;
  font-family: inherit;
}
.role-assistant .turn-text { color: #f0f2f5; }
.role-user .turn-text { color: #c4c8d4; }

.expand-btn {
  display: inline-block;
  margin-top: 4px;
  padding: 2px 8px;
  font-size: 10px;
  background: transparent;
  color: #3b82f6;
  border: 1px dashed rgba(59, 130, 246, 0.4);
  border-radius: 4px;
  cursor: pointer;
  font-family: inherit;
}
.expand-btn:hover { background: rgba(59, 130, 246, 0.08); color: #60a5fa; }

/* Tool list */
.tool-list {
  list-style: none;
  margin: 4px 0 0;
  padding: 0 0 0 4px;
  border-left: 2px solid rgba(245, 158, 11, 0.35);
}
.tool-call, .tool-result {
  display: block;
  padding: 3px 0 3px 10px;
  font-size: 11.5px;
  color: #c4c8d4;
}
.tool-call .tool-glyph { color: #f59e0b; margin-right: 6px; }
.tool-result .tool-glyph { color: #10b981; margin-right: 6px; }
.tool-result.tool-error .tool-glyph { color: #ef4444; }
.tool-name { color: #f59e0b; font-weight: 500; }
.tool-args { color: #8b8fa3; }
.tool-result-text {
  display: block;
  margin: 4px 0 0;
  padding: 4px 8px;
  background: rgba(255, 255, 255, 0.02);
  white-space: pre-wrap;
  word-break: break-word;
  border-radius: 4px;
  color: #a5b4fc;
  font-size: 11px;
  max-height: 240px;
  overflow-y: auto;
  font-family: inherit;
}
.tool-result.tool-error .tool-result-text { color: #fca5a5; }

/* "N new" pill */
.new-pill {
  position: sticky;
  bottom: 8px;
  margin: 0 auto;
  display: block;
  background: #3b82f6;
  color: white;
  border: none;
  padding: 4px 12px;
  border-radius: 20px;
  font-size: 11px;
  font-weight: 500;
  cursor: pointer;
  box-shadow: 0 4px 16px rgba(59, 130, 246, 0.4);
  font-family: 'Inter', sans-serif;
}
.new-pill:hover { background: #2563eb; }

/* Inject bar */
.term-inject {
  flex-shrink: 0;
  border-top: 1px solid #1a1d2e;
  padding: 8px 12px;
  background: #0c0f17;
}
.inject-chips { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 6px; }
.inject-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 1px 8px;
  background: #1e2233;
  border-radius: 12px;
  font-size: 10.5px;
  color: #c4c8d4;
}
.chip-x {
  background: none;
  border: none;
  color: #8b8fa3;
  font-size: 14px;
  cursor: pointer;
  padding: 0 2px;
  line-height: 1;
}
.chip-x:hover { color: #ef4444; }
.inject-row { display: flex; align-items: center; gap: 6px; }
.inject-input {
  flex: 1;
  background: #111318;
  border: 1px solid #1e2030;
  border-radius: 8px;
  padding: 6px 10px;
  color: #f0f2f5;
  font-size: 12px;
  font-family: 'Inter', sans-serif;
  outline: none;
}
.inject-input:focus-within { border-color: #2a3556; }
.inject-send {
  width: 32px;
  height: 32px;
  border-radius: 8px;
  background: #3b82f6;
  color: white;
  border: none;
  cursor: pointer;
  font-size: 14px;
}
.inject-send:disabled { opacity: 0.4; cursor: not-allowed; background: #1e2233; }
.inject-feedback {
  margin-top: 6px;
  padding: 4px 8px;
  border-radius: 6px;
  font-size: 11px;
  font-family: 'Inter', sans-serif;
}
.inject-feedback.ok { background: rgba(16, 185, 129, 0.08); color: #10b981; border: 1px solid rgba(16,185,129,0.2); }
.inject-feedback.err { background: rgba(239, 68, 68, 0.08); color: #ef4444; border: 1px solid rgba(239,68,68,0.2); }

/* Status-driven border accents — green=active (canonical), red=problem. */
.is-running { border-top: 1px solid rgba(16, 185, 129, 0.3); }
.is-error { border-top: 1px solid rgba(239, 68, 68, 0.3); }

/* ─── Responsive ─── */

/* Tablet: tighten the terminal a touch so 2 columns still fit. */
@media (max-width: 900px) {
  .agent-terminal {
    min-height: 320px;
    max-height: 480px;
  }
}

/* Mobile: stack the inject row, drop the model meta, smaller fonts.
   The grid container collapses to one column at this breakpoint
   already (handled in TeamMonitor.vue), so we focus on per-panel
   density here. */
@media (max-width: 640px) {
  .agent-terminal {
    border-radius: 8px;
    min-height: 280px;
    max-height: 60vh;
    font-size: 11.5px;
  }
  .term-header {
    padding: 6px 10px;
    flex-wrap: wrap;
    gap: 6px;
  }
  /* Force the title row to claim a full line so action ctrl-btns
     wrap underneath cleanly instead of squeezing the name to ellipsis
     at depth 2+ nesting. */
  .term-title { width: 100%; min-width: 0; }
  .agent-name { font-size: 12px; }
  /* Cap long team names so they don't break out of the title row. */
  .team-tag {
    max-width: 80px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  /* Hide the model name in the header — it eats space and the agent
     card view already shows it. Keep team tag + name + status. */
  .agent-model { display: none; }
  .status-label { font-size: 10px; }
  /* iOS HIG / WCAG: 40px tap target minimum. Was 22px → tap roulette. */
  .ctrl-btn { width: 40px; height: 40px; font-size: 13px; }

  .term-body { padding: 6px 8px 8px; }
  .turn-row { padding: 3px 0; padding-left: 4px; }
  .turn-head { font-size: 10px; gap: 4px; }
  .turn-text { font-size: 11.5px; line-height: 1.45; }
  .tool-call, .tool-result { font-size: 10.5px; padding-left: 8px; }
  .tool-result-text {
    font-size: 10.5px;
    max-height: 180px;
    padding: 3px 6px;
  }

  .term-inject { padding: 6px 8px; }
  .inject-input { font-size: 12px; padding: 5px 8px; }
  /* Send button matches ctrl-btn min target. */
  .inject-send { width: 40px; height: 40px; font-size: 15px; }
}

/* Very narrow (≤380px): kill the run separator label to save a line
   and tighten code blocks further. */
@media (max-width: 380px) {
  .run-divider { font-size: 9px; }
  .run-tag { display: none; }
  .turn-text { font-size: 11px; }
  .tool-result-text { max-height: 140px; }
}
</style>

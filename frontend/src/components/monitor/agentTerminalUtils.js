/**
 * Pure helpers for AgentTerminal.vue — testable in isolation.
 *
 * Each helper takes a turn (or list of turns) and returns a derived
 * shape used for rendering. Keeping them out of the SFC lets us run
 * them under ``node --test`` without a Vue render context.
 */

/**
 * Insert a "run separator" row whenever ``run_id`` changes between
 * consecutive turns. Returns a flat list of ``{kind:'run'|'turn', ...}``.
 */
export function buildRenderRows(turns) {
  const rows = []
  let prevRun = null
  for (const t of turns || []) {
    if (t.run_id && t.run_id !== prevRun) {
      rows.push({ kind: 'run', run_id: t.run_id, ts: t.ts, key: `run_${t.turn_idx}` })
      prevRun = t.run_id
    }
    rows.push({ kind: 'turn', turn: t, key: `t_${t.turn_idx}` })
  }
  return rows
}

/** Concat all text content blocks of a turn (preserves newlines). */
export function textContent(turn) {
  const blocks = turn?.message?.content || []
  const parts = []
  for (const b of blocks) {
    if (b?.type === 'text' && typeof b.text === 'string' && b.text) parts.push(b.text)
  }
  return parts.join('\n')
}

/** True if any text block in this turn was truncated by the backend. */
export function isTextTruncated(turn) {
  const blocks = turn?.message?.content || []
  return blocks.some(b => b?.type === 'text' && b._truncated === true)
}

/** Extract tool_calls into a render-friendly shape. */
export function toolCallList(turn) {
  const tc = turn?.message?.tool_calls
  if (!tc) return []
  return Object.entries(tc).map(([id, call]) => {
    const params = call?.params || {}
    return {
      id,
      name: params.name || '?',
      args: params.arguments || {},
    }
  })
}

/** Extract tool_results, flagging truncated blocks for "Show full". */
export function toolResultList(turn) {
  const tr = turn?.message?.tool_results
  if (!tr) return []
  return Object.entries(tr).map(([id, res]) => {
    const blocks = res?.content || []
    let text = ''
    let truncated = false
    let fullSize = 0
    for (const b of blocks) {
      if (b?.type === 'text') {
        text += (text ? '\n' : '') + (b.text || '')
        if (b._truncated) {
          truncated = true
          fullSize = Math.max(fullSize, b._full_size || 0)
        }
      }
    }
    return { id, text, truncated, fullSize, isError: !!res?.isError }
  })
}

/** Compact preview of tool_call arguments — shown inline next to tool name. */
export function summarizeArgs(args) {
  if (!args || typeof args !== 'object') return ''
  const parts = []
  for (const [k, v] of Object.entries(args).slice(0, 3)) {
    let s = typeof v === 'string' ? v : JSON.stringify(v)
    if (s.length > 60) s = s.slice(0, 60) + '…'
    parts.push(`${k}=${s}`)
  }
  const rest = Object.keys(args).length - 3
  if (rest > 0) parts.push(`+${rest} more`)
  return parts.join(', ')
}


// Canonical status color/label live in agentMeta.js. Re-exported here
// because the terminal monitor still imports from this module — keeps
// callers stable while routing everyone through the single source of
// truth. (Old whitelist had running=amber/idle=green; that contradicted
// the design tokens and the rest of the app.)
export { statusColor, statusLabel } from '../agent/agentMeta.js'

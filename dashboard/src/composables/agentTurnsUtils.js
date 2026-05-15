/**
 * Pure helpers for the per-agent turn buffer.
 *
 * Extracted from useAgentTurns so the dedup / insert / reset logic can
 * be unit-tested with plain ``node --test`` (no Vue reactivity needed).
 */

/**
 * Insert a turn into a sorted-by-turn_idx array, deduplicating by turn_idx.
 *
 * @param {Array<{turn_idx:number}>} arr — current bucket (caller's snapshot)
 * @param {{turn_idx:number}} turn
 * @param {number} maxPerAgent — cap on returned size
 * @returns {Array} a new array (does not mutate the input)
 */
export function insertTurn(arr, turn, maxPerAgent) {
  const next = arr.slice()
  const existingIdx = next.findIndex(t => t.turn_idx === turn.turn_idx)
  if (existingIdx >= 0) {
    next[existingIdx] = turn
  } else {
    let i = next.length
    while (i > 0 && next[i - 1].turn_idx > turn.turn_idx) i--
    next.splice(i, 0, turn)
  }
  if (next.length > maxPerAgent) next.splice(0, next.length - maxPerAgent)
  return next
}

/**
 * Detect a "history was cleared" signal: a delta arriving at turn_idx 0
 * when the bucket already has higher turn_idx entries means the agent
 * started a new conversation; the old turns are stale.
 */
export function isResetSignal(arr, turn) {
  return (
    !!turn &&
    turn.turn_idx === 0 &&
    arr.length > 0 &&
    arr[arr.length - 1].turn_idx > 0
  )
}

/**
 * Last assistant text-content preview from a sorted bucket.
 */
export function lastAssistantText(arr) {
  for (let i = arr.length - 1; i >= 0; i--) {
    const t = arr[i]
    if (t.role === 'assistant') {
      const block = (t.message?.content || [])[0]
      return block?.text || ''
    }
  }
  return ''
}

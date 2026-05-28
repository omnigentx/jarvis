/**
 * Pure helpers for the per-agent turn buffer.
 *
 * Extracted from useAgentTurns so the dedup / insert / reset logic can
 * be unit-tested with plain ``node --test`` (no Vue reactivity needed).
 */

/**
 * Composite key used to dedupe turns. resume_spawn / restart_spawn / auto-
 * resume all start the NEW subprocess at turn_idx=0 while the agent_name
 * stays the same — so turn_idx alone is not unique across runs and would
 * cause new turns to overwrite history. The (run_id, turn_idx) pair IS
 * unique per logical message. Falls back to turn_idx when run_id missing
 * (legacy events) so older buckets still dedupe correctly.
 */
function _turnKey(turn) {
  return `${turn?.run_id || ''}::${turn?.turn_idx}`
}

/**
 * Stable ordering: turns are sorted by (timestamp, run_id-appearance,
 * turn_idx). Falling back to insertion order keeps runs grouped in the
 * order they happened — buildRenderRows then inserts a "run X" separator
 * whenever run_id changes, so the user sees runs stacked chronologically.
 */
function _compareTurns(a, b) {
  // Prefer timestamp when both sides have it — survives clock skew across
  // runs of the same agent better than run_id alphabetical compare.
  if (a.ts && b.ts && a.ts !== b.ts) return a.ts - b.ts
  // Same logical run (or BOTH legacy missing run_id) → sort by turn_idx.
  // Cross-run without timestamps falls through to "equal" (keep insertion
  // order) — buildRenderRows then groups by run_id appearance.
  if ((a.run_id || '') === (b.run_id || '')) {
    return (a.turn_idx ?? 0) - (b.turn_idx ?? 0)
  }
  return 0
}

/**
 * Insert a turn into a bucket, deduplicating by (run_id, turn_idx).
 *
 * @param {Array<{turn_idx:number, run_id?:string}>} arr — current bucket
 * @param {{turn_idx:number, run_id?:string}} turn
 * @param {number} maxPerAgent — cap on returned size
 * @returns {Array} a new array (does not mutate the input)
 */
export function insertTurn(arr, turn, maxPerAgent) {
  const next = arr.slice()
  const key = _turnKey(turn)
  const existingIdx = next.findIndex(t => _turnKey(t) === key)
  if (existingIdx >= 0) {
    next[existingIdx] = turn
  } else {
    // Append, then sort stably so out-of-order arrival lands correctly.
    next.push(turn)
    next.sort(_compareTurns)
  }
  if (next.length > maxPerAgent) next.splice(0, next.length - maxPerAgent)
  return next
}

/**
 * Detect a "history was cleared" signal.
 *
 * A delta at ``turn_idx=0`` only means the bucket should be wiped when
 * the SAME run_id is restarting from scratch (a real reset). When the
 * run_id differs from the latest one in the bucket, this is a fresh run
 * (resume_spawn / restart_spawn / auto-resume) and the previous run's
 * turns must be kept so the user can see the stacked history.
 */
export function isResetSignal(arr, turn) {
  if (!turn || turn.turn_idx !== 0 || arr.length === 0) return false
  const last = arr[arr.length - 1]
  if (last.turn_idx <= 0) return false
  // Different run_id ⇒ new run of the same agent, NOT a reset of the
  // current conversation. Keep prior turns.
  if (turn.run_id && last.run_id && turn.run_id !== last.run_id) return false
  return true
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

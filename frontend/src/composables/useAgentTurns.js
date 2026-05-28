/**
 * useAgentTurns — per-agent ring buffer of PromptMessageExtended turns.
 *
 * Source of truth for the Team Monitor v2 UI. Each entry is a turn from
 * the agent's ``message_history``, tagged with a stable ``turn_idx``.
 * Live deltas arrive via the ``message_turn`` SSE event (see
 * ``stores/agents.js`` ingest); initial history is fetched from
 * ``GET /api/agents/{name}/messages`` per agent on demand.
 *
 * Dedup is by ``(agent_name, turn_idx)``. The store keeps the latest
 * ``maxPerAgent`` turns to bound memory; older entries are dropped.
 *
 * Why not just read ``store.recentEvents``? That array only carries
 * synthesized lifecycle events (started/idle/error/...). The new
 * ``message_turn`` channel is shape-stable and frequency-bounded
 * (one per agent per turn), so it lives in its own keyed map.
 */

import { ref, shallowRef, triggerRef, computed, onMounted, onUnmounted, watch } from 'vue'
import { apiFetch } from '../api'
import { useAgentsStore } from '../stores/agents'
import { insertTurn, isResetSignal, lastAssistantText } from './agentTurnsUtils.js'

export function useAgentTurns(options = {}) {
  const maxPerAgent = options.maxPerAgent ?? 200

  // Map<agentName, Turn[]> where Turn = { turn_idx, role, message, run_id, ts }
  // Each agent's array is sorted ascending by turn_idx.
  // shallowRef so Vue doesn't deep-watch every nested message blob.
  const turns = shallowRef(new Map())

  // Set of agent names whose initial history fetch is in flight or complete.
  const fetched = ref(new Set())

  // Map<agentName, Promise> dedup of in-flight history fetches.
  const _pendingFetches = new Map()

  const store = useAgentsStore()

  /** Internal: get-or-create the array for an agent, returning a fresh copy. */
  function _bucket(agentName) {
    return turns.value.get(agentName) || []
  }

  /** Insert/replace a turn keyed by turn_idx. Sorted ascending. Bounded by maxPerAgent. */
  function ingestTurn(agentName, turn) {
    if (!agentName || !turn || typeof turn.turn_idx !== 'number') return
    const arr = insertTurn(_bucket(agentName), turn, maxPerAgent)
    const next = new Map(turns.value)
    next.set(agentName, arr)
    turns.value = next
  }

  /** If the delta is a reset signal, drop the bucket so old turns don't linger. */
  function handlePossibleReset(agentName, turn) {
    if (isResetSignal(_bucket(agentName), turn)) {
      const next = new Map(turns.value)
      next.set(agentName, [])
      turns.value = next
    }
  }

  /** Fetch initial message history for an agent (idempotent). */
  async function fetchInitial(agentName) {
    if (!agentName) return
    if (fetched.value.has(agentName)) return
    if (_pendingFetches.has(agentName)) return _pendingFetches.get(agentName)

    const p = (async () => {
      try {
        const data = await apiFetch(`/api/agents/${encodeURIComponent(agentName)}/messages?limit=200`)
        const items = data?.turns || []
        const arr = items.map(t => ({
          turn_idx: t.turn_idx,
          role: t.role,
          message: t.message,
          run_id: t.run_id || null,
          ts: t.timestamp || null,
        })).sort((a, b) => a.turn_idx - b.turn_idx)

        const next = new Map(turns.value)
        next.set(agentName, arr.slice(-maxPerAgent))
        turns.value = next
        fetched.value = new Set([...fetched.value, agentName])
      } catch (e) {
        console.warn(`[useAgentTurns] initial fetch failed for ${agentName}:`, e?.message || e)
      } finally {
        _pendingFetches.delete(agentName)
      }
    })()

    _pendingFetches.set(agentName, p)
    return p
  }

  /** Fetch the untruncated content for one turn (used by "Show full" UX). */
  async function fetchTurnFull(agentName, turnIdx) {
    if (!agentName || typeof turnIdx !== 'number') return null
    try {
      return await apiFetch(
        `/api/agents/${encodeURIComponent(agentName)}/turns/${turnIdx}/full`,
      )
    } catch (e) {
      console.warn(`[useAgentTurns] full fetch failed for ${agentName}#${turnIdx}:`, e?.message || e)
      return null
    }
  }

  /** Reactive accessor: turns for one agent, ascending. */
  function getTurns(agentName) {
    return turns.value.get(agentName) || []
  }

  /** Last assistant text — useful for header preview. */
  function getLastAssistantText(agentName) {
    return lastAssistantText(getTurns(agentName))
  }

  // ── Bridge: pull message_turn events from the store as they arrive ──
  //
  // The agents store already invokes processEvent() on every SSE event.
  // We watch its ``recentEvents`` queue and pick out ``message_turn``
  // entries for ingest. (recentEvents is a 50-cap FIFO of all event types.)

  let _lastSeenEvent = null
  const stopWatch = watch(
    () => store.recentEvents,
    (events) => {
      if (!events?.length) return
      const batch = []
      for (let i = 0; i < events.length; i++) {
        if (events[i] === _lastSeenEvent) break
        batch.push(events[i])
      }
      // Process oldest first so turn_idx ordering is preserved.
      for (let i = batch.length - 1; i >= 0; i--) {
        const evt = batch[i]
        if (evt?.event_type !== 'message_turn') continue
        const d = evt.data || {}
        if (typeof d.turn_idx !== 'number') continue
        const turn = {
          turn_idx: d.turn_idx,
          role: d.role || d.message?.role || null,
          message: d.message || {},
          run_id: evt.run_id || null,
          ts: evt.timestamp || null,
        }
        handlePossibleReset(evt.agent_name, turn)
        ingestTurn(evt.agent_name, turn)
      }
      _lastSeenEvent = events[0]
    },
    { flush: 'post' },
  )

  onUnmounted(() => stopWatch())

  return {
    turns,
    fetched,
    fetchInitial,
    fetchTurnFull,
    getTurns,
    getLastAssistantText,
    ingestTurn, // exposed for tests
    _internal_handleReset: handlePossibleReset, // exposed for tests
  }
}

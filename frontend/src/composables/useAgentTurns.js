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

import { ref, shallowRef, onUnmounted, watch } from 'vue'
import { apiFetch } from '../api.js'
import { useAgentsStore } from '../stores/agents.js'
import { insertTurn, isResetSignal, lastAssistantText } from './agentTurnsUtils.js'
import { agentIdentity, findAgentByIdentity } from './agentIdentity.js'

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
  function _bucket(identity) {
    return turns.value.get(identity) || []
  }

  /** Insert/replace a turn keyed by turn_idx. Sorted ascending. Bounded by maxPerAgent. */
  function ingestTurn(agent, turn) {
    if (!agent?.name || !turn || typeof turn.turn_idx !== 'number') return
    const identity = agentIdentity(agent)
    const arr = insertTurn(_bucket(identity), turn, maxPerAgent)
    const next = new Map(turns.value)
    next.set(identity, arr)
    turns.value = next
  }

  /** If the delta is a reset signal, drop the bucket so old turns don't linger. */
  function handlePossibleReset(agent, turn) {
    const identity = agentIdentity(agent)
    if (isResetSignal(_bucket(identity), turn)) {
      const next = new Map(turns.value)
      next.set(identity, [])
      turns.value = next
    }
  }

  /** Fetch initial message history for an agent (idempotent). */
  async function fetchInitial(agent) {
    if (!agent?.name) return
    const { name: agentName, session_id: sessionId } = agent
    const identity = agentIdentity(agent)
    if (fetched.value.has(identity)) return
    if (_pendingFetches.has(identity)) return _pendingFetches.get(identity)

    const p = (async () => {
      try {
        const data = await apiFetch(`/api/agents/${encodeURIComponent(agentName)}/messages?limit=200${sessionId ? `&session_id=${encodeURIComponent(sessionId)}` : ''}`)
        const items = data?.turns || []
        const arr = items.map(t => ({
          turn_idx: t.turn_idx,
          role: t.role,
          message: t.message,
          run_id: t.run_id || null,
          ts: t.timestamp || null,
        })).sort((a, b) => a.turn_idx - b.turn_idx)

        const next = new Map(turns.value)
        next.set(identity, arr.slice(-maxPerAgent))
        turns.value = next
        fetched.value = new Set([...fetched.value, identity])
      } catch (e) {
        console.warn(`[useAgentTurns] initial fetch failed for ${agentName}:`, e?.message || e)
      } finally {
        _pendingFetches.delete(identity)
      }
    })()

    _pendingFetches.set(identity, p)
    return p
  }

  /** Fetch the untruncated content for one turn (used by "Show full" UX). */
  async function fetchTurnFull(agent, turnIdx) {
    if (!agent?.name || typeof turnIdx !== 'number') return null
    const { name: agentName, session_id: sessionId } = agent
    try {
      return await apiFetch(
        `/api/agents/${encodeURIComponent(agentName)}/turns/${turnIdx}/full${sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''}`,
      )
    } catch (e) {
      console.warn(`[useAgentTurns] full fetch failed for ${agentName}#${turnIdx}:`, e?.message || e)
      return null
    }
  }

  /** Reactive accessor: turns for one agent, ascending. */
  function getTurns(agent) {
    return turns.value.get(typeof agent === 'string' ? agent : agentIdentity(agent)) || []
  }

  /** Last assistant text — useful for header preview. */
  function getLastAssistantText(agent) {
    return lastAssistantText(getTurns(agent))
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
        const rosterAgent = findAgentByIdentity(store.agentsList, evt.agent_name, evt.session_id)
        if (!rosterAgent) continue
        handlePossibleReset(rosterAgent, turn)
        ingestTurn(rosterAgent, turn)
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

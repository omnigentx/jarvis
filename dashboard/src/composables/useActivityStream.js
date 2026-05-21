import { ref, shallowRef, triggerRef, computed, onMounted, onUnmounted, watch } from 'vue'
import { useAgentsStore } from '../stores/agents'
import { normalizeTs, formatTimestamp } from '../utils/timeFormat.js'

// Re-export for callers that still import these from here.
export { normalizeTs, formatTimestamp }

/**
 * Activity Stream composable for Team Monitor.
 *
 * Groups SSE events by agent_name into per-agent event logs and
 * provides computed helpers for status filtering.
 *
 * Loads persisted events from SQLite on mount, then merges with
 * live SSE events from the Pinia store.
 *
 * @param {Object} options
 * @param {number} [options.maxEventsPerAgent=100] - max events per agent
 */
export function useActivityStream(options = {}) {
  const maxPerAgent = options.maxEventsPerAgent ?? 100

  // Per-agent event logs: Map<agentName, Event[]>
  // Using shallowRef to prevent Vue from rebuilding DOM subtrees when new events
  // arrive — this is what caused scroll position to jump on every new event.
  const agentEvents = shallowRef(new Map())

  // Active filter: 'all' | 'active' | 'team:<name>'
  const filter = ref('all')

  // Agent selection. Filled by a watcher once the store roster loads so
  // the dropdown opens with every agent already ticked AND the bulk-
  // delete count badge shows the full roster size. Without this, an
  // empty Set rendered visually as "all checked" (filter convention)
  // while bulk-delete saw zero selections — see the 2026-05-21 thread
  // about that mismatch.
  const selectedAgents = ref(new Set())
  // Flips to true the first time the user touches the selection
  // (toggleAgent / selectAll / clearAll / toggleSortLock... any explicit
  // intent). Once flipped, the "auto-select-all-on-first-roster-load"
  // watcher stops mutating selectedAgents — the user's choice wins.
  const userTouchedSelection = ref(false)

  // Sort lock: freeze grid order so agents stop jumping
  const sortLocked = ref(false)
  const lockedOrder = ref([]) // snapshot of agent names when locked

  // Loading state for persisted events
  const isLoadingHistory = ref(false)

  const store = useAgentsStore()

  // --- Load persisted events on mount ---
  onMounted(async () => {
    isLoadingHistory.value = true
    try {
      const data = await store.fetchAllActivities(20)
      // data is { agentName: [{id, event_type, message, ...}, ...], ... }
      const map = new Map(agentEvents.value)
      for (const [agentName, events] of Object.entries(data)) {
        if (!Array.isArray(events) || events.length === 0) continue
        const existing = map.get(agentName) || []
        // Normalize persisted events to match SSE event shape
        const normalized = events.map(e => ({
          agent_name: agentName,
          event_type: e.event_type,
          message: e.message,
          run_id: e.run_id,
          data: e.data,
          timestamp: e.created_at,
          _persisted: true, // Mark as persisted for dedup
        }))
        // Merge: persisted events go after any existing live events
        const merged = dedupeEvents([...existing, ...normalized])
        if (merged.length > maxPerAgent) merged.length = maxPerAgent
        map.set(agentName, merged)
      }
      agentEvents.value = map
    } catch (e) {
      console.error('[ActivityStream] Failed to load persisted events:', e)
    } finally {
      isLoadingHistory.value = false
    }
  })

  // --- Watch store's recentEvents for new SSE events ---
  // Use Vue watch with object identity tracking instead of length-based polling.
  // recentEvents is capped at 50 items, so length comparison fails once full.
  let lastSeenEvent = null

  const stopWatch = watch(
    () => store.recentEvents,
    (events) => {
      if (!events.length) return
      // Events are prepended (newest first). Walk from newest until
      // we hit the last event we already processed.
      const batch = []
      for (let i = 0; i < events.length; i++) {
        if (events[i] === lastSeenEvent) break
        batch.push(events[i])
      }
      // Process in chronological order (oldest first) so they stack correctly
      for (let i = batch.length - 1; i >= 0; i--) {
        addEvent(batch[i])
      }
      lastSeenEvent = events[0]
    },
    { flush: 'post' },
  )

  onUnmounted(() => stopWatch())

  /**
   * Deduplicate events by timestamp + event_type combo.
   */
  function dedupeEvents(events) {
    const seen = new Set()
    return events.filter(e => {
      const key = `${e.timestamp}_${e.event_type}`
      if (seen.has(key)) return false
      seen.add(key)
      return true
    })
  }

  function addEvent(event) {
    const name = event.agent_name
    if (!name) return

    const map = agentEvents.value
    const existing = map.get(name) || []
    // Dedupe by timestamp + event_type
    const key = `${event.timestamp}_${event.event_type}`
    if (existing.some(e => `${e.timestamp}_${e.event_type}` === key)) return

    existing.unshift(event)
    if (existing.length > maxPerAgent) existing.length = maxPerAgent
    map.set(name, existing)
    // triggerRef instead of new Map() — notifies watchers without
    // causing Vue to rebuild DOM subtrees, preserving scroll positions.
    triggerRef(agentEvents)
  }

  // All known agent names (for dropdown options)
  const allAgentNames = computed(() => {
    return store.agentsList.map(a => a.name).sort()
  })

  // --- Computed: filtered agent panels ---
  const filteredAgents = computed(() => {
    // Team Monitor does NOT use status-priority sort (running/error first).
    // Keep Jarvis (is_default) pinned at top, then alphabetical by name —
    // stable layout so panels don't reshuffle as agents transition status.
    let agents = [...store.agentsList].sort((a, b) => {
      if (a.is_default && !b.is_default) return -1
      if (!a.is_default && b.is_default) return 1
      return a.name.localeCompare(b.name)
    })

    // Apply status filter
    switch (filter.value) {
      case 'active':
        agents = agents.filter(a => a.status === 'running' || a.status === 'error')
        break
      default:
        if (filter.value.startsWith('team:')) {
          const teamName = filter.value.slice(5)
          agents = agents.filter(a => a.team_name === teamName)
        }
        break
    }

    // Apply agent selection filter
    if (selectedAgents.value.size > 0) {
      agents = agents.filter(a => selectedAgents.value.has(a.name))
    }

    // Apply sort lock: keep order frozen but data stays live
    if (sortLocked.value && lockedOrder.value.length > 0) {
      const orderMap = new Map(lockedOrder.value.map((name, i) => [name, i]))
      const sorted = [...agents].sort((a, b) => {
        const aIdx = orderMap.get(a.name) ?? 9999
        const bIdx = orderMap.get(b.name) ?? 9999
        return aIdx - bIdx
      })
      return sorted
    }

    return agents
  })

  // Selection helpers
  //
  // Selection-state contract (post-2026-05-21):
  // - Empty Set = "no explicit selection" (initial state). Visual:
  //   all checkboxes render checked because the filter is inactive;
  //   `dropdownLabel` says "All Agents". BUT downstream consumers that
  //   need an explicit name list (e.g. bulk-delete) MUST treat this as
  //   zero selection — never as "all". This guards destructive actions
  //   from firing without user consent.
  // - `selectAll()` PROMOTES the implicit-all state to an explicit Set
  //   containing every name from the store, so the delete button (and
  //   the count badge) match the visual.
  // - Toggling an individual when starting from empty: the click means
  //   "I want all EXCEPT this one" — expand the implicit-all into an
  //   explicit Set, then remove the clicked name. Without this, the
  //   first click flips from "all checked" to "only this one checked"
  //   which is the opposite of what the checkbox visually promised.
  // - `clearAll()` writes the `__none__` sentinel to signal "show empty
  //   grid", which is filtered out of explicit name lists.

  function toggleAgent(name) {
    userTouchedSelection.value = true
    let s = new Set(selectedAgents.value)
    if (s.size === 0) {
      // Expand implicit-all → explicit roster so we can subtract from it.
      s = new Set(store.agentsList.map(a => a.name))
    }
    s.delete('__none__')
    if (s.has(name)) s.delete(name)
    else s.add(name)
    selectedAgents.value = s
  }

  function selectAll() {
    userTouchedSelection.value = true
    // Materialize the roster so consumers see real names, not the
    // implicit-all empty Set. Drops the `__none__` sentinel implicitly
    // by overwriting the value.
    selectedAgents.value = new Set(store.agentsList.map(a => a.name))
  }

  function clearAll() {
    userTouchedSelection.value = true
    // Select none — show empty grid
    selectedAgents.value = new Set(['__none__'])
  }

  // Auto-fill selection on first roster load. Watches store.agentsList:
  // the moment it transitions from empty → non-empty (initial fetch
  // completes), populate selectedAgents with every name so default state
  // is "all selected" (visual ✓ + state ✓ + delete button enabled with
  // full count, all in sync). Stops mutating once the user has made any
  // explicit choice — even back to "everything" — to avoid stomping on
  // intent (e.g. user clears, new agent arrives later, the watcher
  // should NOT re-select-all).
  const stopRosterAutoSelect = watch(
    () => store.agentsList.length,
    (n) => {
      if (n === 0) return
      if (userTouchedSelection.value) return
      if (selectedAgents.value.size > 0) return  // already populated
      selectedAgents.value = new Set(store.agentsList.map(a => a.name))
    },
    { immediate: true },
  )

  onUnmounted(() => stopRosterAutoSelect())

  function toggleSortLock() {
    if (!sortLocked.value) {
      // Snapshot current order before locking
      lockedOrder.value = filteredAgents.value.map(a => a.name)
    }
    sortLocked.value = !sortLocked.value
  }

  /**
   * Get events for a specific agent, filtered to a time window.
   * @param {string} agentName
   * @param {number} windowMs - milliseconds back from now (default 1h). 0 = no filter.
   */
  function getEvents(agentName, windowMs = 3_600_000) {
    const events = agentEvents.value.get(agentName) || []
    if (!windowMs) return events
    const cutoff = Date.now() - windowMs
    return events.filter(e => {
      const ms = normalizeTs(e.timestamp)
      if (ms === null) return true // keep events with no parseable timestamp
      return ms >= cutoff
    })
  }

  // Helper: latest action summary for an agent
  function getCurrentAction(agentName) {
    const events = getEvents(agentName)
    if (!events.length) return null
    const latest = events[0]
    return {
      type: latest.event_type,
      message: latest.message || latest.event_type,
      timestamp: latest.timestamp,
      data: latest.data,
    }
  }

  return {
    agentEvents,
    filter,
    selectedAgents,
    sortLocked,
    isLoadingHistory,
    allAgentNames,
    filteredAgents,
    getEvents,
    getCurrentAction,
    addEvent,
    toggleAgent,
    selectAll,
    clearAll,
    toggleSortLock,
  }
}


import { ref, computed } from 'vue'
import { useAgentsStore } from '../stores/agents.js'
import { agentIdentity } from './agentIdentity.js'
import { decodeTeamFilter } from './teamIdentity.js'

export { formatTimestamp } from '../utils/timeFormat.js'

/** Provides agent filtering and selection helpers for Team Monitor. */
export function useActivityStream() {

  // Active filter: 'all' | 'active' | 'team:<name>'
  const filter = ref('all')

  // Agent selection. Empty Set = implicit-all (default state): the filter
  // below treats ``size === 0`` as "no filter applied" and the checkboxes
  // render as ✓ via ``size === 0 || has(name)``. We do NOT eagerly fill
  // this with the roster — the store populates agents one-by-one, so a
  // watcher that latched on first non-zero length would lock the set to
  // whatever agent arrived first and silently drop later arrivals
  // (regression 2026-05-21 e2e: ``agent-monitor.spec.ts`` saw only
  // alpha-agent while beta was already in the store). The bulk-delete
  // count badge derives its number from ``store.agentsList`` whenever
  // ``selectedAgents`` is empty — display layer, not state layer.
  const selectedAgents = ref(new Set())

  // Sort lock: freeze grid order so agents stop jumping
  const sortLocked = ref(false)
  const lockedOrder = ref([]) // snapshot of agent identities

  const store = useAgentsStore()

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
          const { sessionId, teamName } = decodeTeamFilter(filter.value)
          agents = agents.filter(a => a.team_name === teamName && a.session_id === sessionId)
        }
        break
    }

    // Apply agent selection filter
    if (selectedAgents.value.size > 0) {
      agents = agents.filter(a => selectedAgents.value.has(agentIdentity(a)))
    }

    // Apply sort lock: keep order frozen but data stays live
    if (sortLocked.value && lockedOrder.value.length > 0) {
      const orderMap = new Map(lockedOrder.value.map((identity, i) => [identity, i]))
      const sorted = [...agents].sort((a, b) => {
        const aIdx = orderMap.get(agentIdentity(a)) ?? 9999
        const bIdx = orderMap.get(agentIdentity(b)) ?? 9999
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

  function toggleAgent(agent) {
    const identity = typeof agent === 'string' ? agent : agentIdentity(agent)
    let s = new Set(selectedAgents.value)
    if (s.size === 0) {
      // Expand implicit-all → explicit roster so we can subtract from it.
      s = new Set(store.agentsList.map(agentIdentity))
    }
    s.delete('__none__')
    if (s.has(identity)) s.delete(identity)
    else s.add(identity)
    selectedAgents.value = s
  }

  function selectAll() {
    // Materialize the roster so consumers see real names, not the
    // implicit-all empty Set. Drops the `__none__` sentinel implicitly
    // by overwriting the value.
    selectedAgents.value = new Set(store.agentsList.map(agentIdentity))
  }

  function clearAll() {
    // Select none — show empty grid
    selectedAgents.value = new Set(['__none__'])
  }

  function toggleSortLock() {
    if (!sortLocked.value) {
      // Snapshot current order before locking
      lockedOrder.value = filteredAgents.value.map(agentIdentity)
    }
    sortLocked.value = !sortLocked.value
  }

  return {
    filter,
    selectedAgents,
    sortLocked,
    filteredAgents,
    toggleAgent,
    selectAll,
    clearAll,
    toggleSortLock,
  }
}


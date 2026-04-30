import { ref, shallowRef, triggerRef, computed, onMounted, onUnmounted, watch } from 'vue'
import { useAgentsStore } from '../stores/agents'

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

  // Agent selection filter (empty = show all)
  const selectedAgents = ref(new Set())

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
    let agents = store.agentsList

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
  function toggleAgent(name) {
    const s = new Set(selectedAgents.value)
    if (s.has(name)) s.delete(name)
    else s.add(name)
    selectedAgents.value = s
  }

  function selectAll() {
    selectedAgents.value = new Set() // empty = show all
  }

  function clearAll() {
    // Select none — show empty grid
    selectedAgents.value = new Set(['__none__'])
  }

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

/**
 * Normalize any timestamp value to milliseconds.
 *
 * Handles:
 * - Unix seconds (float or int, e.g. 1775967073.51)
 * - Unix milliseconds (e.g. 1775967073000)
 * - ISO 8601 strings (e.g. "2026-04-12T12:30:00Z")
 * - null / undefined / 0 / NaN → returns null
 *
 * Heuristic: numeric values < 1e12 (~year 2001 in ms) are treated as seconds.
 */
export function normalizeTs(ts) {
  if (ts == null || ts === 0 || ts === '') return null

  if (typeof ts === 'number') {
    if (!Number.isFinite(ts)) return null
    // seconds → ms (timestamps < 1e12 are definitely in seconds)
    return ts < 1e12 ? Math.round(ts * 1000) : Math.round(ts)
  }

  if (typeof ts === 'string') {
    // Try numeric string first (e.g. "1775967073.51")
    const num = Number(ts)
    if (Number.isFinite(num) && num > 0) {
      return num < 1e12 ? Math.round(num * 1000) : Math.round(num)
    }
    // Try ISO / date string
    const d = new Date(ts)
    return Number.isFinite(d.getTime()) ? d.getTime() : null
  }

  return null
}

/**
 * Format a timestamp for display: 24h, dd/MM/YYYY.
 *
 * @param {number|string|null} ts - raw timestamp (seconds, ms, or ISO string)
 * @param {Object} opts
 * @param {boolean} [opts.dateOnly=false] - show only date without time
 * @param {boolean} [opts.timeOnly=false] - show only time without date
 * @returns {string} formatted string like "14:30:05 12/04/2026" or "" if invalid
 */
export function formatTimestamp(ts, opts = {}) {
  const ms = normalizeTs(ts)
  if (ms === null) return ''

  const d = new Date(ms)
  if (!Number.isFinite(d.getTime())) return ''

  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  const ss = String(d.getSeconds()).padStart(2, '0')
  const dd = String(d.getDate()).padStart(2, '0')
  const MM = String(d.getMonth() + 1).padStart(2, '0')
  const yyyy = d.getFullYear()

  if (opts.timeOnly) return `${hh}:${mm}:${ss}`
  if (opts.dateOnly) return `${dd}/${MM}/${yyyy}`

  // Same day → show time only to save space
  const now = new Date()
  if (d.getDate() === now.getDate() && d.getMonth() === now.getMonth() && d.getFullYear() === now.getFullYear()) {
    return `${hh}:${mm}:${ss}`
  }

  return `${hh}:${mm}:${ss} ${dd}/${MM}/${yyyy}`
}

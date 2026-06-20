/**
 * Memory store — consumes the small subset of memory SSE events the chat UI
 * reacts to live (spec §17): index refresh hint + degraded banner. Pending
 * memory-candidate discovery is owned by the CENTRAL approvals store
 * (`stores/approvals.js`, sidebar badge) since candidates create approval rows
 * — this store deliberately does NOT duplicate that count. All other memory
 * events exist for the audit trail / Memory page and are fetched on demand.
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'

export const useMemoryStore = defineStore('memory', () => {
  // per-agent transient "retrieval degraded" flag (dense backend down, etc.)
  const degradedByAgent = ref({})
  // bumped whenever something is indexed → views can re-fetch index-status
  const indexTick = ref(0)
  // Retrieval-lane provenance (fts/dense/graph) per recalled excerpt, captured
  // live from retrieval SSE so the chat "memories used" chip can SHOW which lane
  // surfaced each memory — without baking it into the prompt (which is kept lean).
  // Keyed by excerpt prefix (the SSE truncates to 160 chars); accumulates across
  // recalls. Live-only (lost on reload — use the Memory tab search for history).
  const recallLanes = ref({})

  function isDegraded(agent) {
    return Boolean(degradedByAgent.value[agent])
  }

  // Best-prefix match: the chip line holds the FULL excerpt, the stored key is
  // the SSE-truncated one, so either may be a prefix of the other.
  function lanesForExcerpt(text) {
    const t = (text || '').trim()
    if (!t) return null
    const map = recallLanes.value
    if (map[t]) return map[t]
    for (const k in map) {
      if (k && (t.startsWith(k) || k.startsWith(t))) return map[k]
    }
    return null
  }

  function _captureLanes(event) {
    const ev = event?.data?.evidence
    if (!Array.isArray(ev)) return
    const next = { ...recallLanes.value }
    for (const e of ev) {
      if (e?.excerpt && Array.isArray(e.lanes)) next[e.excerpt.trim()] = e.lanes
    }
    recallLanes.value = next
  }

  function processMemoryEvent(event) {
    // memory_indexed is a GLOBAL refresh tick from the index worker (no agent) —
    // handle it BEFORE the per-agent guard so the drain hint reaches the panels.
    if (event.event_type === 'memory_indexed') {
      indexTick.value += 1
      return
    }
    const agent = event.agent_name
    if (!agent) return
    switch (event.event_type) {
      case 'retrieval_degraded':
        degradedByAgent.value = { ...degradedByAgent.value, [agent]: true }
        _captureLanes(event)
        break
      default:
        if (event.event_type === 'retrieval_completed') {
          // a healthy retrieval clears the degraded flag
          if (degradedByAgent.value[agent]) {
            degradedByAgent.value = { ...degradedByAgent.value, [agent]: false }
          }
          _captureLanes(event)
        }
    }
  }

  return {
    degradedByAgent, indexTick, recallLanes,
    isDegraded, lanesForExcerpt, processMemoryEvent,
  }
})

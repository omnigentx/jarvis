/**
 * Memory store — consumes the small subset of memory SSE events the chat UI
 * reacts to live (spec §17): candidate badge, index refresh hint, degraded
 * banner. All other memory events exist for the audit trail / Memory page and
 * are fetched on demand, not pushed here.
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'

export const useMemoryStore = defineStore('memory', () => {
  // per-agent count of candidates awaiting approval (drives the tab badge)
  const pendingByAgent = ref({})
  // per-agent transient "retrieval degraded" flag (Qdrant down, etc.)
  const degradedByAgent = ref({})
  // bumped whenever something is indexed → views can re-fetch index-status
  const indexTick = ref(0)

  function _bump(map, agent, delta) {
    const next = Math.max(0, (map.value[agent] || 0) + delta)
    map.value = { ...map.value, [agent]: next }
  }

  function pendingCount(agent) {
    return pendingByAgent.value[agent] || 0
  }

  function isDegraded(agent) {
    return Boolean(degradedByAgent.value[agent])
  }

  function setPending(agent, count) {
    pendingByAgent.value = { ...pendingByAgent.value, [agent]: Math.max(0, count) }
  }

  function processMemoryEvent(event) {
    const agent = event.agent_name
    if (!agent) return
    switch (event.event_type) {
      case 'memory_candidate_created':
        _bump(pendingByAgent, agent, +1)
        break
      case 'memory_candidate_approved':
      case 'memory_candidate_rejected':
        _bump(pendingByAgent, agent, -1)
        break
      case 'memory_indexed':
        indexTick.value += 1
        break
      case 'retrieval_degraded':
        degradedByAgent.value = { ...degradedByAgent.value, [agent]: true }
        break
      default:
        // a healthy retrieval clears the degraded flag
        if (event.event_type === 'retrieval_completed' && degradedByAgent.value[agent]) {
          degradedByAgent.value = { ...degradedByAgent.value, [agent]: false }
        }
    }
  }

  return {
    pendingByAgent, degradedByAgent, indexTick,
    pendingCount, isDegraded, setPending, processMemoryEvent,
  }
})

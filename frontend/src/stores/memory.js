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

  function isDegraded(agent) {
    return Boolean(degradedByAgent.value[agent])
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
        break
      default:
        // a healthy retrieval clears the degraded flag
        if (event.event_type === 'retrieval_completed' && degradedByAgent.value[agent]) {
          degradedByAgent.value = { ...degradedByAgent.value, [agent]: false }
        }
    }
  }

  return {
    degradedByAgent, indexTick,
    isDegraded, processMemoryEvent,
  }
})

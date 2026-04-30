import { ref, onMounted, onUnmounted } from 'vue'
import { useAgentsStore } from '../stores/agents'
import { buildSSEUrl, getApiKey } from '../api'

/**
 * SSE realtime connection composable.
 * Manages EventSource lifecycle, reconnection with exponential backoff,
 * and tab-visibility reconciliation.
 *
 * @param {Object} options
 * @param {string} [options.agentFilter] - filter events by agent name
 * @param {Function} [options.onEvent] - custom per-event callback
 */
export function useRealtimeStream(options = {}) {
  const status = ref('connecting') // 'connecting' | 'connected' | 'disconnected' | 'error'
  const retryCount = ref(0)
  let eventSource = null
  let retryTimer = null
  let destroyed = false

  const MAX_RETRY_DELAY = 30000 // 30s cap

  function connect() {
    if (destroyed) return

    // Don't attempt connection without API key
    if (!getApiKey()) {
      status.value = 'disconnected'
      return
    }

    status.value = 'connecting'

    const params = {}
    if (options.agentFilter) params.agent_name = options.agentFilter

    const url = buildSSEUrl('/api/agents/activity-stream', params)
    eventSource = new EventSource(url)

    eventSource.onopen = () => {
      status.value = 'connected'
      retryCount.value = 0
      console.log('[SSE] Connected to activity stream')
    }

    eventSource.onmessage = (e) => {
      try {
        const event = JSON.parse(e.data)

        // Skip pings and connected meta events
        if (event.type === 'ping' || event.type === 'connected') return

        // Process through central store
        const store = useAgentsStore()
        store.processEvent(event)

        // Custom per-view hook
        if (options.onEvent) options.onEvent(event)
      } catch (err) {
        console.warn('[SSE] Failed to parse event:', err)
      }
    }

    eventSource.onerror = () => {
      eventSource?.close()
      eventSource = null

      if (destroyed) return
      status.value = 'disconnected'
      scheduleReconnect()
    }
  }

  function scheduleReconnect() {
    if (destroyed) return
    // Exponential backoff: 1s, 2s, 4s, 8s, ... capped at 30s
    const delay = Math.min(1000 * Math.pow(2, retryCount.value), MAX_RETRY_DELAY)
    retryCount.value++
    retryTimer = setTimeout(connect, delay)
  }

  function disconnect() {
    if (retryTimer) clearTimeout(retryTimer)
    if (eventSource) {
      eventSource.close()
      eventSource = null
    }
    status.value = 'disconnected'
  }

  /**
   * Public reconnect — kills existing connection and starts fresh.
   * Used after API key change or manual reconnect.
   */
  function reconnect() {
    retryCount.value = 0
    disconnect()
    connect()
  }

  // Tab visibility: reconnect + reconcile when tab becomes visible
  function onVisibilityChange() {
    if (document.visibilityState === 'visible') {
      if (status.value !== 'connected') {
        reconnect()
      }
      // REST reconcile: re-fetch agent list to fix any missed events
      useAgentsStore().fetchAgents()
    }
  }

  onMounted(() => {
    connect()
    document.addEventListener('visibilitychange', onVisibilityChange)
  })

  onUnmounted(() => {
    destroyed = true
    disconnect()
    document.removeEventListener('visibilitychange', onVisibilityChange)
  })

  return { status, retryCount, disconnect, reconnect }
}

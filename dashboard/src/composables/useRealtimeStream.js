import { useAgentsStore } from '../stores/agents'
import { buildSSEUrl } from '../api'

import { useSSEConnection } from './useSSEConnection.js'

/**
 * Activity-stream SSE for the agents dashboard.
 *
 * Now a thin adapter over :func:`useSSEConnection` — auth gating, retry
 * with backoff, tab visibility, and bus integration are all owned by
 * the shared composable.
 *
 * @param {Object} options
 * @param {string} [options.agentFilter]
 * @param {Function} [options.onEvent]
 */
export function useRealtimeStream(options = {}) {
  const buildUrl = () => {
    const params = {}
    if (options.agentFilter) params.agent_name = options.agentFilter
    return buildSSEUrl('/api/agents/activity-stream', params)
  }

  function handleMessage(e) {
    try {
      const event = JSON.parse(e.data)
      if (event.type === 'ping' || event.type === 'connected') return
      const store = useAgentsStore()
      store.processEvent(event)
      if (options.onEvent) options.onEvent(event)
    } catch (err) {
      console.warn('[SSE] Failed to parse event:', err)
    }
  }

  const { status, retryCount, disconnect, reconnect } = useSSEConnection(
    buildUrl,
    {
      onMessage: handleMessage,
      onConnected: () => {
        console.log('[SSE] Connected to activity stream')
        // Catch up on anything missed while disconnected.
        useAgentsStore().fetchAgents()
      },
    },
  )

  return { status, retryCount, disconnect, reconnect }
}

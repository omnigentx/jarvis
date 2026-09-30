/** Snapshot + pushed deltas; no timer, no repeated REST fetch per SSE event. */
import { ref } from 'vue'

function mergeEvent(plugin, event) {
  if (event.runEnded) {
    const bindings = (plugin.bindings || []).map(binding =>
      (event.runEnded === '*' ? !!binding.run_id : binding.run_id === event.runEnded) && binding.status === 'ready'
        ? { ...binding, status: 'needs_reactivation' } : binding)
    return { ...plugin, bindings, status: plugin.status === 'ready' && !bindings.some(binding => binding.status === 'ready')
      ? 'needs_reactivation' : plugin.status }
  }
  const bindings = [...(plugin.bindings || [])]
  if (event.agent) {
    const index = bindings.findIndex(binding => binding.agent === event.agent)
    const binding = { agent: event.agent, status: event.binding_status || event.status }
    if (index === -1) bindings.push(binding)
    else bindings[index] = { ...bindings[index], ...binding }
  }
  return { ...plugin, status: event.status, bindings,
    ...(typeof event.global_enabled === 'boolean' ? { global_enabled: event.global_enabled } : {}) }
}

export function createPluginInventory(fetch) {
  const plugins = ref([])
  const error = ref('')
  const loading = ref(false)
  let sequence = 0
  let request = 0
  const events = new Map()
  const endedRuns = new Map()

  async function reload() {
    const ownRequest = ++request
    const start = sequence
    loading.value = true
    try {
      const result = await fetch('/api/plugins')
      if (ownRequest !== request) return
      plugins.value = result.plugins.map(plugin => {
        const pushed = [...(events.get(plugin.id)?.values() || []), ...endedRuns.values()]
          .filter(event => event.sequence > start).sort((a, b) => a.sequence - b.sequence)
        return pushed.reduce((current, event) => mergeEvent(current, event.data), plugin)
      })
      for (const [id, bucket] of events) {
        for (const [target, event] of bucket) if (event.sequence <= start) bucket.delete(target)
        if (!bucket.size) events.delete(id)
      }
      error.value = ''
    } catch (cause) {
      if (ownRequest === request) error.value = cause.message || 'Could not load plugins'
    } finally {
      if (ownRequest === request) {
        loading.value = false
        endedRuns.clear()
      }
    }
  }

  function onEvent(event) {
    if (['agent_completed', 'completed', 'error', 'timeout', 'cancelled', 'killed', 'agent_removed'].includes(event.event_type)) {
      const run = event.run_id || event.data?.run_id
      if (!run) return
      const data = { runEnded: run }
      if (loading.value) {
        // Bound reconnect bursts; overflow conservatively invalidates team Ready.
        if (endedRuns.size >= 256) endedRuns.set('*', { sequence: ++sequence, data: { runEnded: '*' } })
        else endedRuns.set(run, { sequence: ++sequence, data })
      }
      plugins.value = plugins.value.map(plugin => mergeEvent(plugin, data))
      return
    }
    if (event.event_type !== 'plugin_status' || !event.data?.id || !event.data?.status) return
    const bucket = events.get(event.data.id) || new Map()
    bucket.set(event.data.agent || '', { sequence: ++sequence, data: event.data })
    events.set(event.data.id, bucket)
    const index = plugins.value.findIndex(plugin => plugin.id === event.data.id)
    if (index !== -1) plugins.value[index] = mergeEvent(plugins.value[index], event.data)
  }

  return { plugins, error, loading, reload, onEvent }
}

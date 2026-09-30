/** Snapshot + pushed deltas; no timer, no repeated REST fetch per SSE event. */
import { ref } from 'vue'

function mergeEvent(plugin, event) {
  const bindings = [...(plugin.bindings || [])]
  if (event.agent) {
    const index = bindings.findIndex(binding => binding.agent === event.agent)
    const binding = { agent: event.agent, status: event.status }
    if (index === -1) bindings.push(binding)
    else bindings[index] = binding
  }
  return { ...plugin, status: event.status, bindings }
}

export function createPluginInventory(fetch) {
  const plugins = ref([])
  const error = ref('')
  const loading = ref(false)
  let sequence = 0
  let request = 0
  const events = new Map()

  async function reload() {
    const ownRequest = ++request
    const start = sequence
    loading.value = true
    try {
      const result = await fetch('/api/plugins')
      if (ownRequest !== request) return
      plugins.value = result.plugins.map(plugin => {
        const event = events.get(plugin.id)
        return event && event.sequence > start ? mergeEvent(plugin, event.data) : plugin
      })
      for (const [id, event] of events) if (event.sequence <= start) events.delete(id)
      error.value = ''
    } catch (cause) {
      if (ownRequest === request) error.value = cause.message || 'Could not load plugins'
    } finally {
      if (ownRequest === request) loading.value = false
    }
  }

  function onEvent(event) {
    if (event.event_type !== 'plugin_status' || !event.data?.id || !event.data?.status) return
    events.set(event.data.id, { sequence: ++sequence, data: event.data })
    const index = plugins.value.findIndex(plugin => plugin.id === event.data.id)
    if (index !== -1) plugins.value[index] = mergeEvent(plugins.value[index], event.data)
  }

  return { plugins, error, loading, reload, onEvent }
}

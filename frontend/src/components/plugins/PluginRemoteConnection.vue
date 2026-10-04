<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { apiFetch } from '../../api'
import { useLang } from '../../composables/useLang'
const props = defineProps({ plugin: { type: Object, required: true } })
const { t } = useLang()
const states = ref({})
const flows = ref({})
const busy = ref(false)
const error = ref('')
const connecting = computed(() => Object.values(states.value).includes('connecting'))
const canDisconnect = computed(() => !props.plugin.global_enabled && !(props.plugin.bindings || []).some(binding => ['ready', 'activating', 'deactivating', 'activation_interrupted', 'detach_failed'].includes(binding.status)))
async function load() {
  try {
    const result = await apiFetch(`/api/plugins/${encodeURIComponent(props.plugin.id)}/remote`)
    for (const server of result.servers) {
      states.value[server.name] = server.status
      flows.value[server.name] = server.flow_id
    }
  } catch (cause) { error.value = cause.message }
}
onMounted(load)
watch(() => [props.plugin.remote_status, props.plugin.remote_server], ([status, name]) => {
  if (status && name) { states.value[name] = status; flows.value[name] = null }
})
async function connect(server) {
  if (busy.value || connecting.value) return
  // Open while the click still has browser user activation; do not wait for
  // network discovery and then trigger a popup blocker.
  const popup = window.open('about:blank', '_blank')
  if (!popup) { error.value = t('settings.plugins.remote.popupBlocked'); return }
  popup.opener = null
  busy.value = true
  error.value = ''
  states.value[server.name] = 'connecting'
  try {
    const result = await apiFetch(`/api/plugins/${encodeURIComponent(props.plugin.id)}/remote/connect`, {
      method: 'POST', body: JSON.stringify({ server: server.name,
        redirect_uri: `${window.location.origin}/api/plugins/oauth/callback` }),
    })
    flows.value[server.name] = result.flow_id
    if (result.url) popup.location.href = result.url
    else { popup.close(); await load() }
  } catch (cause) {
    popup.close(); states.value[server.name] = 'connection_failed'; error.value = cause.message
  } finally { busy.value = false }
}
async function cancel(server) {
  const flow = flows.value[server.name]
  if (!flow || busy.value) return
  busy.value = true
  try {
    await apiFetch(`/api/plugins/${encodeURIComponent(props.plugin.id)}/remote/connect/${encodeURIComponent(flow)}`, { method: 'DELETE' })
    states.value[server.name] = 'cancelled'; flows.value[server.name] = null
  } catch (cause) { error.value = cause.message }
  finally { busy.value = false }
}
async function disconnect() {
  if (busy.value || !canDisconnect.value) return
  busy.value = true
  error.value = ''
  try {
    await apiFetch(`/api/plugins/${encodeURIComponent(props.plugin.id)}/remote`, { method: 'DELETE' })
    await load()
  } catch (cause) { error.value = cause.message }
  finally { busy.value = false }
}
</script>
<template>
  <section class="remote-connection" :aria-label="t('settings.plugins.remote.title')">
    <h4>{{ t('settings.plugins.remote.title') }}</h4>
    <p>{{ t('settings.plugins.remote.description') }}</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <div v-for="server in plugin.remote_servers" :key="server.name" class="remote-server">
      <div class="remote-server-copy"><strong>{{ server.name }}</strong><code>{{ server.url }}</code>
        <span role="status" :class="{ ready: states[server.name] === 'connected' }">{{ t(`settings.plugins.remote.states.${states[server.name] || 'disconnected'}`) }}</span></div>
      <button v-if="states[server.name] === 'connecting'" :disabled="busy || !flows[server.name]" @click="cancel(server)">{{ t('settings.plugins.remote.cancel') }}</button>
      <button v-else-if="states[server.name] === 'connected'" :disabled="busy || !canDisconnect" @click="disconnect">{{ t('settings.plugins.remote.disconnect') }}</button>
      <button v-else :disabled="busy || connecting || states[server.name] === 'connected'" @click="connect(server)">{{ t('settings.plugins.remote.connect') }}</button>
    </div>
    <p v-if="!canDisconnect" class="activation-help">{{ t('settings.plugins.remote.disconnectHint') }}</p>
    <p class="activation-help">{{ t('settings.plugins.remote.scopeHint') }}</p>
  </section>
</template>
<style scoped>
.remote-connection { border: 1px solid var(--border-primary, #1a1d2e); border-radius: 12px; padding: 20px; margin: 20px 0; }
h4 { margin: 0 0 12px; font-weight: 600; }
p { margin: 0 0 16px; color: var(--text-secondary, #c4c8d4); }
.remote-server { display: flex; align-items: center; gap: 16px; justify-content: space-between; }
.remote-server-copy { display: grid; gap: 8px; min-width: 0; }
code { color: var(--text-muted, #8b8fa3); overflow-wrap: anywhere; }
.ready { color: var(--status-success, #10b981); }
.activation-help { margin: 16px 0 0; }
@media (max-width: 640px) { .remote-connection { padding: 16px; } .remote-server { align-items: stretch; flex-direction: column; } }
</style>

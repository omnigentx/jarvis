<script setup>
import { computed, ref } from 'vue'
import { apiFetch } from '../../api'
import { useLang } from '../../composables/useLang'
import { useRealtimeStream } from '../../composables/useRealtimeStream'
import { createPluginInventory } from '../../composables/pluginInventory'

import PluginFilesReview from '../../components/plugins/PluginFilesReview.vue'
import PluginExecutionPolicy from '../../components/plugins/PluginExecutionPolicy.vue'

const { t } = useLang()
const inventory = createPluginInventory(apiFetch)
const { plugins, error: inventoryError, loading } = inventory
useRealtimeStream({ onEvent: inventory.onEvent, onConnected: refresh })
const repo = ref('openai/plugins')
const catalog = ref([])
const query = ref('')
const busy = ref(false)
const error = ref('')
const notice = ref('')
const targets = ref({})
const availableTargets = ref([])
const revisions = ref({})
const review = ref(null)
const filtered = computed(() => catalog.value.filter(item => `${item.name} ${item.description}`.toLowerCase().includes(query.value.toLowerCase())))
const label = status => t(`settings.plugins.status.${status}`)

async function operation(action) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  notice.value = ''
  try { await action() } catch (cause) { error.value = cause.message || t('settings.plugins.failed') }
  finally { busy.value = false }
}
function browse() {
  return operation(async () => { catalog.value = (await apiFetch('/api/plugins/catalog', { method: 'POST', body: JSON.stringify({ repo: repo.value.trim() }) })).plugins })
}
function install(item) {
  return operation(async () => {
    const result = await apiFetch('/api/plugins/install', { method: 'POST', body: JSON.stringify({ repo: item.repo, commit: item.commit || revisions.value[item.name], subdirectory: item.subdirectory }) })
    notice.value = label(result.status)
    await inventory.reload()
  })
}
async function refresh() {
  await inventory.reload()
  try { availableTargets.value = (await apiFetch('/api/plugins/targets')).targets }
  catch (cause) { error.value = cause.message }
}
function targetKey(target) { return target.run_id || target.agent }
function selectedTarget(plugin) {
  return availableTargets.value.find(target => targetKey(target) === targets.value[plugin.id])
}
function supported(plugin) {
  const allowed = plugin.policy_configured ? ['mcp_requires_policy_review', ...(plugin.skills.length ? [] : ['executable_content'])] : []
  return !(plugin.blockers || []).some(blocker => !allowed.includes(blocker))
}
function activate(plugin) {
  return operation(async () => {
    const target = selectedTarget(plugin)
    if (!target) throw new Error(t('settings.plugins.selectTarget'))
    const result = await apiFetch(`/api/plugins/${encodeURIComponent(plugin.id)}/activate`, { method: 'POST', body: JSON.stringify({ agent: target.agent, run_id: target.run_id }) })
    const index = plugins.value.findIndex(item => item.id === result.id)
    if (index !== -1) plugins.value[index] = result
    notice.value = label(result.status)
  })
}
function deactivate(plugin, binding) {
  return operation(async () => {
    await apiFetch(`/api/plugins/${encodeURIComponent(plugin.id)}/deactivate`, { method: 'POST', body: JSON.stringify({ agent: binding.agent_name || binding.agent, run_id: binding.run_id || null }) })
    await inventory.reload()
  })
}
function share(plugin) {
  return operation(async () => {
    const action = plugin.global_enabled ? 'stop-sharing' : 'promote'
    const result = await apiFetch(`/api/plugins/${encodeURIComponent(plugin.id)}/${action}`, { method: 'POST' })
    const index = plugins.value.findIndex(item => item.id === result.id)
    if (index !== -1) plugins.value[index] = result
    notice.value = label(result.status)
  })
}
function checkUpdate(plugin) {
  return operation(async () => {
    repo.value = plugin.repo
    query.value = plugin.name
    catalog.value = (await apiFetch('/api/plugins/catalog', { method: 'POST', body: JSON.stringify({ repo: plugin.repo }) })).plugins
  })
}
function uninstall(plugin) {
  return operation(async () => {
    await apiFetch(`/api/plugins/${encodeURIComponent(plugin.id)}`, { method: 'DELETE' })
    await inventory.reload()
  })
}
function inspect(plugin, skill) {
  return operation(async () => {
    review.value = await apiFetch(`/api/plugins/${encodeURIComponent(plugin.id)}/skills/${encodeURIComponent(skill.name)}/content`)
  })
}
</script>

<template>
  <div class="plugin-panel">
    <p class="intro">{{ t('settings.plugins.intro') }}</p>
    <p class="support-note">{{ t('settings.plugins.support') }}</p>
    <div v-if="error || inventoryError" class="error" role="alert">{{ error || inventoryError }}</div>
    <div v-if="notice" class="notice" role="status">{{ notice }} <RouterLink to="/approvals">{{ t('settings.plugins.approvals') }}</RouterLink></div>
    <section aria-labelledby="plugin-marketplace-title" class="card">
      <h2 id="plugin-marketplace-title">{{ t('settings.plugins.marketplace') }}</h2>
      <form class="controls" @submit.prevent="browse">
        <label>{{ t('settings.plugins.repository') }}<input v-model="repo" list="plugin-sources" autocomplete="off" required maxlength="160" placeholder="owner/repository" /></label>
        <datalist id="plugin-sources"><option value="openai/plugins" /><option value="anthropics/claude-code" /></datalist>
        <button :disabled="busy" type="submit">{{ t('settings.plugins.browse') }}</button>
      </form>
      <label v-if="catalog.length" class="search">{{ t('settings.plugins.search') }}<input v-model="query" type="search" /></label>
      <ul v-if="catalog.length" class="catalog">
        <li v-for="item in filtered" :key="`${item.repo}:${item.subdirectory}:${item.name}`">
          <div class="identity"><strong>{{ item.name }}</strong><p>{{ item.description }}</p><small>{{ item.repo }} · {{ item.subdirectory }}</small></div>
          <div class="actions">
            <label v-if="!item.commit">{{ t('settings.plugins.commit') }}<input v-model="revisions[item.name]" pattern="[a-f0-9]{40}" maxlength="40" /></label>
            <small v-if="item.requires_source_approval">{{ t('settings.plugins.external') }}</small>
            <button :disabled="busy || !(item.commit || /^[a-f0-9]{40}$/.test(revisions[item.name] || ''))" @click="install(item)">{{ t('settings.plugins.add') }}</button>
          </div>
        </li>
      </ul>
    </section>
    <section aria-labelledby="plugin-installed-title">
      <header><h2 id="plugin-installed-title">{{ t('settings.plugins.installed') }}</h2><button :disabled="busy || loading" @click="refresh">{{ t('settings.plugins.refresh') }}</button></header>
      <p v-if="loading">{{ t('common.loading') }}</p>
      <p v-else-if="!plugins.length">{{ t('settings.plugins.empty') }}</p>
      <article v-for="plugin in plugins" :key="plugin.id" class="card" :data-testid="`plugin-${plugin.id}`">
        <header><h3>{{ plugin.name }} <small v-if="plugin.version">{{ plugin.version }}</small></h3><span class="status" :class="{ ready: plugin.status === 'ready' }">{{ label(plugin.status) }}</span></header>
        <p v-if="plugin.global_enabled" class="support-note">{{ t('settings.plugins.sharedHint') }}</p>
        <div class="controls"><button :disabled="busy || (!plugin.global_enabled && (!supported(plugin) || plugin.status === 'expired'))" @click="share(plugin)">{{ t(plugin.global_enabled ? 'settings.plugins.stopSharing' : 'settings.plugins.promote') }}</button><button :disabled="busy" @click="checkUpdate(plugin)">{{ t('settings.plugins.checkUpdate') }}</button></div>
        <button v-if="plugin.status !== 'expired' && !plugin.global_enabled && !(plugin.bindings || []).some(binding => binding.status !== 'disabled')" :disabled="busy" @click="uninstall(plugin)">{{ t('settings.plugins.uninstall') }}</button>
        <p class="source">{{ plugin.repo }} · {{ plugin.commit }}<br />SHA-256: {{ plugin.digest }}</p>
        <ul v-if="plugin.blockers?.length" class="blocked"><li v-for="blocker in plugin.blockers" :key="blocker">{{ blocker }}</li></ul>
        <ul class="skills"><li v-for="skill in plugin.skills" :key="skill.name"><span>{{ skill.name }} — {{ skill.description }}</span><button :disabled="busy" @click="inspect(plugin, skill)">{{ t('settings.plugins.review') }}</button></li></ul>
        <PluginFilesReview :plugin="plugin" />
        <PluginExecutionPolicy v-if="plugin.server_names?.length" :plugin="plugin" @saved="inventory.reload" />
        <ul v-if="plugin.bindings?.length" class="bindings"><li v-for="binding in plugin.bindings" :key="binding.agent"><span>{{ binding.agent_name || binding.agent }}: {{ label(binding.status) }}</span><button v-if="!['disabled','expired'].includes(binding.status)" :disabled="busy" @click="deactivate(plugin, binding)">{{ t('settings.plugins.disable') }}</button></li></ul>
        <div class="controls"><label>{{ t('settings.plugins.target') }}<select v-model="targets[plugin.id]"><option value="" disabled>{{ t('settings.plugins.selectTarget') }}</option><option v-for="target in availableTargets" :key="targetKey(target)" :value="targetKey(target)">{{ target.label }}</option></select></label><button :disabled="busy || !selectedTarget(plugin) || !supported(plugin) || plugin.status === 'expired'" @click="activate(plugin)">{{ t('settings.plugins.activate') }}</button></div>
      </article>
    </section>
    <section v-if="review" class="card" aria-labelledby="plugin-review-title">
      <header><h2 id="plugin-review-title">{{ t('settings.plugins.content') }}</h2><button @click="review = null">{{ t('common.close') }}</button></header>
      <p>{{ t('settings.plugins.untrusted') }}</p>
      <pre tabindex="0">{{ review.content }}</pre>
    </section>
  </div>
</template>

<style scoped>
.plugin-panel { max-width: 1100px; display: grid; gap: 20px; }
.intro, .support-note { margin: 0; color: var(--text-dim); line-height: 1.6; }
.support-note { border-left: 3px solid var(--warning, #f59e0b); padding: 10px 14px; background: var(--bg-1); }
.card { padding: 20px; border: 1px solid var(--border); background: var(--bg-1); border-radius: var(--r-md); margin-bottom: 12px; min-width: 0; }
h2, h3 { margin: 0 0 12px; font-size: 16px; font-weight: 600; }
header { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; flex-wrap: wrap; }
.controls { display: flex; gap: 12px; align-items: end; flex-wrap: wrap; }
label { display: grid; gap: 6px; color: var(--text-dim); font-size: 13px; flex: 1; min-width: 0; }
input, select { width: 100%; box-sizing: border-box; padding: 11px 12px; border: 1px solid var(--border); background: var(--bg-0); color: var(--text); border-radius: var(--r-md); font: inherit; min-height: 44px; }
input:focus-visible, select:focus-visible, button:focus-visible, pre:focus-visible { outline: 2px solid var(--primary); outline-offset: 3px; }
button { min-height: 44px; border: 1px solid var(--border); padding: 10px 16px; border-radius: var(--r-md); background: var(--primary-bg-strong); color: var(--text); cursor: pointer; white-space: nowrap; }
button:disabled { opacity: .5; cursor: default; }
.search { margin-top: 18px; }
ul { list-style: none; padding: 0; }
.catalog { max-height: 480px; overflow-y: auto; }
.catalog li, .skills li, .bindings li { padding: 14px 0; border-top: 1px solid var(--border); display: flex; justify-content: space-between; gap: 16px; }
.identity { min-width: 0; overflow-wrap: anywhere; }
.identity p { margin: 6px 0; color: var(--text-dim); font-size: 13px; }
small, .source { color: var(--text-muted); overflow-wrap: anywhere; font-size: 12px; }
.actions { display: grid; gap: 8px; align-content: center; flex-shrink: 0; max-width: 240px; }
.status { padding: 4px 8px; background: var(--bg-0); border: 1px solid var(--border); border-radius: var(--r-md); font-size: 12px; }
.status.ready { color: #10b981; }
.blocked, .error { color: #ef4444; }
.notice { color: var(--text); padding: 12px; border: 1px solid var(--border); border-radius: var(--r-md); }
.notice a { color: var(--primary); margin-left: 8px; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 480px; overflow-y: auto; font: 13px var(--font-mono); line-height: 1.6; }
@media (max-width: 600px) { .card { padding: 14px; } .catalog li, .skills li { flex-direction: column; } .actions { max-width: 100%; } .controls { align-items: stretch; flex-direction: column; } }
</style>

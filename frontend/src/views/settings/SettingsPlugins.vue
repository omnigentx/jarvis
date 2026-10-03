<script setup>
import { computed, ref } from 'vue'
import { apiFetch } from '../../api'
import { useLang } from '../../composables/useLang'
import { useRealtimeStream } from '../../composables/useRealtimeStream'
import { createPluginInventory } from '../../composables/pluginInventory'
import { ALL_CURRENT_TARGETS, targetKey, blockingCapabilities, activationTargets, activateReviewedTargets } from '../../composables/pluginActivation'

import PluginActivationReview from '../../components/plugins/PluginActivationReview.vue'
import PluginSourceBadge from '../../components/plugins/PluginSourceBadge.vue'
import PluginSourceReview from '../../components/plugins/PluginSourceReview.vue'

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
const sourceReview = ref(null)
const activationReview = ref(null)
const activationResults = ref({})
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
    const result = await apiFetch('/api/plugins/install', { method: 'POST', body: JSON.stringify({ repo: item.repo, commit: item.commit || revisions.value[item.name], subdirectory: item.subdirectory, source_confirmed: true }) })
    notice.value = result.status
    await inventory.reload()
  })
}
async function refresh() {
  await inventory.reload()
  for (const plugin of plugins.value) targets.value[plugin.id] ??= ''
  try { availableTargets.value = (await apiFetch('/api/plugins/targets')).targets }
  catch (cause) { error.value = cause.message }
}
function selectedTargets(plugin) {
  return activationTargets(targets.value[plugin.id], availableTargets.value)
}
function supported(plugin) { return blockingCapabilities(plugin).length === 0 }
function blockerLabel(blocker) {
  const key = `settings.plugins.activation.blockers.${blocker}`
  const translated = t(key)
  return translated === key ? t('settings.plugins.activation.unsupported', { capability: blocker }) : translated
}
function activationReason(plugin) {
  if (plugin.status === 'expired') return t('settings.plugins.activation.expired')
  if (!supported(plugin)) return t('settings.plugins.activation.blockedHint')
  if (!availableTargets.value.length) return t('settings.plugins.activation.noTargets')
  if (!selectedTargets(plugin).length) return t('settings.plugins.selectTarget')
  return t('settings.plugins.activation.reviewHint')
}
function prepareActivation(plugin) {
  return operation(async () => {
    const chosen = selectedTargets(plugin)
    if (!chosen.length) throw new Error(t('settings.plugins.selectTarget'))
    const reviews = []
    // Resolve and sign every selected runtime before showing the exact scope.
    // A failed review never causes a partial, unreviewed activation.
    for (const target of chosen) {
      const reviewed = await apiFetch(`/api/plugins/${encodeURIComponent(plugin.id)}/manual-review`, { method: 'POST', body: JSON.stringify({ agent: target.agent, run_id: target.run_id }) })
      if (reviews.length && (reviewed.plugin.digest !== reviews[0].plugin.digest || JSON.stringify(reviewed.execution_policy) !== JSON.stringify(reviews[0].execution_policy))) {
        throw new Error(t('settings.plugins.activation.reviewChanged'))
      }
      reviews.push({ ...reviewed, target_label: target.label })
    }
    activationReview.value = { ...reviews[0], reviews, allCurrent: targets.value[plugin.id] === ALL_CURRENT_TARGETS }
  })
}
function confirmActivation() {
  const reviewed = activationReview.value
  activationReview.value = null
  return operation(async () => {
    const id = reviewed.plugin.id
    activationResults.value[id] = []
    await activateReviewedTargets(reviewed.reviews, review => apiFetch(`/api/plugins/${encodeURIComponent(id)}/manual-activate`, { method:'POST', body:JSON.stringify({agent:review.target,run_id:review.run_id,review_token:review.review_token}) }), results => { activationResults.value[id] = results })
    await inventory.reload()
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
    notice.value = result.status
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
    <details class="support-details"><summary>{{ t('settings.plugins.capabilities') }}</summary><p>{{ t('settings.plugins.support') }}</p></details>
    <div v-if="error || inventoryError" class="error" role="alert">{{ error || inventoryError }}</div>
    <div v-if="notice" class="notice" role="status">{{ label(notice) }} <RouterLink v-if="['needs_source_approval', 'needs_global_approval'].includes(notice)" to="/approvals">{{ t('settings.plugins.approvals') }}</RouterLink></div>
    <section aria-labelledby="plugin-marketplace-title" class="card">
      <h2 id="plugin-marketplace-title">{{ t('settings.plugins.marketplace') }}</h2>
      <form class="controls" @submit.prevent="browse">
        <label>{{ t('settings.plugins.repository') }}<input v-model="repo" list="plugin-sources" autocomplete="off" required maxlength="160" placeholder="owner/repository" /></label>
        <datalist id="plugin-sources"><option value="openai/plugins" /><option value="anthropics/claude-code" /></datalist>
        <button class="primary-action" :disabled="busy" type="submit">{{ t('settings.plugins.browse') }}</button>
      </form>
      <label v-if="catalog.length" class="search">{{ t('settings.plugins.search') }}<input v-model="query" type="search" /></label>
      <ul v-if="catalog.length" class="catalog">
        <li v-for="item in filtered" :key="`${item.repo}:${item.subdirectory}:${item.name}`">
          <div class="identity"><PluginSourceBadge :source="item.source_origin" /><strong>{{ item.name }}</strong><p>{{ item.description }}</p><small>{{ item.repo }} · {{ item.subdirectory }}</small></div>
          <div class="actions">
            <label v-if="!item.commit">{{ t('settings.plugins.commit') }}<input v-model="revisions[item.name]" pattern="[a-f0-9]{40}" maxlength="40" /></label>
            <button :disabled="busy || !(item.commit || /^[a-f0-9]{40}$/.test(revisions[item.name] || ''))" @click="sourceReview = { ...item, commit: item.commit || revisions[item.name] }">{{ t('settings.plugins.add') }}</button>
          </div>
        </li>
      </ul>
    </section>
    <section aria-labelledby="plugin-installed-title">
      <header class="section-heading"><h2 id="plugin-installed-title">{{ t('settings.plugins.installed') }} <span class="count">{{ plugins.length }}</span></h2><button :disabled="busy || loading" @click="refresh">{{ t('settings.plugins.refresh') }}</button></header>
      <p v-if="loading">{{ t('common.loading') }}</p>
      <p v-else-if="!plugins.length">{{ t('settings.plugins.empty') }}</p>
      <article v-for="plugin in plugins" :key="plugin.id" class="card" :data-testid="`plugin-${plugin.id}`">
        <header><h3>{{ plugin.name }} <small v-if="plugin.version">{{ plugin.version }}</small></h3><span class="status" :class="{ ready: plugin.status === 'ready' && supported(plugin) }">{{ plugin.status !== 'expired' && !supported(plugin) ? t('settings.plugins.activation.blockedStatus') : label(plugin.status) }}</span></header>
        <p v-if="plugin.global_enabled" class="support-note">{{ t('settings.plugins.sharedHint') }}</p>
        <PluginSourceBadge :source="plugin.source_origin" />
        <p class="repository-name">{{ plugin.repo }}</p>
        <details class="source-details"><summary>{{ t('settings.plugins.sourceDetails') }}</summary><dl><dt>{{ t('settings.plugins.sourceCommit') }}</dt><dd>{{ plugin.commit }}</dd><dt>SHA-256</dt><dd>{{ plugin.digest }}</dd></dl></details>
        <div v-if="!supported(plugin)" class="blocked" role="note"><strong>{{ t('settings.plugins.activation.blockedTitle') }}</strong><ul><li v-for="blocker in blockingCapabilities(plugin)" :key="blocker">{{ blockerLabel(blocker) }}</li></ul></div>
        <ul class="skills"><li v-for="skill in plugin.skills" :key="skill.name"><div class="skill-copy"><strong>{{ skill.name }}</strong><p>{{ skill.description }}</p></div><button :disabled="busy" @click="inspect(plugin, skill)">{{ t('settings.plugins.review') }}</button></li></ul>
        <PluginFilesReview :plugin="plugin" />
        <PluginExecutionPolicy v-if="plugin.server_names?.length" :plugin="plugin" @saved="inventory.reload" />
        <ul v-if="plugin.bindings?.length" class="bindings"><li v-for="binding in plugin.bindings" :key="binding.agent"><div class="binding-copy"><strong>{{ binding.agent_name || binding.agent }}</strong><span class="binding-status" :class="{ ready: binding.status === 'ready' }">{{ label(binding.status) }}</span></div><button v-if="!['disabled','expired'].includes(binding.status)" :disabled="busy" @click="deactivate(plugin, binding)">{{ t('settings.plugins.disable') }}</button></li></ul>
        <div class="controls activation"><label>{{ t('settings.plugins.target') }}<select v-model="targets[plugin.id]" :disabled="busy" :aria-describedby="`activation-help-${plugin.id}`"><option value="" disabled>{{ t('settings.plugins.selectTarget') }}</option><option v-if="availableTargets.length" :value="ALL_CURRENT_TARGETS">{{ t('settings.plugins.activation.allCurrent', { count: activationTargets(ALL_CURRENT_TARGETS, availableTargets).length }) }}</option><option v-for="target in availableTargets" :key="targetKey(target)" :value="targetKey(target)">{{ target.label }}</option></select></label><button class="primary-action" :disabled="busy || !selectedTargets(plugin).length || !supported(plugin) || plugin.status === 'expired'" @click="prepareActivation(plugin)" :aria-describedby="`activation-help-${plugin.id}`">{{ t('settings.plugins.activate') }}</button></div>
        <p class="activation-help" :id="`activation-help-${plugin.id}`">{{ activationReason(plugin) }}</p>
        <ul v-if="activationResults[plugin.id]?.length" class="activation-results" aria-live="polite"><li v-for="result in activationResults[plugin.id]" :key="result.run_id || result.target"><strong>{{ result.label }}</strong><span :class="{ ready: result.status === 'ready' }">{{ label(result.status) }}</span><p v-if="result.error">{{ result.error }}</p></li></ul>
        <footer class="package-actions">
          <button :disabled="busy" @click="checkUpdate(plugin)">{{ t('settings.plugins.checkUpdate') }}</button>
          <button :disabled="busy || (!plugin.global_enabled && (!supported(plugin) || plugin.status === 'expired'))" @click="share(plugin)">{{ t(plugin.global_enabled ? 'settings.plugins.stopSharing' : 'settings.plugins.promote') }}</button>
          <button class="danger-action" v-if="plugin.status !== 'expired' && !plugin.global_enabled && !(plugin.bindings || []).some(binding => binding.status !== 'disabled')" :disabled="busy" @click="uninstall(plugin)">{{ t('settings.plugins.uninstall') }}</button>
        </footer>
      </article>
    </section>
    <PluginActivationReview v-if="activationReview" :review="activationReview" @close="activationReview = null" @confirm="confirmActivation" />
    <PluginSourceReview v-if="sourceReview" :plugin="sourceReview" @close="sourceReview = null" @confirm="install(sourceReview); sourceReview = null" />
    <section v-if="review" class="card" aria-labelledby="plugin-review-title">
      <header><h2 id="plugin-review-title">{{ t('settings.plugins.content') }}</h2><button @click="review = null">{{ t('common.close') }}</button></header>
      <p>{{ t('settings.plugins.untrusted') }}</p>
      <pre tabindex="0">{{ review.content }}</pre>
    </section>
  </div>
</template>

<style src="/src/components/plugins/plugin-ui.css"></style>

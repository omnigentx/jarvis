<script setup>
import { computed, onMounted, ref } from 'vue'
import { useLang } from '../../composables/useLang'
import PluginSourceBadge from './PluginSourceBadge.vue'
const props = defineProps({ plugin: { type: Object, required: true } })
const emit = defineEmits(['confirm', 'close'])
const { t } = useLang()
const dialog = ref(null)
const accepted = ref(false)
const official = computed(() => props.plugin.source_origin?.kind === 'official')
onMounted(() => dialog.value.showModal())
</script>
<template>
  <dialog ref="dialog" class="source-review" aria-labelledby="plugin-source-review-title" @close="emit('close')">
    <form method="dialog" @submit.prevent="emit('confirm')">
      <header><h2 id="plugin-source-review-title">{{ t('settings.plugins.source.reviewTitle') }}</h2><button type="button" @click="dialog.close()">{{ t('common.close') }}</button></header>
      <PluginSourceBadge :source="plugin.source_origin" /><h3>{{ plugin.name }}</h3>
      <dl><dt>{{ t('settings.plugins.repository') }}</dt><dd>{{ plugin.repo }}</dd><dt>{{ t('settings.plugins.sourceCommit') }}</dt><dd>{{ plugin.commit }}</dd><dt>{{ t('settings.plugins.source.path') }}</dt><dd>{{ plugin.subdirectory || '/' }}</dd></dl>
      <p class="source-advice" :class="{ 'source-warning': !official }">{{ t(official ? 'settings.plugins.source.officialHint' : 'settings.plugins.source.warning') }}</p>
      <label v-if="!official" class="source-consent"><input v-model="accepted" type="checkbox" />{{ t('settings.plugins.source.acceptRisk') }}</label>
      <p class="source-next-step">{{ t('settings.plugins.source.nextStep') }}</p>
      <footer><button type="button" @click="dialog.close()">{{ t('settings.plugins.source.cancel') }}</button><button type="submit" class="primary-action" :disabled="!official && !accepted">{{ t('settings.plugins.source.continue') }}</button></footer>
    </form>
  </dialog>
</template>

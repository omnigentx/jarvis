<script setup>
import { onMounted, ref } from 'vue'
import { useLang } from '../../composables/useLang'
import PluginSourceBadge from './PluginSourceBadge.vue'
import PluginFilesReview from './PluginFilesReview.vue'
const props = defineProps({ review: { type:Object, required:true } })
const emit = defineEmits(['confirm','close'])
const { t } = useLang()
const dialog = ref(null)
const accepted = ref(false)
onMounted(()=>dialog.value.showModal())
</script>
<template>
  <dialog ref="dialog" class="source-review" aria-labelledby="activation-review-title" @close="emit('close')">
    <div class="review-body">
      <header><h2 id="activation-review-title">{{ t('settings.plugins.source.activationTitle') }}</h2><button type="button" @click="dialog.close()">{{ t('common.close') }}</button></header>
      <PluginSourceBadge :source="review.plugin.source_origin" />
      <h3>{{ review.plugin.name }}</h3>
      <p v-if="review.allCurrent" class="source-advice">{{ t('settings.plugins.activation.currentScope') }}</p>
      <ul class="review-targets"><li v-for="target in (review.reviews || [review])" :key="target.run_id || target.target">{{ target.target_label || target.target }}</li></ul>
      <dl><dt>SHA-256</dt><dd>{{ review.plugin.digest }}</dd><dt>{{ t('settings.plugins.source.capabilities') }}</dt><dd>{{ [...review.plugin.skills.map(skill=>skill.name), ...(review.plugin.server_names || [])].join(', ') || '—' }}</dd></dl>
      <p class="source-advice">{{ t('settings.plugins.source.activationHint') }}</p>
      <PluginFilesReview :plugin="review.plugin" />
      <pre v-if="review.execution_policy">{{ JSON.stringify(review.execution_policy, null, 2) }}</pre>
      <label class="source-consent"><input v-model="accepted" type="checkbox" />{{ t('settings.plugins.source.confirmActivation') }}</label>
      <footer><button type="button" @click="dialog.close()">{{ t('settings.plugins.source.cancel') }}</button><button type="button" @click="emit('confirm')" class="primary-action" :disabled="!accepted">{{ t('settings.plugins.source.activateNow') }}</button></footer>
    </div>
  </dialog>
</template>

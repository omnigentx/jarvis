<script setup>
import { computed } from 'vue'
import { useLang } from '../../composables/useLang'
const props = defineProps({ source: { type: Object, default: null } })
const { t } = useLang()
const kind = computed(() => ['official', 'community'].includes(props.source?.kind) ? props.source.kind : 'external')
</script>
<template>
  <span class="source-badge" :class="`source-${kind}`"><span aria-hidden="true">{{ kind === 'official' ? '✓' : '!' }}</span>{{ t(`settings.plugins.source.${kind}`) }}<span v-if="kind === 'official' && source?.publisher">· {{ source.publisher }}</span><span v-if="kind === 'community' && source?.marketplace">· {{ source.marketplace }}</span></span>
</template>

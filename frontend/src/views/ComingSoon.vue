<script setup>
import { useRoute, useRouter } from 'vue-router'
import { useLang } from '../composables/useLang'

const { t } = useLang()
const route = useRoute()
const router = useRouter()

function goBack() {
  if (window.history.length > 1) router.back()
  else router.push('/')
}
</script>

<template>
  <div class="coming-soon jv">
    <div class="coming-soon__inner">
      <div class="eyebrow">{{ t('comingSoon.eyebrow') }}</div>
      <h1 class="coming-soon__title">
        <span class="grad" style="font-style: italic;">{{ t('comingSoon.titleLead') }}</span> {{ t('comingSoon.titleRest') }}
      </h1>
      <p class="coming-soon__body">
        {{ t('comingSoon.bodyLead') }}
        <strong>{{ route.meta.title || t('comingSoon.feature') }}</strong>
        {{ t('comingSoon.bodyRest') }}
      </p>
      <button class="btn btn-secondary coming-soon__back" @click="goBack">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M19 12H5"/><polyline points="12 19 5 12 12 5"/>
        </svg>
        {{ t('common.back') }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.coming-soon {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 60vh;
  padding: 40px 24px;
  color: var(--text);
}
.coming-soon__inner {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 12px;
  max-width: 480px;
  padding: 28px;
  background: var(--bg-1);
  border: 1px solid var(--border);
  border-radius: var(--r-lg);
}
.coming-soon__title {
  font-family: var(--font-display);
  font-size: 28px;
  letter-spacing: -0.02em;
  margin: 0;
}
.coming-soon__body {
  font-size: 13.5px;
  color: var(--text-dim);
  line-height: 1.55;
}
.coming-soon__back {
  margin-top: 6px;
}
</style>

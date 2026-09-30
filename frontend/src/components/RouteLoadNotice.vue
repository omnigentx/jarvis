<script setup>
import { routeRecovery } from '../router.js'
import { useLang } from '../composables/useLang'

const { t } = useLang()
const { failure, reload, dismiss } = routeRecovery
</script>

<template>
  <Teleport to="body">
    <section v-if="failure" class="route-load-notice" role="alert" aria-labelledby="route-load-title">
      <h2 id="route-load-title">{{ t('routeRecovery.title') }}</h2>
      <p>{{ t('routeRecovery.message') }}</p>
      <div class="route-load-actions">
        <button type="button" @click="dismiss">{{ t('routeRecovery.dismiss') }}</button>
        <button type="button" class="reload" @click="reload">{{ t('routeRecovery.reload') }}</button>
      </div>
    </section>
  </Teleport>
</template>

<style scoped>
.route-load-notice {
  position: fixed;
  z-index: 10002;
  top: max(16px, env(safe-area-inset-top));
  right: 16px;
  width: min(440px, calc(100vw - 32px));
  box-sizing: border-box;
  max-height: calc(100dvh - 32px);
  overflow-y: auto;
  padding: 20px;
  border: 1px solid var(--border-bright);
  border-radius: var(--r-lg);
  background: var(--bg-2);
  color: var(--text);
  box-shadow: var(--shadow-lg);
  font-family: var(--font-body);
}
h2 { font-size: 16px; font-weight: 600; margin: 0 0 8px; }
p { font-size: 14px; line-height: 1.5; color: var(--text-dim); margin: 0 0 16px; }
.route-load-actions { display: flex; flex-wrap: wrap; gap: 8px; }
button {
  flex: 1;
  min-height: 44px;
  padding: 8px 12px;
  border: 1px solid var(--border-bright);
  border-radius: var(--r-md);
  background: var(--bg-3);
  color: var(--text);
  cursor: pointer;
}
button.reload { background: var(--primary); color: white; }
button:focus-visible { outline: 2px solid var(--primary); outline-offset: 3px; }
</style>

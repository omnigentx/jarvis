<script setup>
/**
 * Step body shell — the panel inside the browser-window chrome.
 * Renders a primary-tinted container with an eyebrow + content + footer.
 *
 * Footer is rendered by the parent SetupWizard.vue (so it sits at the bottom
 * of the browser chrome, not inside the scrolling body), so here we only
 * expose `#footer-left` / `#footer-right` slots that parents teleport into
 * via the shared event bus. To keep the existing component API stable
 * (each StepXxx still uses `<template #footer-left>` / `<template #footer-right>`),
 * we render footer slots inline as a card-footer at the bottom of THIS shell.
 */
defineProps({
  title: { type: String, required: true },
  subtitle: { type: String, default: '' },
  stepLabel: { type: String, default: '' },
  width: { type: String, default: '720px' },
})
</script>

<template>
  <section class="wizard-card-frame" :style="{ maxWidth: width }">
    <div v-if="stepLabel" class="eyebrow">{{ stepLabel }}</div>
    <header class="card-heading">
      <h1>{{ title }}</h1>
      <p v-if="subtitle">{{ subtitle }}</p>
    </header>
    <div class="card-body">
      <slot />
    </div>
    <footer class="card-footer">
      <div class="left">
        <slot name="footer-left" />
      </div>
      <div class="right">
        <slot name="footer-right" />
      </div>
    </footer>
  </section>
</template>

<style scoped>
.wizard-card-frame {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 18px;
  background: var(--primary-bg);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-lg);
  padding: 28px 32px 24px;
}
.eyebrow {
  font-family: var(--font-mono);
  font-size: 10.5px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--primary-hover);
}
.card-heading { display: flex; flex-direction: column; gap: 6px; }
.card-heading h1 {
  font-family: var(--font-display);
  font-size: 22px;
  font-weight: 600;
  letter-spacing: -0.02em;
  color: var(--text);
  line-height: 1.2;
  margin: 0;
}
.card-heading p {
  font-family: var(--font-body);
  font-size: 13.5px;
  color: var(--text-dim);
  line-height: 1.55;
  max-width: 560px;
  margin: 0;
}
.card-body { display: flex; flex-direction: column; gap: 16px; }
.card-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding-top: 16px;
  margin-top: 4px;
  border-top: 1px solid var(--border);
}
.card-footer .left { display: flex; gap: 8px; }
.card-footer .right { display: flex; gap: 8px; }
</style>

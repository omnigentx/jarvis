<script setup>
/**
 * Card + header + footer shell used by every wizard step.  Keeps the visual
 * rhythm identical across steps so the user's eye doesn't jump.
 */
defineProps({
  title: { type: String, required: true },
  subtitle: { type: String, default: '' },
  stepLabel: { type: String, default: '' },
  width: { type: String, default: '700px' },
})
</script>

<template>
  <section class="wizard-card-frame" :style="{ maxWidth: width }">
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
      <div class="center">
        <span v-if="stepLabel" class="step-label">{{ stepLabel }}</span>
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
  gap: 24px;
  background: #111827;
  border: 1px solid #243244;
  border-radius: 16px;
  padding: 40px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.25);
}
.card-heading { text-align: center; display: flex; flex-direction: column; gap: 10px; }
.card-heading h1 {
  font-size: 24px;
  font-weight: 700;
  color: #e5eef8;
  line-height: 1.2;
}
.card-heading p {
  font-size: 14px;
  color: #94a3b8;
  line-height: 1.5;
  max-width: 500px;
  margin: 0 auto;
}
.card-body { display: flex; flex-direction: column; gap: 20px; }
.card-footer {
  display: grid;
  grid-template-columns: 1fr auto 1fr;
  align-items: center;
  gap: 16px;
  padding-top: 24px;
  border-top: 1px solid #243244;
}
.card-footer .left { justify-self: start; display: flex; gap: 8px; }
.card-footer .right { justify-self: end; display: flex; gap: 8px; }
.step-label {
  font-size: 13px;
  color: #64748b;
  white-space: nowrap;
}
</style>

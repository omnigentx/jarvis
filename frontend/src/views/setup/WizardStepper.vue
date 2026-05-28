<script setup>
/**
 * Progress strip — 5 equal columns, each with a 3px bar + small status mark
 * + UPPERCASE mono label. States:
 *   - done    → green bar, white checkmark in green circle
 *   - active  → indigo bar with shimmer keyframe, mono number in primary circle
 *   - pending → bg-4 bar, mono number with muted border, text-subtle label
 *
 * Pure presentation — takes the same `steps` + `current` props the old
 * stepper did, so the parent component contract is unchanged.
 */
import { computed } from 'vue'

const props = defineProps({
  steps: { type: Array, required: true }, // [{name, completed, skipped}]
  current: { type: String, default: null },
  labels: {
    type: Object,
    default: () => ({
      auth: 'AUTH',
      llm: 'LLM',
      services: 'SERVICES',
      yaml_config: 'YAML',
      verify: 'VERIFY',
    }),
  },
})

const order = ['auth', 'llm', 'services', 'yaml_config', 'verify']

const decorated = computed(() =>
  order.map((name, idx) => {
    const s = props.steps.find((x) => x.name === name)
    const done = !!(s?.completed || s?.skipped)
    const active = props.current === name
    return {
      name,
      number: idx + 1,
      label: props.labels[name] || name,
      done,
      active,
      pending: !done && !active,
    }
  }),
)
</script>

<template>
  <div class="progress-strip">
    <div
      v-for="step in decorated"
      :key="step.name"
      class="step-col"
      :class="{ done: step.done, active: step.active, pending: step.pending }"
    >
      <div class="bar">
        <div v-if="step.active" class="shimmer" aria-hidden="true" />
      </div>
      <div class="meta">
        <span class="badge">
          <svg
            v-if="step.done"
            width="11"
            height="11"
            viewBox="0 0 16 16"
            aria-hidden="true"
          >
            <path
              d="M3 8.5l3 3L13 4.5"
              fill="none"
              stroke="currentColor"
              stroke-width="2.4"
              stroke-linecap="round"
              stroke-linejoin="round"
            />
          </svg>
          <span v-else>{{ step.number }}</span>
        </span>
        <span class="label">{{ step.label }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.progress-strip {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 12px;
  padding: 20px 32px 22px;
  border-bottom: 1px solid var(--border);
  background: var(--bg-1);
}
.step-col {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
}
.bar {
  height: 3px;
  border-radius: 2px;
  background: var(--bg-4);
  position: relative;
  overflow: hidden;
}
.step-col.done .bar { background: var(--success); }
.step-col.active .bar {
  background: var(--primary);
  box-shadow: 0 0 12px var(--primary-glow);
}
.shimmer {
  position: absolute;
  inset: 0;
  background: linear-gradient(
    90deg,
    transparent,
    rgba(255, 255, 255, 0.6),
    transparent
  );
  animation: shimmer 1.8s ease-in-out infinite;
}

.meta {
  display: flex;
  align-items: center;
  gap: 8px;
}
.badge {
  width: 22px;
  height: 22px;
  border-radius: 50%;
  display: grid;
  place-items: center;
  font-family: var(--font-mono);
  font-size: 10px;
  font-weight: 600;
  color: #ffffff;
}
.step-col.done .badge { background: var(--success); }
.step-col.active .badge { background: var(--primary); }
.step-col.pending .badge {
  background: var(--bg-3);
  color: var(--text-muted);
  border: 1px solid var(--border-strong);
}
.label {
  font-family: var(--font-mono);
  font-size: 10.5px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.step-col.done .label { color: var(--text); }
.step-col.active .label { color: var(--text); }
.step-col.pending .label { color: var(--text-subtle); }

@keyframes shimmer {
  0%   { transform: translateX(-100%); }
  100% { transform: translateX(100%); }
}
</style>

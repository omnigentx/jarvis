<script setup>
/**
 * Horizontal progress stepper (Figma: 800×60, 5 steps, connector lines).
 *
 * Renders a circle per step.  States:
 *   - done    → filled check, green
 *   - active  → filled accent blue, number
 *   - pending → outlined, muted number
 *
 * The connector bar between nodes turns green once the LEFT step is done.
 */
import { computed } from 'vue'

const props = defineProps({
  steps: { type: Array, required: true }, // [{name, completed, skipped}]
  current: { type: String, default: null },
  labels: {
    type: Object,
    default: () => ({
      auth: 'Auth',
      llm: 'LLM Provider',
      services: 'Services',
      yaml_config: 'YAML Config',
      verify: 'Verify',
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
    }
  }),
)
</script>

<template>
  <div class="wizard-stepper">
    <template v-for="(step, idx) in decorated" :key="step.name">
      <div
        v-if="idx > 0"
        class="connector"
        :class="{ done: decorated[idx - 1].done }"
      />
      <div class="step-node" :class="{ done: step.done, active: step.active }">
        <div class="circle">
          <svg v-if="step.done" width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">
            <path
              d="M3 8.5l3 3L13 4.5"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
            />
          </svg>
          <span v-else>{{ step.number }}</span>
        </div>
        <span class="label">{{ step.label }}</span>
      </div>
    </template>
  </div>
</template>

<style scoped>
.wizard-stepper {
  display: flex;
  align-items: center;
  width: 100%;
  max-width: 800px;
  margin: 0 auto;
  gap: 0;
}
.step-node {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.step-node .circle {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  background: transparent;
  border: 1.5px solid #243244;
  color: #64748b;
  display: grid;
  place-items: center;
  font-size: 13px;
  font-weight: 700;
  transition: background 0.2s, color 0.2s, border-color 0.2s;
}
.step-node.active .circle {
  background: #5b8cff;
  border-color: #5b8cff;
  color: #ffffff;
  box-shadow: 0 0 0 4px rgba(91, 140, 255, 0.15);
}
.step-node.done .circle {
  background: #22c55e;
  border-color: #22c55e;
  color: #ffffff;
}
.step-node .label {
  font-size: 11px;
  color: #64748b;
  font-weight: 400;
  white-space: nowrap;
}
.step-node.active .label {
  color: #e5eef8;
  font-weight: 600;
}
.step-node.done .label {
  color: #22c55e;
}
.connector {
  flex: 1 1 auto;
  height: 2px;
  background: #243244;
  margin: 0 8px;
  margin-bottom: 21px; /* aligns with circle center (32/2 + label height + gap) */
  transition: background 0.2s;
}
.connector.done {
  background: #22c55e;
}
</style>

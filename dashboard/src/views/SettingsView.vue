<script setup>
/**
 * Settings — entry point for the post-setup configuration surface.
 *
 * Ships in Phase 1f with the "General" tab only; LLM Provider, Services
 * and YAML tabs stub to the roadmap so the navigation structure is in
 * place but the Phase 2/3 work doesn't block the initial release.
 */
import { ref } from 'vue'
import SettingsGeneral from './settings/SettingsGeneral.vue'
import SettingsYaml from './settings/SettingsYaml.vue'
import SettingsServices from './settings/SettingsServices.vue'
import SettingsLLM from './settings/SettingsLLM.vue'
import SettingsVoice from './settings/SettingsVoice.vue'
import SettingsExperimental from './settings/SettingsExperimental.vue'

const TABS = [
  { id: 'general', label: 'General' },
  { id: 'llm', label: 'LLM Provider' },
  { id: 'voice', label: 'Voice' },
  { id: 'services', label: 'Services' },
  { id: 'yaml', label: 'YAML Config' },
  { id: 'experimental', label: 'Experimental' },
]
const active = ref('general')
</script>

<template>
  <div class="settings-page">
    <header class="page-header">
      <h1>Settings</h1>
      <p>Manage your Jarvis configuration, secrets, and connected services.</p>
    </header>

    <nav class="tabs">
      <button
        v-for="t in TABS"
        :key="t.id"
        type="button"
        class="tab"
        :class="{ active: active === t.id }"
        @click="active = t.id"
      >
        {{ t.label }}
      </button>
    </nav>

    <section class="panel">
      <SettingsGeneral v-if="active === 'general'" />
      <SettingsLLM v-else-if="active === 'llm'" />
      <SettingsVoice v-else-if="active === 'voice'" />
      <SettingsYaml v-else-if="active === 'yaml'" />
      <SettingsServices v-else-if="active === 'services'" />
      <SettingsExperimental v-else-if="active === 'experimental'" />
      <div v-else class="placeholder">
        <p>
          <strong>{{ TABS.find((t) => t.id === active).label }}</strong> —
          coming in a follow-up release.
        </p>
        <p class="sub">
          The API is already live; the UI for this tab lands alongside the
          related backend work (Phase 3a).
        </p>
      </div>
    </section>
  </div>
</template>

<style scoped>
.settings-page {
  display: flex;
  flex-direction: column;
  gap: 24px;
  padding: 32px 40px;
  max-width: 1200px;
}
.page-header h1 {
  font-size: 26px;
  font-weight: 700;
  color: var(--text-primary, #f0f2f5);
}
.page-header p {
  margin-top: 6px;
  font-size: 14px;
  color: var(--text-nav, #8b8fa3);
}
.tabs {
  display: flex;
  gap: 4px;
  border-bottom: 1px solid var(--border, #1e2030);
}
.tab {
  padding: 12px 20px;
  background: transparent;
  border: none;
  border-bottom: 2px solid transparent;
  font-family: inherit;
  font-size: 14px;
  font-weight: 500;
  color: var(--text-nav, #8b8fa3);
  cursor: pointer;
  transition: color 0.15s, border-color 0.15s;
}
.tab:hover { color: var(--text-secondary, #c4c8d4); }
.tab.active {
  color: var(--accent-blue, #3b82f6);
  border-bottom-color: var(--accent-blue, #3b82f6);
  font-weight: 600;
}
.panel { padding-top: 8px; }
.placeholder {
  padding: 40px;
  background: var(--bg-card, #111318);
  border: 1px dashed var(--border, #1e2030);
  border-radius: 12px;
  color: var(--text-nav, #8b8fa3);
  text-align: center;
}
.placeholder strong { color: var(--text-primary, #f0f2f5); }
.placeholder .sub { margin-top: 8px; font-size: 13px; }

@media (max-width: 720px) {
  .settings-page { padding: 20px 16px; }
  .tabs { overflow-x: auto; }
}
</style>

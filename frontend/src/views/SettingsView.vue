<script setup>
/**
 * Settings shell — left sidebar nav + main content area.
 *
 * Renders each tab via v-if so per-tab state (form drafts, fetched
 * registries) lives only while the user is on that tab. The active tab
 * is purely local state — the router only has a single /settings route,
 * so deep-linking to a specific tab is intentionally out of scope here.
 *
 * Visual spec: DESIGN_HANDOFF §6 sidebar — section header in uppercase
 * mono, active item has primary-bg background + 2px primary left border.
 */
import { ref } from 'vue'
import { useLang } from '../composables/useLang'
import SettingsGeneral from './settings/SettingsGeneral.vue'
import SettingsAuth from './settings/SettingsAuth.vue'
import SettingsYaml from './settings/SettingsYaml.vue'
import SettingsRunningTemplates from './settings/SettingsRunningTemplates.vue'
import SettingsServices from './settings/SettingsServices.vue'
import SettingsLLM from './settings/SettingsLLM.vue'
import SettingsVoice from './settings/SettingsVoice.vue'
import SettingsCompaction from './settings/SettingsCompaction.vue'
import SettingsMemory from './settings/SettingsMemory.vue'
import SettingsExperimental from './settings/SettingsExperimental.vue'

const { t } = useLang()

// eyebrow is the stable grouping id; its display text is resolved via the
// settings.shell.group.* keys (see locales) so section headers translate too.
const TABS = [
  { id: 'general',      labelKey: 'settings.shell.tab.general',      eyebrow: 'CORE' },
  { id: 'auth',         labelKey: 'settings.shell.tab.auth',         eyebrow: 'CORE' },
  { id: 'llm',          labelKey: 'settings.shell.tab.llm',          eyebrow: 'MODELS' },
  { id: 'voice',        labelKey: 'settings.shell.tab.voice',        eyebrow: 'MODELS' },
  { id: 'services',     labelKey: 'settings.shell.tab.services',     eyebrow: 'INTEGRATIONS' },
  { id: 'yaml',         labelKey: 'settings.shell.tab.yaml',         eyebrow: 'INTEGRATIONS' },
  { id: 'running-tmpl', labelKey: 'settings.shell.tab.runningTmpl',  eyebrow: 'INTEGRATIONS' },
  { id: 'compaction',   labelKey: 'settings.shell.tab.compaction',   eyebrow: 'ADVANCED' },
  { id: 'memory',       labelKey: 'settings.shell.tab.memory',       eyebrow: 'ADVANCED' },
  { id: 'experimental', labelKey: 'settings.shell.tab.experimental', eyebrow: 'ADVANCED' },
]
const GROUP_KEY = {
  CORE: 'settings.shell.group.core',
  MODELS: 'settings.shell.group.models',
  INTEGRATIONS: 'settings.shell.group.integrations',
  ADVANCED: 'settings.shell.group.advanced',
}
const active = ref('general')

// Group tabs by eyebrow so the sidebar renders section headers between
// runs of items sharing the same group label — keeps the JSX-style
// sidebar pattern (SECTION → items → SECTION → items).
function groupedTabs() {
  const out = []
  let lastEyebrow = null
  for (const t of TABS) {
    if (t.eyebrow !== lastEyebrow) {
      out.push({ isHeader: true, eyebrow: t.eyebrow })
      lastEyebrow = t.eyebrow
    }
    out.push(t)
  }
  return out
}
const navItems = groupedTabs()

function labelFor(id) {
  const tab = TABS.find((tab) => tab.id === id)
  return tab ? t(tab.labelKey) : ''
}
</script>

<template>
  <div class="settings-shell">
    <!-- Sidebar -->
    <aside class="settings-nav">
      <div class="nav-eyebrow">{{ t('settings.shell.eyebrow') }}</div>
      <template v-for="(item, idx) in navItems" :key="idx">
        <div v-if="item.isHeader" class="nav-section">{{ t(GROUP_KEY[item.eyebrow]) }}</div>
        <button
          v-else
          type="button"
          class="nav-item"
          :class="{ active: active === item.id }"
          @click="active = item.id"
        >
          {{ t(item.labelKey) }}
        </button>
      </template>
    </aside>

    <!-- Main -->
    <main class="settings-main">
      <header class="page-header">
        <div class="eyebrow">{{ t('settings.shell.eyebrow') }} · {{ labelFor(active).toUpperCase() }}</div>
        <h1>
          <span class="grad">{{ labelFor(active) }}</span>
        </h1>
      </header>

      <section class="panel">
        <SettingsGeneral v-if="active === 'general'" />
        <SettingsAuth v-else-if="active === 'auth'" />
        <SettingsLLM v-else-if="active === 'llm'" />
        <SettingsVoice v-else-if="active === 'voice'" />
        <SettingsYaml v-else-if="active === 'yaml'" />
        <SettingsRunningTemplates v-else-if="active === 'running-tmpl'" />
        <SettingsServices v-else-if="active === 'services'" />
        <SettingsCompaction v-else-if="active === 'compaction'" />
        <SettingsMemory v-else-if="active === 'memory'" />
        <SettingsExperimental v-else-if="active === 'experimental'" />
      </section>
    </main>
  </div>
</template>

<style scoped>
.settings-shell {
  display: flex;
  width: 100%;
  min-height: 100%;
  background: var(--bg-0);
  color: var(--text);
}

/* ── Sidebar ──────────────────────────────────────────────────────── */
.settings-nav {
  flex-shrink: 0;
  width: 220px;
  padding: 16px 12px;
  background: var(--bg-1);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.nav-eyebrow {
  padding: 4px 10px 10px;
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--text-muted);
}
.nav-section {
  padding: 14px 10px 4px;
  font-family: var(--font-mono);
  font-size: 9.5px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--text-subtle);
}
.nav-item {
  display: flex;
  align-items: center;
  padding: 8px 10px;
  margin: 1px 0;
  border-radius: var(--r-md);
  background: transparent;
  border: none;
  border-left: 2px solid transparent;
  color: var(--text-dim);
  font: 400 13px var(--font-body);
  text-align: left;
  cursor: pointer;
  transition: background 0.12s, color 0.12s;
}
.nav-item:hover {
  color: var(--text);
  background: rgba(255, 255, 255, 0.03);
}
.nav-item.active {
  color: var(--text);
  background: var(--primary-bg-strong);
  border-left-color: var(--primary);
  font-weight: 500;
}

/* ── Main ─────────────────────────────────────────────────────────── */
.settings-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  overflow-y: auto;
}
.page-header {
  padding: 28px 32px 18px;
  border-bottom: 1px solid var(--border);
}
.eyebrow {
  font-family: var(--font-mono);
  font-size: 10.5px;
  letter-spacing: 0.14em;
  color: var(--text-muted);
  text-transform: uppercase;
  margin-bottom: 6px;
}
.page-header h1 {
  margin: 0;
  font-family: var(--font-display);
  font-size: 26px;
  font-weight: 600;
  letter-spacing: -0.01em;
  color: var(--text);
}
.grad {
  font-style: italic;
  background: linear-gradient(135deg, var(--primary-hover) 0%, var(--accent) 100%);
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
  color: transparent;
}
.panel {
  flex: 1;
  padding: 24px 32px 40px;
}

/* ── Responsive ───────────────────────────────────────────────────── */
@media (max-width: 880px) {
  .settings-shell { flex-direction: column; }
  .settings-nav {
    width: 100%;
    border-right: none;
    border-bottom: 1px solid var(--border);
    flex-direction: row;
    /* nowrap + overflow-x:auto turns the nav into a single-line
       horizontal-scroll strip. flex-wrap was making 7 items collapse
       to 2-3 rows on phones, eating ~110px of vertical space before
       any setting content became visible. */
    flex-wrap: nowrap;
    padding: 8px 12px;
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
    scrollbar-width: none;
  }
  .settings-nav::-webkit-scrollbar { display: none; }
  .nav-item { flex-shrink: 0; white-space: nowrap; }
  .nav-eyebrow,
  .nav-section { display: none; }
  .nav-item {
    border-left: none;
    border-bottom: 2px solid transparent;
    border-radius: 0;
    padding: 8px 12px;
  }
  /* Flat strip on mobile: the desktop active state is a rounded lavender
     pill, which collided with the horizontal-scroll clipping and read as
     "broken". Drop the pill fill — the bottom-border alone marks active.
     Keep the inherited var(--text) label (not --primary-hover, a light
     lavender ~2.7:1 on the white light-theme strip, below WCAG AA); the
     border carries the accent, the text stays high-contrast. */
  .nav-item.active {
    background: transparent;
    border-left-color: transparent;
    border-bottom-color: var(--primary);
  }
  .page-header { padding: 20px 18px 14px; }
  .panel { padding: 18px; }
}
</style>

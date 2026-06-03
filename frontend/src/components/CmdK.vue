<script setup>
/**
 * CmdK — global command palette.
 *
 * Opens on ⌘K (handled by AppLayout). Lists every route returned by
 * ``router.getRoutes()`` whose ``meta.title`` is set and which isn't a
 * bare/setup/oauth-callback route. Arrow keys navigate, Enter opens,
 * Esc closes.
 *
 * Trigger glue lives in AppLayout — this component is purely a
 * controlled overlay.
 */
import { ref, computed, watch, nextTick } from 'vue'
import { useRouter } from 'vue-router'

const props = defineProps({
  open: { type: Boolean, default: false },
})
const emit = defineEmits(['close'])

const router = useRouter()
const query = ref('')
const cursor = ref(0)
const inputEl = ref(null)
const listEl = ref(null)

// Hidden routes: dynamic detail pages (/:id) and bare-layout flows
// shouldn't show up — they aren't user-navigable destinations.
function visibleRoute(r) {
  if (!r.meta?.title) return false
  if (r.meta?.layout === 'bare') return false
  if (r.path.includes(':')) return false
  if (r.meta?.comingSoon) return false
  return true
}

const allRoutes = computed(() => {
  return router.getRoutes()
    .filter(visibleRoute)
    .map(r => ({
      path: r.path,
      label: r.meta.title,
      name: r.name,
    }))
})

const filtered = computed(() => {
  const q = query.value.trim().toLowerCase()
  if (!q) return allRoutes.value
  return allRoutes.value.filter(r =>
    r.label.toLowerCase().includes(q) ||
    r.path.toLowerCase().includes(q),
  )
})

watch(() => props.open, async (val) => {
  if (val) {
    query.value = ''
    cursor.value = 0
    await nextTick()
    inputEl.value?.focus()
  }
})

watch(filtered, () => {
  // Reset cursor whenever the result set shrinks.
  if (cursor.value >= filtered.value.length) cursor.value = 0
})

function close() {
  emit('close')
}

function pick(item) {
  if (!item) return
  router.push(item.path)
  close()
}

function onKeydown(e) {
  if (e.key === 'Escape') {
    e.preventDefault()
    close()
  } else if (e.key === 'ArrowDown') {
    e.preventDefault()
    cursor.value = Math.min(cursor.value + 1, filtered.value.length - 1)
    scrollCursorIntoView()
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    cursor.value = Math.max(cursor.value - 1, 0)
    scrollCursorIntoView()
  } else if (e.key === 'Enter') {
    e.preventDefault()
    pick(filtered.value[cursor.value])
  }
}

function scrollCursorIntoView() {
  nextTick(() => {
    const el = listEl.value?.children[cursor.value]
    if (el) el.scrollIntoView({ block: 'nearest' })
  })
}

function onOverlayClick(e) {
  // Click outside the floating panel closes the palette.
  if (e.target === e.currentTarget) close()
}
</script>

<template>
  <Teleport to="body">
    <div
      v-if="open"
      class="cmdk-overlay jv"
      role="dialog"
      aria-modal="true"
      aria-label="Command palette"
      @click="onOverlayClick"
      @keydown="onKeydown"
    >
      <div class="cmdk-panel" role="document">
        <div class="cmdk-input-row">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
            <path d="M11 4a7 7 0 1 1 0 14 7 7 0 0 1 0-14zM21 21l-5-5"
              stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
          <input
            ref="inputEl"
            v-model="query"
            class="cmdk-input"
            placeholder="Go to agent, page, or setting…"
            autocomplete="off"
            spellcheck="false"
          />
          <kbd class="cmdk-kbd">ESC</kbd>
        </div>

        <div ref="listEl" class="cmdk-list">
          <div
            v-for="(item, i) in filtered"
            :key="item.path"
            class="cmdk-row"
            :class="{ 'cmdk-row--active': cursor === i }"
            @mouseenter="cursor = i"
            @click="pick(item)"
          >
            <span class="cmdk-row__icon">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
                <path d="M5 12h14M13 6l6 6-6 6"
                  stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
            </span>
            <div class="cmdk-row__body">
              <div class="cmdk-row__label">{{ item.label }}</div>
              <div class="cmdk-row__sub mono-label">PAGE · {{ item.path }}</div>
            </div>
            <kbd v-if="cursor === i" class="cmdk-kbd">⏎</kbd>
          </div>

          <div v-if="filtered.length === 0" class="cmdk-empty">
            No matches for &ldquo;{{ query }}&rdquo;
          </div>
        </div>

        <div class="cmdk-footer">
          <span>↑↓ NAVIGATE</span>
          <span>⏎ OPEN</span>
          <span class="cmdk-footer__count">{{ filtered.length }} RESULT{{ filtered.length === 1 ? '' : 'S' }}</span>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.cmdk-overlay {
  position: fixed;
  inset: 0;
  z-index: 10000;
  background: rgba(0, 0, 0, 0.6);
  backdrop-filter: blur(6px);
  -webkit-backdrop-filter: blur(6px);
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding-top: 84px;
  font-family: var(--font-body);
  color: var(--text);
}

.cmdk-panel {
  width: 640px;
  max-width: calc(100vw - 32px);
  background: var(--bg-2);
  border: 1px solid var(--border-bright);
  border-radius: var(--r-xl);
  box-shadow: var(--shadow-lg);
  overflow: hidden;
  display: flex;
  flex-direction: column;
  /* dvh excludes the iOS URL bar AND the on-screen keyboard, so the
     list never gets pushed off-screen when the user starts typing. */
  max-height: calc(100dvh - 168px);
}

/* Mobile: tighter top inset (84px wastes a third of the viewport on
   phones), full vh-aware height, hide the "↑↓ NAVIGATE" footer hint
   that's meaningless on touch. */
@media (max-width: 640px) {
  .cmdk-overlay {
    padding-top: max(12px, var(--safe-top));
    padding-bottom: max(12px, var(--safe-bottom));
  }
  .cmdk-panel {
    max-height: calc(100dvh - 24px);
  }
}

.cmdk-input-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px 16px;
  border-bottom: 1px solid var(--border-strong);
  color: var(--text-muted);
}

.cmdk-input {
  flex: 1;
  /* Without min-width:0 the input refuses to shrink below its intrinsic
     width, pushing the ESC kbd past the panel's clipped right edge on
     narrow phones. */
  min-width: 0;
  background: transparent;
  border: 0;
  outline: 0;
  color: var(--text);
  font-size: 14px;
  font-family: var(--font-body);
}
.cmdk-input::placeholder { color: var(--text-muted); }

.cmdk-kbd {
  flex-shrink: 0;
  padding: 2px 6px;
  border-radius: 5px;
  background: var(--bg-3);
  border: 1px solid var(--border-strong);
  font-family: var(--font-mono);
  font-size: 10.5px;
  color: var(--text-dim);
}

.cmdk-list {
  padding: 8px;
  overflow-y: auto;
  flex: 1;
}

.cmdk-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 12px;
  border-radius: var(--r-md);
  background: transparent;
  border-left: 2px solid transparent;
  cursor: pointer;
  transition: background 0.12s var(--ease-out);
}
.cmdk-row--active {
  background: var(--primary-bg);
  border-left-color: var(--primary);
}

.cmdk-row__icon {
  width: 28px;
  height: 28px;
  border-radius: 7px;
  background: var(--bg-3);
  border: 1px solid var(--border-strong);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-dim);
  flex-shrink: 0;
}

.cmdk-row__body { flex: 1; min-width: 0; }
.cmdk-row__label {
  font-size: 13.5px;
  font-weight: 500;
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.cmdk-row__sub {
  font-size: 10px;
  margin-top: 2px;
  color: var(--text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.cmdk-empty {
  padding: 24px 12px;
  text-align: center;
  color: var(--text-muted);
  font-size: 13px;
}

.cmdk-footer {
  padding: 8px 16px;
  border-top: 1px solid var(--border-strong);
  background: var(--bg-1);
  display: flex;
  align-items: center;
  gap: 14px;
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-muted);
  letter-spacing: 0.08em;
}
.cmdk-footer__count { margin-left: auto; }

/* Hide the arrow-keys hint on touch — keyboard navigation isn't the
   primary affordance there, and the footer was overflowing on <380px. */
@media (max-width: 480px) {
  .cmdk-footer > :first-child { display: none; }
}
</style>

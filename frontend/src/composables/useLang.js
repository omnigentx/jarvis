/**
 * useLang — single source of truth for the UI language preference.
 *
 * The pref lives in localStorage (`jarvis_lang`: 'vi' | 'en') and was
 * previously a ref local to AppLayout, which meant other components could
 * only read a stale snapshot at mount. This module-level ref is shared by
 * every importer, so the topbar toggle reactively updates any component
 * that renders bilingual copy (e.g. ChatView's crawl banner).
 *
 * Jarvis ships globally: every user-visible string outside chat content
 * must render for BOTH locales. Pattern: `lang === 'vi' ? '…' : '…'` off
 * this composable — never a hardcoded single-language literal.
 */
import { ref } from 'vue'

const lang = ref(localStorage.getItem('jarvis_lang') || 'vi')

function toggleLang() {
  lang.value = lang.value === 'vi' ? 'en' : 'vi'
  localStorage.setItem('jarvis_lang', lang.value)
}

export function useLang() {
  return { lang, toggleLang }
}

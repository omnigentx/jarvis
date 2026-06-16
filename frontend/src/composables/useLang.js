/**
 * useLang — UI language preference + i18n lookup.
 *
 * Jarvis ships globally; ENGLISH is the default and the COMPLETE base locale.
 * Other locales (locales/<code>.json) are partial overlays: a key present in
 * the active locale is used, otherwise it falls back to English, otherwise the
 * key itself is returned. This lets OSS contributors add a language by dropping
 * one JSON file and translating only the keys they want — no key is ever
 * "missing" for the user.
 *
 * Adding a language (for contributors):
 *   1. copy src/locales/en.json → src/locales/<code>.json
 *   2. translate the values you want (leave the rest; English fills the gaps)
 *   3. register it in LOCALES + the topbar toggle
 *
 * Usage in components:
 *   const { t, lang, toggleLang } = useLang()
 *   t('settings.memory.enable')                  // → localized string
 *   t('settings.memory.qdrantPoints', { points: 12 })   // {placeholder} interpolation
 *
 * Legacy inline copy (`lang === 'vi' ? '…' : '…'`) still works off the same
 * `lang` ref and can be migrated to keys incrementally.
 */
import { ref } from 'vue'

import en from '../locales/en.json' with { type: 'json' }
import vi from '../locales/vi.json' with { type: 'json' }

// English first so it is always the fallback base.
const LOCALES = { en, vi }

// English is the COMPLETE base/fallback locale (keys resolve en → key), but the
// default UI language stays 'vi' to preserve existing behaviour — flipping the
// default for new users is a separate product decision, not this PR's scope.
const lang = ref(localStorage.getItem('jarvis_lang') || 'vi')

function toggleLang() {
  lang.value = lang.value === 'vi' ? 'en' : 'vi'
  localStorage.setItem('jarvis_lang', lang.value)
}

function _resolve(obj, path) {
  return path.split('.').reduce(
    (o, k) => (o && typeof o === 'object' ? o[k] : undefined),
    obj,
  )
}

/**
 * Look up a dot-path key in the active locale, falling back to English, then
 * to the raw key. ``params`` fills ``{name}`` placeholders. Reactive: reading
 * ``lang.value`` makes templates re-render on a language toggle.
 */
function t(key, params) {
  const active = _resolve(LOCALES[lang.value] || {}, key)
  const base = _resolve(LOCALES.en, key)
  let str = active != null ? active : base != null ? base : key
  if (params && typeof str === 'string') {
    str = str.replace(/\{(\w+)\}/g, (_, k) =>
      params[k] != null ? String(params[k]) : `{${k}}`,
    )
  }
  return str
}

export function useLang() {
  return { lang, toggleLang, t }
}

/**
 * useLang — UI language preference + i18n lookup.
 *
 * Jarvis ships globally; ENGLISH is the default and the COMPLETE base locale.
 * Every other locale (src/locales/<code>.json) is a PARTIAL overlay: a key
 * present in the active locale wins, otherwise English fills the gap, otherwise
 * the raw key is returned. No key is ever "missing" for the user, so a half-
 * finished translation is still shippable.
 *
 * Contributing a translation (the whole workflow):
 *   1. copy src/locales/en.json → src/locales/<code>.json
 *   2. translate the values you want (leave the rest; English fills the gaps)
 *   3. open a PR — that's it.
 *
 * No JS edit is needed: locales are auto-discovered via import.meta.glob, so a
 * new <code>.json automatically appears in `availableLocales` and the topbar
 * toggle. This is the OSS-contributor promise — one file, zero wiring.
 *
 * Usage in components:
 *   const { t, lang, toggleLang } = useLang()
 *   t('agents.title')                          // → localized string
 *   t('agents.statAgents', { n: 12 })          // {placeholder} interpolation
 *
 * Legacy inline copy (`lang === 'vi' ? '…' : '…'`) still works off the same
 * `lang` ref and can be migrated to keys incrementally.
 */
import { ref } from 'vue'

// Auto-discover every locale file. eager + import:'default' gives us a plain
// map of { './locales/en.json': {...}, './locales/vi.json': {...} }.
const _modules = import.meta.glob('../locales/*.json', {
  eager: true,
  import: 'default',
})

// Build { en: {...}, vi: {...}, ... } keyed by the filename stem.
const LOCALES = {}
for (const [path, data] of Object.entries(_modules)) {
  const code = path.split('/').pop().replace(/\.json$/, '')
  LOCALES[code] = data
}

// English is the COMPLETE base/fallback locale and must always exist.
if (!LOCALES.en) {
  throw new Error('useLang: src/locales/en.json (the base locale) is missing.')
}

// Available codes, English first so it is always the fallback base; the rest
// sorted for a stable toggle order regardless of glob iteration order.
export const availableLocales = [
  'en',
  ...Object.keys(LOCALES)
    .filter((c) => c !== 'en')
    .sort(),
]

// Default UI language stays 'vi' to preserve existing behaviour — flipping the
// default for new users is a separate product decision, not this change's scope.
const _stored = localStorage.getItem('jarvis_lang')
const lang = ref(availableLocales.includes(_stored) ? _stored : 'vi')

function setLang(code) {
  if (!availableLocales.includes(code)) return
  lang.value = code
  localStorage.setItem('jarvis_lang', code)
}

// Cycle through the available locales. With just en+vi this behaves like a
// binary toggle; with a third file dropped in it rotates through all of them.
function toggleLang() {
  const i = availableLocales.indexOf(lang.value)
  setLang(availableLocales[(i + 1) % availableLocales.length])
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
  // Only leaf string values are valid translations; a non-leaf key (e.g. a
  // bare namespace like "agents" that resolves to an object) falls through to
  // the raw key rather than rendering "[object Object]".
  let str =
    typeof active === 'string' ? active : typeof base === 'string' ? base : key
  if (params && typeof str === 'string') {
    str = str.replace(/\{(\w+)\}/g, (_, k) =>
      params[k] != null ? String(params[k]) : `{${k}}`,
    )
  }
  return str
}

export function useLang() {
  return { lang, toggleLang, setLang, availableLocales, t }
}

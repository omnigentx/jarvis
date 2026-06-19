/**
 * i18n coverage guard — the scale safety-net for the locale dictionary.
 *
 * Two checks, run under `node --test` (no Vite, so we read files via fs and
 * never touch useLang's import.meta.glob):
 *
 *   1. FAIL  — every string-literal key used as `t('a.b')` in a .vue/.js file
 *              must exist in en.json (the COMPLETE base locale). A missing key
 *              is the i18n equivalent of "single-language literal is a bug":
 *              it would silently render the raw key to the user.
 *
 *   2. REPORT — keys present in en.json but absent from a translation overlay
 *              (e.g. vi.json) are printed as a coverage summary, NOT failed:
 *              partial translations are intentionally shippable (English fills
 *              the gap). This tells contributors what is left to translate.
 *
 * Dynamic keys (`t(someVar)`) can't be checked statically and are skipped by
 * design — keep them rare and covered by an e2e/render test instead.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..')
const LOCALES = join(SRC, 'locales')

function flatten(obj, prefix = '', out = new Set()) {
  for (const [k, v] of Object.entries(obj)) {
    const key = prefix ? `${prefix}.${k}` : k
    if (v && typeof v === 'object' && !Array.isArray(v)) flatten(v, key, out)
    else out.add(key)
  }
  return out
}

function loadLocale(code) {
  return JSON.parse(readFileSync(join(LOCALES, `${code}.json`), 'utf8'))
}

// Recursively collect every .vue / .js source file (skip tests + node_modules).
function sourceFiles(dir, out = []) {
  for (const ent of readdirSync(dir, { withFileTypes: true })) {
    if (ent.name === 'node_modules' || ent.name === 'locales') continue
    const full = join(dir, ent.name)
    if (ent.isDirectory()) sourceFiles(full, out)
    else if (/\.(vue|js)$/.test(ent.name) && !/\.test\.js$/.test(ent.name))
      out.push(full)
  }
  return out
}

// `t('a.b')` / `t("a.b")` but not `obj.t(`, `await(`, `split(`… via the
// no-word-or-dot lookbehind before the bare `t`. The trailing `[),]` requires
// the literal to be the COMPLETE first argument — so dynamic concatenations
// like `t('lifecycle.' + type)` (followed by ` +`) are skipped, since only the
// runtime knows the full key.
const T_CALL = /(?<![\w.])t\(\s*['"]([^'"]+)['"]\s*[),]/g

function usedKeys() {
  const keys = new Map() // key -> first file that used it (for error messages)
  for (const file of sourceFiles(SRC)) {
    const text = readFileSync(file, 'utf8')
    for (const m of text.matchAll(T_CALL)) {
      if (!keys.has(m[1])) keys.set(m[1], file.replace(SRC, 'src'))
    }
  }
  return keys
}

test('every t() key used in source exists in en.json (base locale)', () => {
  const base = flatten(loadLocale('en'))
  const missing = []
  for (const [key, file] of usedKeys()) {
    if (!base.has(key)) missing.push(`${key}  (first used in ${file})`)
  }
  assert.equal(
    missing.length,
    0,
    `\n${missing.length} t() key(s) missing from en.json:\n  ${missing.join('\n  ')}\n`,
  )
})

test('translation coverage report (informational — never fails)', () => {
  const base = flatten(loadLocale('en'))
  for (const code of readdirSync(LOCALES)) {
    const m = code.match(/^(.+)\.json$/)
    if (!m || m[1] === 'en') continue
    const overlay = flatten(loadLocale(m[1]))
    const untranslated = [...base].filter((k) => !overlay.has(k))
    const pct = Math.round(((base.size - untranslated.length) / base.size) * 100)
    console.log(
      `  [i18n] ${m[1]}: ${pct}% translated (${untranslated.length}/${base.size} keys fall back to English)`,
    )
  }
  assert.ok(true)
})

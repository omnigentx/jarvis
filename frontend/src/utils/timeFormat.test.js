import { test } from 'node:test'
import assert from 'node:assert/strict'

import { normalizeTs, formatTimestamp } from './timeFormat.js'

// --- normalizeTs ---------------------------------------------------------

test('normalizeTs: Unix seconds float → ms (the bug case)', () => {
  // Backend serialized this exact value for meeting created_at on 2026-05-15.
  // Before the fix, `new Date(1778834375.42)` produced 1970-01-21 because JS
  // treated the number as milliseconds. normalizeTs must scale it to ms.
  const ms = normalizeTs(1778834375.42)
  assert.equal(ms, 1778834375420)
  const year = new Date(ms).getUTCFullYear()
  assert.equal(year, 2026, `Expected year 2026, got ${year}`)
})

test('normalizeTs: Unix seconds int → ms', () => {
  assert.equal(normalizeTs(1778834375), 1778834375000)
})

test('normalizeTs: Unix milliseconds → unchanged', () => {
  // Already ≥ 1e12 → kept as-is.
  assert.equal(normalizeTs(1778834375000), 1778834375000)
})

test('normalizeTs: numeric string (seconds) → ms', () => {
  assert.equal(normalizeTs('1778834375.42'), 1778834375420)
})

test('normalizeTs: numeric string (ms) → unchanged', () => {
  assert.equal(normalizeTs('1778834375000'), 1778834375000)
})

test('normalizeTs: ISO 8601 string → ms', () => {
  const ms = normalizeTs('2026-05-15T08:39:35Z')
  assert.equal(ms, Date.UTC(2026, 4, 15, 8, 39, 35))
})

test('normalizeTs: null/undefined/0/empty → null', () => {
  assert.equal(normalizeTs(null), null)
  assert.equal(normalizeTs(undefined), null)
  assert.equal(normalizeTs(0), null)
  assert.equal(normalizeTs(''), null)
})

test('normalizeTs: NaN / non-finite → null', () => {
  assert.equal(normalizeTs(NaN), null)
  assert.equal(normalizeTs(Infinity), null)
})

test('normalizeTs: unparseable string → null', () => {
  assert.equal(normalizeTs('not a date'), null)
})

// --- formatTimestamp -----------------------------------------------------

test('formatTimestamp: invalid input → empty string', () => {
  assert.equal(formatTimestamp(null), '')
  assert.equal(formatTimestamp(0), '')
  assert.equal(formatTimestamp('garbage'), '')
})

test('formatTimestamp: Unix seconds renders correct year (not 1970)', () => {
  // Regression guard: the meeting-time bug surfaced as "Jan 21 1970" because
  // the seconds-vs-ms heuristic was missing. Asserting the year here pins
  // that fix in place.
  const out = formatTimestamp(1778834375.42)
  assert.ok(out.includes('2026'), `Expected output to contain 2026, got "${out}"`)
  assert.ok(!out.includes('1970'), `Output must not contain 1970, got "${out}"`)
})

test('formatTimestamp: dateOnly returns dd/MM/yyyy', () => {
  const out = formatTimestamp('2026-05-15T08:00:00Z', { dateOnly: true })
  // Format is local-timezone-dependent but year must be 2026 and slash-separated.
  assert.match(out, /^\d{2}\/\d{2}\/\d{4}$/)
  assert.ok(out.endsWith('/2026'))
})

test('formatTimestamp: timeOnly returns HH:mm:ss', () => {
  const out = formatTimestamp(1778834375.42, { timeOnly: true })
  assert.match(out, /^\d{2}:\d{2}:\d{2}$/)
})

/**
 * Pure timestamp helpers — no vue / no pinia deps so they're directly testable.
 *
 * Backend serializes timestamps as Unix **seconds** (float), e.g. 1778834375.42.
 * Passing that straight to `new Date(n)` produces 1970-01-21 because JS Date
 * treats raw numbers as **milliseconds**. Use `normalizeTs` before constructing
 * a Date from any backend timestamp.
 */

/**
 * Normalize any timestamp value to milliseconds.
 *
 * Handles:
 * - Unix seconds (float or int, e.g. 1775967073.51)
 * - Unix milliseconds (e.g. 1775967073000)
 * - ISO 8601 strings (e.g. "2026-04-12T12:30:00Z")
 * - null / undefined / 0 / NaN → returns null
 *
 * Heuristic: numeric values < 1e12 (~year 2001 in ms) are treated as seconds.
 */
export function normalizeTs(ts) {
  if (ts == null || ts === 0 || ts === '') return null

  if (typeof ts === 'number') {
    if (!Number.isFinite(ts)) return null
    // seconds → ms (timestamps < 1e12 are definitely in seconds)
    return ts < 1e12 ? Math.round(ts * 1000) : Math.round(ts)
  }

  if (typeof ts === 'string') {
    // Try numeric string first (e.g. "1775967073.51")
    const num = Number(ts)
    if (Number.isFinite(num) && num > 0) {
      return num < 1e12 ? Math.round(num * 1000) : Math.round(num)
    }
    // Try ISO / date string
    const d = new Date(ts)
    return Number.isFinite(d.getTime()) ? d.getTime() : null
  }

  return null
}

/**
 * Format a timestamp for display: 24h, dd/MM/YYYY.
 *
 * @param {number|string|null} ts - raw timestamp (seconds, ms, or ISO string)
 * @param {Object} opts
 * @param {boolean} [opts.dateOnly=false] - show only date without time
 * @param {boolean} [opts.timeOnly=false] - show only time without date
 * @returns {string} formatted string like "14:30:05 12/04/2026" or "" if invalid
 */
export function formatTimestamp(ts, opts = {}) {
  const ms = normalizeTs(ts)
  if (ms === null) return ''

  const d = new Date(ms)
  if (!Number.isFinite(d.getTime())) return ''

  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  const ss = String(d.getSeconds()).padStart(2, '0')
  const dd = String(d.getDate()).padStart(2, '0')
  const MM = String(d.getMonth() + 1).padStart(2, '0')
  const yyyy = d.getFullYear()

  if (opts.timeOnly) return `${hh}:${mm}:${ss}`
  if (opts.dateOnly) return `${dd}/${MM}/${yyyy}`

  // Same day → show time only to save space
  const now = new Date()
  if (d.getDate() === now.getDate() && d.getMonth() === now.getMonth() && d.getFullYear() === now.getFullYear()) {
    return `${hh}:${mm}:${ss}`
  }

  return `${hh}:${mm}:${ss} ${dd}/${MM}/${yyyy}`
}

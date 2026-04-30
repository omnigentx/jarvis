/**
 * Pinia store for /api/settings — the post-setup configuration surface.
 *
 * The backend masks secrets as "***" in reads, so we never hold cleartext
 * secrets in the store.  Callers writing a secret must go through `setValue`
 * with `isSecret: true`; we pass that flag through to the backend untouched.
 *
 * Backend response shapes:
 *   GET  /api/settings            → {categories: {cat: [ConfigEntry, ...]}}
 *   GET  /api/settings/{cat}      → {category, items: [ConfigEntry]}
 *   GET  /api/settings/{cat}/{k}  → ConfigEntry
 *   PUT  /api/settings/{cat}/{k}  → {category, key, action, is_secret}   (no value)
 *   PUT  /api/settings/bulk       → {events: [...]}                       (no values)
 *   DEL  /api/settings/{cat}/{k}  → {deleted: true, category, key}
 *
 * Because PUT/bulk don't echo the full entry, we re-fetch the affected entry
 * (or full snapshot on bulk) after a write to keep the store in sync.
 */
import { defineStore } from 'pinia'
import { apiFetch, setApiKey, ApiError } from '../api'

export const useSettingsStore = defineStore('settings', {
  state: () => ({
    // { category: { key: ConfigEntry } }
    entries: {},
    loading: false,
    error: null,
    lastMutationError: null,
  }),
  getters: {
    categories: (state) => Object.keys(state.entries).sort(),
    listCategory: (state) => (category) => {
      const rows = Object.values(state.entries[category] || {})
      return rows.sort((a, b) => a.key.localeCompare(b.key))
    },
    getEntry: (state) => (category, key) =>
      state.entries[category]?.[key] || null,
    getValue: (state) => (category, key) =>
      state.entries[category]?.[key]?.value ?? null,
  },
  actions: {
    _ingestGrouped(grouped) {
      const next = {}
      for (const [cat, rows] of Object.entries(grouped || {})) {
        next[cat] = {}
        for (const row of rows || []) {
          if (!row?.key) continue
          next[cat][row.key] = row
        }
      }
      this.entries = next
    },
    _upsert(row) {
      if (!row?.category || !row?.key) return
      const cat = { ...(this.entries[row.category] || {}) }
      cat[row.key] = row
      this.entries = { ...this.entries, [row.category]: cat }
    },
    _remove(category, key) {
      const cat = { ...(this.entries[category] || {}) }
      delete cat[key]
      const next = { ...this.entries }
      if (Object.keys(cat).length === 0) delete next[category]
      else next[category] = cat
      this.entries = next
    },
    async fetchAll() {
      this.loading = true
      this.error = null
      try {
        const res = await apiFetch('/api/settings')
        this._ingestGrouped(res?.categories)
        return res
      } catch (err) {
        this.error = _formatApiError(err)
        throw err
      } finally {
        this.loading = false
      }
    },
    async refreshEntry(category, key) {
      const row = await apiFetch(
        `/api/settings/${encodeURIComponent(category)}/${encodeURIComponent(key)}`,
      )
      this._upsert(row)
      return row
    },
    async setValue(category, key, value, { isSecret = false } = {}) {
      this.lastMutationError = null
      try {
        await apiFetch(
          `/api/settings/${encodeURIComponent(category)}/${encodeURIComponent(key)}`,
          {
            method: 'PUT',
            body: JSON.stringify({ value, is_secret: isSecret }),
          },
        )
        // Master-key rotation: persist new bearer locally so the *next* request
        // authenticates.  Do this BEFORE refreshEntry so the GET succeeds.
        if (category === 'auth' && key === 'JARVIS_API_KEY' && typeof value === 'string') {
          setApiKey(value)
        }
        await this.refreshEntry(category, key).catch(() => {
          // Entry may have been deleted via DELETE endpoint elsewhere.
          this._remove(category, key)
        })
      } catch (err) {
        this.lastMutationError = _formatApiError(err)
        throw err
      }
    },
    async bulkUpdate(items) {
      this.lastMutationError = null
      try {
        await apiFetch('/api/settings/bulk', {
          method: 'PUT',
          body: JSON.stringify({ items }),
        })
        const rotated = items.find(
          (i) => i.category === 'auth' && i.key === 'JARVIS_API_KEY',
        )
        if (rotated && typeof rotated.value === 'string') setApiKey(rotated.value)
        // Re-fetch the whole snapshot; bulk writes rarely happen so this is fine.
        await this.fetchAll()
      } catch (err) {
        this.lastMutationError = _formatApiError(err)
        throw err
      }
    },
    async deleteEntry(category, key) {
      this.lastMutationError = null
      try {
        await apiFetch(
          `/api/settings/${encodeURIComponent(category)}/${encodeURIComponent(key)}`,
          { method: 'DELETE' },
        )
        this._remove(category, key)
      } catch (err) {
        this.lastMutationError = _formatApiError(err)
        throw err
      }
    },
    async fetchHistory({ category = null, key = null, limit = 50 } = {}) {
      const params = new URLSearchParams()
      if (category) params.set('category', category)
      if (key) params.set('key', key)
      if (limit) params.set('limit', String(limit))
      const qs = params.toString() ? `?${params.toString()}` : ''
      const res = await apiFetch(`/api/settings/history${qs}`)
      return res?.items || []
    },
    async exportConfig({ includeSecrets = false } = {}) {
      const qs = includeSecrets ? '?include_secrets=true' : ''
      return await apiFetch(`/api/settings/export${qs}`)
    },
    async importConfig({ version, items, replace = false }) {
      this.lastMutationError = null
      try {
        const res = await apiFetch('/api/settings/import', {
          method: 'POST',
          body: JSON.stringify({ version, items, replace }),
        })
        await this.fetchAll()
        return res
      } catch (err) {
        this.lastMutationError = _formatApiError(err)
        throw err
      }
    },
    async restartBackend() {
      return await apiFetch('/api/system/restart', { method: 'POST' })
    },
  },
})

function _formatApiError(err) {
  if (err instanceof ApiError) {
    const body = err.body
    if (body && typeof body === 'object') {
      if (typeof body.detail === 'string') return body.detail
      if (body.detail && typeof body.detail === 'object') {
        return body.detail.message || JSON.stringify(body.detail)
      }
      return body.message || JSON.stringify(body)
    }
    return body || err.message
  }
  return err?.message || String(err)
}

/** One metadata-only lookahead; no chapter text, audio blobs or polling. */
export function createChapterQueue(load, onError = () => {}) {
  let entry = null, controller = null
  function clear() {
    controller?.abort()
    controller = null
    entry = null
  }
  function prepare(storyId, fromFile, nextFile) {
    clear()
    if (!nextFile) return
    const pending = { storyId, fromFile, nextFile, data: null }
    entry = pending
    const abort = new AbortController()
    controller = abort
    const timer = setTimeout(() => abort.abort(), 15_000)
    Promise.resolve().then(() => load(storyId, nextFile, abort.signal)).then(data => {
      if (entry !== pending || abort.signal.aborted) return
      if (!data?.audio_url || data.error) throw new Error(data?.error || 'Missing queued audio URL')
      pending.data = data
    }).catch(error => {
      if (entry === pending && !abort.signal.aborted) onError(error)
    }).finally(() => clearTimeout(timer))
  }
  function take(storyId, fromFile, nextFile) {
    if (!entry || entry.storyId !== storyId || entry.fromFile !== fromFile || entry.nextFile !== nextFile) return null
    const data = entry.data
    clear()
    return data
  }
  return { prepare, take, clear }
}

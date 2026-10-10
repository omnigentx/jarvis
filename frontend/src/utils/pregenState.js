/** Protocol reducer: partial audio is never presented as a completed chapter. */
export function applyPregenEvent(previous, type, data) {
  const state = type === 'snapshot' ? new Map() : new Map(previous)
  if (type === 'snapshot') {
    for (const chapter_file of data.ready || []) state.set(chapter_file, { chapter_file, status: 'ready' })
    for (const row of data.failures || []) state.set(row.chapter_file, { ...row, status: 'error' })
    if (data.generating) state.set(data.generating.chapter_file, { ...data.generating, status: 'generating' })
    for (const row of data.active || []) state.set(row.chapter_file, { ...row, status: 'generating' })
  } else {
    const status = { chapter_generating: 'generating', chapter_progress: 'generating',
      chapter_ready: 'ready', chapter_error: 'error', chapter_pending: 'none' }[type]
    if (status && data.chapter_file) state.set(data.chapter_file, { ...data, status })
  }
  return state
}

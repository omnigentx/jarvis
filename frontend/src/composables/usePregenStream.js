/** Realtime pregen state with authoritative snapshots on every reconnect. */
import { ref, watch, onUnmounted } from 'vue'
import { buildSSEUrl } from '../api'
import { EVENTS, on } from '../auth/bus.js'
import { useAuthStore } from '../stores/auth.js'
import { applyPregenEvent } from '../utils/pregenState.js'

export function usePregenStream(storyIdRef) {
  const auth = useAuthStore()
  const isConnected = ref(false)
  const queue = ref([])
  const generating = ref(null)
  const chapterStatuses = ref(new Map())
  let eventSource, reconnectTimer, lastStoryId
  let reconnectDelay = 1000

  function disconnect() {
    eventSource?.close()
    eventSource = null
    clearTimeout(reconnectTimer)
    reconnectTimer = null
    isConnected.value = false
  }

  function connect(storyId, reconnect = false) {
    if (storyId !== lastStoryId) {
      chapterStatuses.value = new Map()
      queue.value = []
      generating.value = null
    }
    lastStoryId = storyId
    disconnect()
    if (!reconnect) reconnectDelay = 1000
    if (!auth.isAuthenticated) return
    const params = new URLSearchParams()
    if (storyId) params.set('story_id', storyId)
    const source = new EventSource(buildSSEUrl(`/api/stories/pregen-stream?${params}`))
    eventSource = source
    source.onopen = () => {
      if (eventSource !== source) return
      isConnected.value = true
      reconnectDelay = 1000
    }
    for (const type of ['snapshot', 'chapter_generating', 'chapter_progress', 'chapter_ready', 'chapter_error', 'chapter_pending']) {
      source.addEventListener(type, e => {
        if (eventSource !== source) return
        try {
          const data = JSON.parse(e.data)
          if (data.story_id && storyId && data.story_id !== storyId) return
          chapterStatuses.value = applyPregenEvent(chapterStatuses.value, type, data)
          if (type === 'snapshot') generating.value = data.generating || null
          else if (type === 'chapter_progress' || type === 'chapter_generating') generating.value = data
          else if (generating.value?.chapter_file === data.chapter_file) generating.value = null
          if (type === 'chapter_ready') queue.value = queue.value.filter(q => q.chapter_file !== data.chapter_file)
        } catch { /* Ignore malformed upstream events; reconnect supplies a full snapshot. */ }
      })
    }
    source.addEventListener('queue_update', e => {
      if (eventSource !== source) return
      try {
        const data = JSON.parse(e.data)
        queue.value = (data.queue || []).filter(row => !storyId || row.story_id === storyId)
      } catch { /* Preserve the last valid queue. */ }
    })
    source.onerror = () => {
      if (eventSource !== source) return
      source.close()
      eventSource = null
      isConnected.value = false
      if (!auth.isAuthenticated) return
      reconnectTimer = setTimeout(() => {
        reconnectDelay = Math.min(reconnectDelay * 2, 30000)
        connect(storyId, true)
      }, reconnectDelay)
    }
  }

  const offExpired = on(EVENTS.EXPIRED, disconnect)
  const offRestored = on(EVENTS.RESTORED, () => { if (lastStoryId) connect(lastStoryId) })
  const onVisible = () => {
    if (document.visibilityState === 'visible' && lastStoryId) connect(lastStoryId)
  }
  document.addEventListener('visibilitychange', onVisible)

  function getQueuePosition(file) {
    return queue.value.findIndex(q => q.chapter_file === file)
  }
  function getEffectiveStatus(file, apiPreload) {
    const state = chapterStatuses.value.get(file)
    if (state) return state.status
    if (getQueuePosition(file) >= 0) return 'queued'
    if (isConnected.value && apiPreload === 'generating') return 'none'
    return apiPreload || 'none'
  }
  function getChapterProgress(file) { return chapterStatuses.value.get(file) || {} }

  if (storyIdRef) watch(storyIdRef, id => {
    if (id) connect(id)
    else { disconnect(); lastStoryId = null; chapterStatuses.value = new Map(); queue.value = []; generating.value = null }
  }, { immediate: true })
  onUnmounted(() => {
    disconnect()
    offExpired()
    offRestored()
    document.removeEventListener('visibilitychange', onVisible)
  })
  return { isConnected, queue, generating, chapterStatuses, getQueuePosition,
    getEffectiveStatus, getChapterProgress, connect, disconnect }
}

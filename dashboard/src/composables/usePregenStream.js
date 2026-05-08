/**
 * usePregenStream — SSE composable cho TTS pre-generation status.
 *
 * Connect tới /api/stories/pregen-stream, nhận events:
 *   - queue_update: danh sách chapters trong queue
 *   - chapter_generating: chapter đang generate
 *   - chapter_ready: chapter generate xong
 *   - chapter_error: generate thất bại
 *
 * Auto-reconnect with exponential backoff.
 * Tự cleanup khi component unmount.
 */
import { ref, watch, onUnmounted } from 'vue'
import { buildSSEUrl } from '../api'
import { EVENTS, on } from '../auth/bus.js'
import { useAuthStore } from '../stores/auth.js'

export function usePregenStream(storyIdRef) {
  const auth = useAuthStore()
  const isConnected = ref(false)
  /** @type {import('vue').Ref<Array<{story_id: string, chapter_file: string, priority: number}>>} */
  const queue = ref([])
  /** @type {import('vue').Ref<{story_id: string, chapter_file: string}|null>} */
  const generating = ref(null)
  /** @type {import('vue').Ref<Map<string, string>>} Map<chapter_file, status> */
  const chapterStatuses = ref(new Map())

  let eventSource = null
  let reconnectTimer = null
  let reconnectDelay = 1000
  let lastStoryId = null
  const MAX_RECONNECT_DELAY = 30000

  function connect(storyId) {
    lastStoryId = storyId
    if (!auth.isAuthenticated) {
      // Skip — RESTORED listener below will retry once we're back.
      return
    }
    disconnect()
    reconnectDelay = 1000

    const params = new URLSearchParams()
    if (storyId) params.set('story_id', storyId)

    const url = buildSSEUrl(`/api/stories/pregen-stream?${params.toString()}`)
    eventSource = new EventSource(url)

    eventSource.onopen = () => {
      isConnected.value = true
      reconnectDelay = 1000
    }

    eventSource.addEventListener('queue_update', (e) => {
      try {
        const data = JSON.parse(e.data)
        queue.value = data.queue || []
        // Update chapterStatuses from queue
        const newStatuses = new Map(chapterStatuses.value)
        for (const item of queue.value) {
          const key = item.chapter_file
          if (!newStatuses.has(key) || newStatuses.get(key) === 'none') {
            newStatuses.set(key, 'queued')
          }
        }
        chapterStatuses.value = newStatuses
      } catch (_) {}
    })

    eventSource.addEventListener('chapter_generating', (e) => {
      try {
        const data = JSON.parse(e.data)
        generating.value = { story_id: data.story_id, chapter_file: data.chapter_file }
        const newStatuses = new Map(chapterStatuses.value)
        newStatuses.set(data.chapter_file, 'generating')
        chapterStatuses.value = newStatuses
      } catch (_) {}
    })

    eventSource.addEventListener('chapter_ready', (e) => {
      try {
        const data = JSON.parse(e.data)
        if (generating.value?.chapter_file === data.chapter_file) {
          generating.value = null
        }
        const newStatuses = new Map(chapterStatuses.value)
        newStatuses.set(data.chapter_file, 'ready')
        chapterStatuses.value = newStatuses
        // Remove from queue
        queue.value = queue.value.filter(q => q.chapter_file !== data.chapter_file)
      } catch (_) {}
    })

    eventSource.addEventListener('chapter_error', (e) => {
      try {
        const data = JSON.parse(e.data)
        if (generating.value?.chapter_file === data.chapter_file) {
          generating.value = null
        }
        const newStatuses = new Map(chapterStatuses.value)
        newStatuses.set(data.chapter_file, 'error')
        chapterStatuses.value = newStatuses
      } catch (_) {}
    })

    eventSource.addEventListener('ping', () => {
      // Keepalive — no action needed
    })

    eventSource.onerror = () => {
      isConnected.value = false
      eventSource?.close()
      eventSource = null
      if (!auth.isAuthenticated) return  // EXPIRED handler will own teardown
      // Exponential backoff reconnect
      reconnectTimer = setTimeout(() => {
        reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT_DELAY)
        connect(storyId)
      }, reconnectDelay)
    }
  }

  // ─── Auth bus integration ────────────────────────────────────────────
  const offExpired = on(EVENTS.EXPIRED, () => {
    if (eventSource) { try { eventSource.close() } catch (_) { /* ignore */ } eventSource = null }
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null }
    isConnected.value = false
  })
  const offRestored = on(EVENTS.RESTORED, () => {
    if (lastStoryId) connect(lastStoryId)
  })

  function disconnect() {
    if (eventSource) {
      eventSource.close()
      eventSource = null
    }
    if (reconnectTimer) {
      clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    isConnected.value = false
  }

  /**
   * Lấy vị trí queue cho 1 chapter file.
   * @returns {number} 0-based index, hoặc -1 nếu không có trong queue
   */
  function getQueuePosition(chapterFile) {
    return queue.value.findIndex(q => q.chapter_file === chapterFile)
  }

  /**
   * Lấy effective status cho 1 chapter (kết hợp API preload + SSE updates).
   * 
   * Khi SSE connected, SSE là source of truth cho "generating":
   *  - Chỉ 1 chapter có thể generating tại 1 thời điểm
   *  - API có thể trả "generating" từ stale lock files → SSE override
   * 
   * @param {string} chapterFile
   * @param {string} apiPreload - 'ready' | 'generating' | 'none' from API
   * @returns {'ready' | 'generating' | 'queued' | 'none'}
   */
  function getEffectiveStatus(chapterFile, apiPreload) {
    const sseStatus = chapterStatuses.value.get(chapterFile)
    
    // SSE data always takes priority (more real-time)
    if (sseStatus === 'ready') return 'ready'
    if (sseStatus === 'generating') return 'generating'
    if (sseStatus === 'queued') return 'queued'
    
    // When SSE is connected: override stale API "generating"
    // Only the chapter tracked by SSE `generating` ref is truly generating
    if (isConnected.value && apiPreload === 'generating') {
      // SSE is connected but doesn't say this chapter is generating → stale
      // Check if it's in queue instead
      const queuePos = getQueuePosition(chapterFile)
      return queuePos >= 0 ? 'queued' : 'none'
    }
    
    // Fallback to API preload (for 'ready' and 'none')
    return apiPreload || 'none'
  }

  // Auto-connect when storyId changes
  if (storyIdRef) {
    watch(storyIdRef, (newId) => {
      if (newId) {
        connect(newId)
      } else {
        disconnect()
      }
    }, { immediate: true })
  }

  onUnmounted(() => {
    disconnect()
    offExpired()
    offRestored()
  })

  return {
    isConnected,
    queue,
    generating,
    chapterStatuses,
    getQueuePosition,
    getEffectiveStatus,
    connect,
    disconnect,
  }
}

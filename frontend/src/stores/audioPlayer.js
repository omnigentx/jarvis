import { ref, computed, watch } from 'vue'
import { defineStore } from 'pinia'
import { apiFetch } from '../api.js'
import { createChapterQueue } from '../utils/chapterQueue.js'

const STORAGE_KEY = 'jarvis_audio_progress'
const SPEED_STORAGE_KEY = 'jarvis_audio_speed'
const API_SAVE_INTERVAL = 15_000 // 15s

export const useAudioPlayerStore = defineStore('audioPlayer', () => {
  const playbackType = ref('none') // 'none' | 'story' | 'chatTts' | 'libraryBook' | 'notifTts'
  const isPlaying = ref(false)
  const isPaused = ref(false)
  const isBuffering = ref(false)
  const currentTime = ref(0) // seconds
  const duration = ref(0) // seconds (0 = unknown)
  const playbackSpeed = ref(_loadSpeed())

  const currentStoryId = ref(null)
  const currentStoryTitle = ref(null)
  const currentChapterFile = ref(null)
  const chapterFiles = ref([]) // ordered list of chapter filenames
  const currentIndex = ref(-1)

  const currentAudioUrl = ref(null)
  const currentAudioReady = ref(false)
  const currentRequestId = ref(null) // TTS request ID for the audio URL

  const notifTtsUrl = ref(null)         // active notifTts audio URL
  const notifTtsState = ref('idle')     // 'idle' | 'loading' | 'playing' | 'paused' | 'error'
  // Snapshot of interrupted story so we can offer resume
  const _interruptedStory = ref(null)   // { storyId, storyTitle, chapterFile, chapterFiles, index }

  const generationStatus = ref({}) // { [chapterFile]: 'generating' | 'ready' | 'none' }

  const isFullPlayerOpen = ref(false)
  const isMiniPlayerVisible = ref(false)

  const pendingSeekPosition = ref(null) // Set when a seek is needed after audio loads

  let _apiSaveTimer = null

  let selectionRevision = 0
  const queue = createChapterQueue((storyId, file, signal) =>
    apiFetch(`/api/stories/${encodeURIComponent(storyId)}/${encodeURIComponent(file)}/prepare`, { method: 'POST', signal }),
    error => console.warn('[AudioStore] Next chapter preparation failed:', error))
  watch([currentStoryId, currentChapterFile, playbackType], () => {
    selectionRevision++
    queue.clear()
  }, { flush: 'sync' })

  const canPlayPrev = computed(() => currentIndex.value > 0)
  const canPlayNext = computed(() => currentIndex.value < chapterFiles.value.length - 1)

  const progressPercent = computed(() => {
    if (!duration.value || duration.value <= 0) return 0
    return Math.min((currentTime.value / duration.value) * 100, 100)
  })

  const formattedTime = computed(() => _formatTime(currentTime.value))
  const formattedDuration = computed(() => {
    return duration.value > 0 ? _formatTime(duration.value) : '--:--'
  })

  const currentChapterLabel = computed(() => {
    if (!currentChapterFile.value) return ''
    return _chapterLabel(currentChapterFile.value)
  })

  const chapterProgress = computed(() => {
    if (chapterFiles.value.length === 0 || currentIndex.value < 0) return ''
    return `${currentIndex.value + 1} / ${chapterFiles.value.length}`
  })

  async function playChapter(storyId, storyTitle, filename, allChapterFiles = [], prepared = null) {
    const restorePlaylist = !allChapterFiles.length &&
      (currentStoryId.value !== storyId || !chapterFiles.value.includes(filename))
    queue.clear()
    if (currentRequestId.value && currentChapterFile.value !== filename) {
      isPlaying.value = false
      isPaused.value = false
    }

    if (currentStoryId.value !== storyId || currentChapterFile.value !== filename) {
      currentTime.value = 0
      duration.value = 0
      pendingSeekPosition.value = null
    }

    playbackType.value = 'story'
    currentStoryId.value = storyId
    currentStoryTitle.value = storyTitle
    currentChapterFile.value = filename
    if (allChapterFiles.length > 0) {
      chapterFiles.value = allChapterFiles
    }
    currentIndex.value = chapterFiles.value.indexOf(filename)
    isBuffering.value = true
    isMiniPlayerVisible.value = true

    const revision = ++selectionRevision
    currentAudioReady.value = false

    // Update generation status to 'generating' optimistically
    generationStatus.value = { ...generationStatus.value, [filename]: 'generating' }

    try {
      if (restorePlaylist) {
        const chapters = await apiFetch(`/api/stories/${encodeURIComponent(storyId)}/chapters`)
        if (revision !== selectionRevision) return null
        if (!Array.isArray(chapters) || !chapters.some(ch => ch.file === filename)) throw new Error('Chapter is unavailable')
        chapterFiles.value = chapters.map(ch => ch.file)
        currentIndex.value = chapterFiles.value.indexOf(filename)
      }
      const data = prepared || await apiFetch(`/api/stories/${encodeURIComponent(storyId)}/${encodeURIComponent(filename)}/play`, {
        method: 'POST',
      })
      if (revision !== selectionRevision) return null

      if (data.error) {
        throw new Error(data.error)
      }

      if (!data.audio_url) throw new Error('Audio source is unavailable')
      currentRequestId.value = data.audio_url.replace('/api/tts/', '')
      currentAudioReady.value = data.status === 'ready'
      if (Number.isFinite(data.duration)) duration.value = data.duration
      // Publish the source LAST: the engine's synchronous watcher sees a
      // complete chapter snapshot and calls native play in this same task.
      currentAudioUrl.value = data.audio_url
      queue.prepare(storyId, filename, chapterFiles.value[currentIndex.value + 1])
      if (prepared) {
        // Progress is bookkeeping after native play, never a transition gate.
        Promise.resolve().then(() => {
          if (revision === selectionRevision) return apiFetch(`/api/stories/${encodeURIComponent(storyId)}/${encodeURIComponent(filename)}/play`, { method: 'POST' })
        }).catch(error => console.warn('[AudioStore] Progress update failed:', error))
      }

      // Update generation status
      if (data.status === 'ready') {
        generationStatus.value = { ...generationStatus.value, [filename]: 'ready' }
      }

      // Start API progress saving timer
      _startApiSaveTimer()

      return { audioUrl: data.audio_url, duration: data.duration }
    } catch (err) {
      if (revision !== selectionRevision) return null
      isBuffering.value = false
      isPlaying.value = false
      isPaused.value = true
      currentAudioUrl.value = null
      currentRequestId.value = null
      console.error('[AudioStore] playChapter error:', err)
      throw err
    }
  }

  function playChatTts(audioUrl) {
    playbackType.value = 'chatTts'
    currentStoryId.value = null
    currentStoryTitle.value = 'Chat'
    currentChapterFile.value = null
    chapterFiles.value = []
    currentIndex.value = -1
    currentRequestId.value = null
    currentTime.value = 0
    duration.value = 0
    isBuffering.value = true
    isMiniPlayerVisible.value = true
    // Setting the url triggers the useAudioPlayer watcher → playback.
    currentAudioUrl.value = audioUrl
  }

  function playFromChat(event, ttsEnabled) {
    const s = event?.story
    if (s && s.story_id && s.chapter_file) {
      playChapter(s.story_id, s.story_title || s.story_id, s.chapter_file, s.chapter_files || [])
    } else if (ttsEnabled && event?.audio) {
      playChatTts(event.audio)
    }
  }

  async function nextChapter() {
    if (!canPlayNext.value) {
      // End of playlist
      stopAndReset()
      return null
    }
    const nextFile = chapterFiles.value[currentIndex.value + 1]
    const prepared = queue.take(currentStoryId.value, currentChapterFile.value, nextFile)
    return playChapter(currentStoryId.value, currentStoryTitle.value, nextFile, chapterFiles.value, prepared)
  }

  async function prevChapter() {
    if (!canPlayPrev.value) return null
    const prevFile = chapterFiles.value[currentIndex.value - 1]
    return playChapter(currentStoryId.value, currentStoryTitle.value, prevFile, chapterFiles.value)
  }

  function togglePlayPause() {
    if (isPlaying.value) {
      isPlaying.value = false
      isPaused.value = true
    } else if (isPaused.value) {
      // Restored from localStorage without audio URL — need to re-fetch TTS URL
      // Only call playChapter for local stories (type 'story'). Library books can't be
      // re-opened via this path (they lack a storyId-as-folder on disk).
      if (!currentAudioUrl.value && playbackType.value === 'story' && currentStoryId.value && currentChapterFile.value) {
        playChapter(currentStoryId.value, currentStoryTitle.value, currentChapterFile.value, chapterFiles.value)
        return
      }
      isPlaying.value = true
      isPaused.value = false
    }
  }

  const seekTarget = ref(null)
  function seekTo(seconds) {
    const clamped = Math.max(0, Math.min(seconds, duration.value || Infinity))
    seekTarget.value = clamped
    // Save immediately so reload doesn't lose position
    currentTime.value = clamped
    saveProgress()
  }

  function skipForward(seconds = 30) {
    seekTo(currentTime.value + seconds)
  }

  function skipBackward(seconds = 10) {
    seekTo(currentTime.value - seconds)
  }

  const SPEED_OPTIONS = [0.75, 1.0, 1.25, 1.5, 2.0]
  function setSpeed(rate) {
    playbackSpeed.value = rate
    localStorage.setItem(SPEED_STORAGE_KEY, String(rate))
  }

  function cycleSpeed() {
    const idx = SPEED_OPTIONS.indexOf(playbackSpeed.value)
    const next = SPEED_OPTIONS[(idx + 1) % SPEED_OPTIONS.length]
    setSpeed(next)
  }

  function stopAndReset() {
    _stopApiSaveTimer()
    // Explicit close (X button, end-of-playlist, audio-type swap) — drop
    // the persisted progress so `initFromSavedProgress()` doesn't resurrect
    // the mini-player on next page load. Mid-session refresh is unaffected
    // because the 15s API save timer + seekTo() still keep localStorage in
    // sync while the player is alive.
    try { localStorage.removeItem(STORAGE_KEY) } catch (_) { /* ignore quota */ }

    // Also stop notifTts if active
    if (playbackType.value === 'notifTts') {
      notifTtsUrl.value = null
      notifTtsState.value = 'idle'
    }

    playbackType.value = 'none'
    isPlaying.value = false
    isPaused.value = false
    isBuffering.value = false
    currentTime.value = 0
    duration.value = 0
    currentAudioUrl.value = null
    currentRequestId.value = null
    currentStoryId.value = null
    currentStoryTitle.value = null
    currentChapterFile.value = null
    chapterFiles.value = []
    currentIndex.value = -1
    isMiniPlayerVisible.value = false
    isFullPlayerOpen.value = false
    seekTarget.value = null
    _interruptedStory.value = null
  }

  function updateTime(time) {
    currentTime.value = time
  }

  function updateDuration(dur) {
    duration.value = dur
    isBuffering.value = false
    // NOTE: do NOT mark the chapter 'ready' here. While a chapter streams
    // live, the browser reports a duration from the bytes received so far —
    // marking 'ready' on that would flip the chapter-list badge to green
    // before generation actually finishes. The authoritative 'ready' signal
    // is the pregen SSE 'chapter_ready' event (usePregenStream) / the play
    // API's status:'ready'. Leave generationStatus untouched here.
  }

  function setPlayingState(playing) {
    isPlaying.value = playing
    isPaused.value = !playing
    isBuffering.value = false
  }

  function setBuffering(buffering) {
    isBuffering.value = buffering
  }

  function updateChapterStatus(filename, status) {
    generationStatus.value = { ...generationStatus.value, [filename]: status }
  }

  function updateBatchChapterStatus(chapters) {
    const updated = { ...generationStatus.value }
    for (const ch of chapters) {
      updated[ch.file] = ch.preload
    }
    generationStatus.value = updated
  }

  function _notifShouldInterrupt() {
    if (playbackType.value === 'none') return true
    if (playbackType.value === 'story' && isPlaying.value) return false // story wins
    return true // chatTts, libraryBook, idle → allow
  }

  function startNotifTts(audioUrl) {
    if (!_notifShouldInterrupt()) {
      // Story is playing → don't interrupt, just surface a hint
      notifTtsState.value = 'blocked'
      return false
    }

    // If non-story audio was playing (chatTts / libraryBook / another notifTts) — stop it
    if (playbackType.value !== 'none' && playbackType.value !== 'story') {
      stopAndReset()
    }

    // If story was paused (not playing), snapshot it so composable can pause cleanly
    if (playbackType.value === 'story') {
      _interruptedStory.value = {
        storyId: currentStoryId.value,
        storyTitle: currentStoryTitle.value,
        chapterFile: currentChapterFile.value,
        chapterFiles: [...chapterFiles.value],
        index: currentIndex.value,
      }
      // Pause story — composable watches isPlaying
      isPlaying.value = false
      isPaused.value = true
    }

    notifTtsUrl.value = audioUrl
    notifTtsState.value = 'playing'
    playbackType.value = 'notifTts'
    return true
  }

  function stopNotifTts() {
    notifTtsUrl.value = null
    notifTtsState.value = 'idle'
    if (playbackType.value === 'notifTts') {
      // Restore story state if one was interrupted
      if (_interruptedStory.value) {
        const snap = _interruptedStory.value
        _interruptedStory.value = null
      playbackType.value = 'story'
        currentStoryId.value = snap.storyId
        currentStoryTitle.value = snap.storyTitle
        currentChapterFile.value = snap.chapterFile
        chapterFiles.value = snap.chapterFiles
        currentIndex.value = snap.index
        isPlaying.value = false
        isPaused.value = true
        isMiniPlayerVisible.value = true
      } else {
        playbackType.value = 'none'
        isMiniPlayerVisible.value = false
      }
    }
  }

  function onNotifTtsEnded() {
    stopNotifTts()
  }

  function onNotifTtsError() {
    notifTtsState.value = 'error'
    stopNotifTts()
  }

  function saveProgress() {
    if (!currentStoryId.value || !currentChapterFile.value) return

    const data = {
      playbackType: playbackType.value, // persist so restore knows story vs libraryBook
      storyId: currentStoryId.value,
      storyTitle: currentStoryTitle.value,
      chapterFile: currentChapterFile.value,
      position: currentTime.value,
      duration: duration.value,
      speed: playbackSpeed.value,
      timestamp: Date.now(),
    }

    // Always save to localStorage
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(data))
    } catch (_) { /* quota exceeded — ignore */ }
  }

  async function _saveProgressToApi() {
    if (!currentRequestId.value) return
    try {
      await apiFetch('/api/library/progress', {
        method: 'POST',
        body: JSON.stringify({
          id: currentRequestId.value,
          current_time: Math.floor(currentTime.value),
        }),
      })
    } catch (_) { /* silent fail — will retry in 15s */ }
  }

  function _startApiSaveTimer() {
    _stopApiSaveTimer()
    _apiSaveTimer = setInterval(() => {
      if (isPlaying.value) {
        saveProgress()
        _saveProgressToApi()
      }
    }, API_SAVE_INTERVAL)
  }

  function _stopApiSaveTimer() {
    if (_apiSaveTimer) {
      clearInterval(_apiSaveTimer)
      _apiSaveTimer = null
    }
  }

  function restoreProgress() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY)
      if (!raw) return null
      const data = JSON.parse(raw)
      // Only restore if data is < 24h old
      if (Date.now() - data.timestamp > 24 * 60 * 60 * 1000) return null
      return data
    } catch (_) {
      return null
    }
  }

  function initFromSavedProgress() {
    const saved = restoreProgress()
    if (!saved) return null

    // Restore store state (but don't play)
    // Use saved playbackType if present, default 'story' for backward compat
    playbackType.value = saved.playbackType || 'story'
    currentStoryId.value = saved.storyId
    currentStoryTitle.value = saved.storyTitle
    currentChapterFile.value = saved.chapterFile
    currentTime.value = saved.position || 0
    duration.value = saved.duration || 0
    isPaused.value = true
    isPlaying.value = false
    isMiniPlayerVisible.value = true

    // Set pending seek — composable will apply when audio loads
    if (saved.position > 0) {
      pendingSeekPosition.value = saved.position
    }

    // Restore speed if present
    if (saved.speed) {
      playbackSpeed.value = saved.speed
    }

    return saved
  }

  function _formatTime(seconds) {
    if (!seconds || isNaN(seconds)) return '0:00'
    const s = Math.floor(seconds)
    const h = Math.floor(s / 3600)
    const m = Math.floor((s % 3600) / 60)
    const sec = s % 60
    if (h > 0) {
      return `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
    }
    return `${m}:${String(sec).padStart(2, '0')}`
  }

  function _chapterLabel(filename) {
    // Example: "0454_huyet_chien_vo_song.txt" → "Ch.454: Huyet Chien Vo Song"
    const name = filename.replace('.txt', '')
    const parts = name.split('_')
    const numStr = parts[0]
    const num = parseInt(numStr, 10)
    if (isNaN(num)) return name
    const title = parts.slice(1).map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ')
    return title ? `Ch.${num}: ${title}` : `Ch.${num}`
  }

  function _loadSpeed() {
    const saved = localStorage.getItem(SPEED_STORAGE_KEY)
    return saved ? parseFloat(saved) : 1.0
  }

  return {
    // State
    playbackType,
    isPlaying,
    isPaused,
    isBuffering,
    currentTime,
    duration,
    playbackSpeed,
    currentStoryId,
    currentStoryTitle,
    currentChapterFile,
    chapterFiles,
    currentIndex,
    currentAudioUrl,
    currentAudioReady,
    currentRequestId,
    generationStatus,
    isFullPlayerOpen,
    isMiniPlayerVisible,
    seekTarget,
    pendingSeekPosition,
    // NotifTts
    notifTtsUrl,
    notifTtsState,

    // Computed
    canPlayPrev,
    canPlayNext,
    progressPercent,
    formattedTime,
    formattedDuration,
    currentChapterLabel,
    chapterProgress,
    SPEED_OPTIONS,

    // Actions
    playChapter,
    playChatTts,
    playFromChat,
    nextChapter,
    prevChapter,
    togglePlayPause,
    seekTo,
    skipForward,
    skipBackward,
    setSpeed,
    cycleSpeed,
    stopAndReset,
    // NotifTts actions
    startNotifTts,
    stopNotifTts,
    onNotifTtsEnded,
    onNotifTtsError,

    // Internal — called by useAudioPlayer composable
    updateTime,
    updateDuration,
    setPlayingState,
    setBuffering,
    updateChapterStatus,
    updateBatchChapterStatus,
    saveProgress,
    restoreProgress,
    initFromSavedProgress,
  }
})

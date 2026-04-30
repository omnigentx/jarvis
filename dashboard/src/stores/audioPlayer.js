/**
 * Audio Player Pinia Store
 *
 * Quản lý toàn bộ state phát audio truyện:
 * - Playback state (play/pause/buffering)
 * - Playlist (danh sách chương, vị trí hiện tại)
 * - Progress saving (localStorage + API mỗi 15s)
 * - TTS generation status tracking
 * - Speed control (persist qua chapters)
 */
import { ref, computed } from 'vue'
import { defineStore } from 'pinia'
import { apiFetch } from '../api'

const STORAGE_KEY = 'jarvis_audio_progress'
const SPEED_STORAGE_KEY = 'jarvis_audio_speed'
const API_SAVE_INTERVAL = 15_000 // 15s

export const useAudioPlayerStore = defineStore('audioPlayer', () => {
  // ─── Playback state ───
  const playbackType = ref('none') // 'none' | 'story' | 'chatTts' | 'libraryBook' | 'notifTts'
  const isPlaying = ref(false)
  const isPaused = ref(false)
  const isBuffering = ref(false)
  const currentTime = ref(0) // seconds
  const duration = ref(0) // seconds (0 = unknown)
  const playbackSpeed = ref(_loadSpeed())

  // ─── Story playlist ───
  const currentStoryId = ref(null)
  const currentStoryTitle = ref(null)
  const currentChapterFile = ref(null)
  const chapterFiles = ref([]) // ordered list of chapter filenames
  const currentIndex = ref(-1)

  // ─── Audio source ───
  const currentAudioUrl = ref(null)
  const currentRequestId = ref(null) // TTS request ID for the audio URL

  // ─── NotifTts state (interrupt-able by story) ───
  const notifTtsUrl = ref(null)         // active notifTts audio URL
  const notifTtsState = ref('idle')     // 'idle' | 'loading' | 'playing' | 'paused' | 'error'
  // Snapshot of interrupted story so we can offer resume
  const _interruptedStory = ref(null)   // { storyId, storyTitle, chapterFile, chapterFiles, index }

  // ─── TTS generation status ───
  const generationStatus = ref({}) // { [chapterFile]: 'generating' | 'ready' | 'none' }

  // ─── UI ───
  const isFullPlayerOpen = ref(false)
  const isMiniPlayerVisible = ref(false)

  // ─── Resume support ───
  const pendingSeekPosition = ref(null) // Set khi cần seek sau khi audio load

  // ─── API progress timer ───
  let _apiSaveTimer = null

  // ─── Computed ───
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

  // ─── Actions ───

  /**
   * Play một chương truyện.
   * @param {string} storyId - ID thư mục truyện
   * @param {string} storyTitle - Tên truyện hiển thị
   * @param {string} filename - Tên file chương (.txt)
   * @param {string[]} allChapterFiles - Danh sách tất cả chapter files
   * @returns {Promise<{audioUrl: string}>}
   */
  async function playChapter(storyId, storyTitle, filename, allChapterFiles = []) {
    // Cancel previous if different
    if (currentRequestId.value && currentChapterFile.value !== filename) {
      isPlaying.value = false
      isPaused.value = false
    }

    // Update playlist state
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

    // Update generation status to 'generating' optimistically
    generationStatus.value = { ...generationStatus.value, [filename]: 'generating' }

    try {
      const data = await apiFetch(`/api/stories/${encodeURIComponent(storyId)}/${encodeURIComponent(filename)}/play`, {
        method: 'POST',
      })

      if (data.error) {
        throw new Error(data.error)
      }

      currentAudioUrl.value = data.audio_url
      currentRequestId.value = data.audio_url.replace('/api/tts/', '')

      // Update generation status
      if (data.status === 'ready') {
        generationStatus.value = { ...generationStatus.value, [filename]: 'ready' }
      }

      // Start API progress saving timer
      _startApiSaveTimer()

      return { audioUrl: data.audio_url, duration: data.duration }
    } catch (err) {
      isBuffering.value = false
      console.error('[AudioStore] playChapter error:', err)
      throw err
    }
  }

  /**
   * Phát từ playlist — chương tiếp theo.
   */
  async function nextChapter() {
    if (!canPlayNext.value) {
      // Hết playlist
      stopAndReset()
      return null
    }
    const nextFile = chapterFiles.value[currentIndex.value + 1]
    return playChapter(currentStoryId.value, currentStoryTitle.value, nextFile, chapterFiles.value)
  }

  /**
   * Phát chương trước.
   */
  async function prevChapter() {
    if (!canPlayPrev.value) return null
    const prevFile = chapterFiles.value[currentIndex.value - 1]
    return playChapter(currentStoryId.value, currentStoryTitle.value, prevFile, chapterFiles.value)
  }

  /**
   * Toggle play/pause. Composable useAudioPlayer sẽ listen state này.
   * Nếu đang ở trạng thái restored (có storyId nhưng chưa có audioUrl),
   * gọi playChapter() thay vì chỉ toggle.
   */
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

  /**
   * Seek đến vị trí cụ thể (giây).
   * Composable sẽ watch và apply lên audio element.
   */
  const seekTarget = ref(null)
  function seekTo(seconds) {
    const clamped = Math.max(0, Math.min(seconds, duration.value || Infinity))
    seekTarget.value = clamped
    // Lưu ngay để reload không mất vị trí
    currentTime.value = clamped
    saveProgress()
  }

  function skipForward(seconds = 30) {
    seekTo(currentTime.value + seconds)
  }

  function skipBackward(seconds = 10) {
    seekTo(currentTime.value - seconds)
  }

  /**
   * Thay đổi tốc độ phát. Persist vào localStorage.
   */
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

  /**
   * Dừng phát, reset state, ẩn player.
   */
  function stopAndReset() {
    _stopApiSaveTimer()
    saveProgress() // Lưu trước khi reset

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

  /**
   * Update time từ audio element events.
   * Được gọi bởi composable useAudioPlayer.
   */
  function updateTime(time) {
    currentTime.value = time
  }

  function updateDuration(dur) {
    duration.value = dur
    isBuffering.value = false
    generationStatus.value = { ...generationStatus.value, [currentChapterFile.value]: 'ready' }
  }

  function setPlayingState(playing) {
    isPlaying.value = playing
    isPaused.value = !playing
    isBuffering.value = false
  }

  function setBuffering(buffering) {
    isBuffering.value = buffering
  }

  /**
   * Update generation status cho chapter list display.
   */
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

  // ─── NotifTts: play notification content as TTS ───

  /**
   * Priority logic — mirrors Flutter AudioService._shouldInterrupt().
   * story > notifTts: never interrupt a playing story.
   */
  function _notifShouldInterrupt() {
    if (playbackType.value === 'none') return true
    if (playbackType.value === 'story' && isPlaying.value) return false // story wins
    return true // chatTts, libraryBook, idle → allow
  }

  /**
   * Kick off notifTts playback.
   * @param {string} audioUrl - relative URL returned by /api/tts/prepare
   * Returns false if skipped because story is playing.
   */
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

  /**
   * Stop notifTts, clean up URL. Composable will stop audio element.
   * If a story was interrupted, it remains paused — user can resume from mini-player.
   */
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

  /**
   * Called by composable when notifTts audio naturally ends.
   */
  function onNotifTtsEnded() {
    stopNotifTts()
  }

  /**
   * Called by composable when notifTts audio errors.
   */
  function onNotifTtsError() {
    notifTtsState.value = 'error'
    stopNotifTts()
  }

  // ─── Progress saving ───

  /**
   * Lưu progress vào localStorage + gọi API (mỗi 15s).
   */
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

  /**
   * Gọi API lưu progress (cho cross-device resume).
   */
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

  /**
   * Khôi phục progress từ localStorage khi khởi tạo.
   * Trả về thông tin để composable resume.
   */
  function restoreProgress() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY)
      if (!raw) return null
      const data = JSON.parse(raw)
      // Chỉ restore nếu dữ liệu < 24h tuổi
      if (Date.now() - data.timestamp > 24 * 60 * 60 * 1000) return null
      return data
    } catch (_) {
      return null
    }
  }

  /**
   * Khôi phục UI state từ localStorage khi page load.
   * Hiện mini player ở trạng thái paused, sẵn sàng resume.
   * Gọi 1 lần bởi useAudioPlayer composable.
   */
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

    // Set pending seek — composable sẽ apply khi audio load
    if (saved.position > 0) {
      pendingSeekPosition.value = saved.position
    }

    // Restore speed nếu có
    if (saved.speed) {
      playbackSpeed.value = saved.speed
    }

    return saved
  }

  // ─── Helpers ───

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
    // "0454_huyet_chien_vo_song.txt" → "Ch.454: Huyết Chiến Vô Song"
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

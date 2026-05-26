/**
 * useAudioPlayer — Composable wrap HTML5 Audio API
 *
 * Responsibilities:
 * - Create and manage HTML5 Audio element
 * - Sync events (timeupdate, ended, error, etc.) → Pinia store
 * - Watch store.seekTarget → apply seek on audio
 * - Watch store.playbackSpeed → apply playbackRate
 * - BroadcastChannel multi-tab sync
 * - MediaSession API (lock screen controls)
 * - Interval to save progress to localStorage
 * - Beforeunload to save progress + sendBeacon API
 *
 * Usage:
 *   const { play, pause, resume, destroy } = useAudioPlayer()
 */
import { watch, onUnmounted, nextTick } from 'vue'
import { useAudioPlayerStore } from '../stores/audioPlayer'

// Singleton audio element — only one audio playback at a time
let _audio = null
let _progressInterval = null
let _broadcastChannel = null
const BROADCAST_CHANNEL_NAME = 'jarvis-audio'

export function useAudioPlayer() {
  const store = useAudioPlayerStore()

  // ─── BroadcastChannel: multi-tab sync ───
  _initBroadcastChannel()

  // ─── Init: Restore saved progress on first mount ───
  const savedProgress = store.initFromSavedProgress()
  if (savedProgress) {
    console.info('[AudioPlayer] Restored progress:', savedProgress.chapterFile, '@', Math.floor(savedProgress.position), 's')
  }

  // ─── Watch: when store has a new audioUrl → play ───
  const stopWatchUrl = watch(
    () => store.currentAudioUrl,
    (url) => {
      if (url) {
        _playUrl(url)
      }
    },
  )

  // ─── Watch: notifTtsUrl → play inline TTS without touching story state ───
  const stopWatchNotifTts = watch(
    () => store.notifTtsUrl,
    (url) => {
      if (url) {
        _playNotifTts(url)
      } else {
        // URL cleared → stop audio if we're still in notifTts mode
        if (store.playbackType === 'notifTts' && _audio) {
          _audio.pause()
          _audio.src = ''
          _audio.load()
        }
      }
    },
  )

  // ─── Watch: toggle play/pause from store ───
  const stopWatchPlaying = watch(
    () => store.isPlaying,
    (playing) => {
      if (!_audio) return
      if (playing && _audio.paused) {
        _audio.play().catch(() => {})
      } else if (!playing && !_audio.paused) {
        _audio.pause()
      }
    },
  )

  // ─── Watch: seek target ───
  const stopWatchSeek = watch(
    () => store.seekTarget,
    (target) => {
      if (target !== null && _audio) {
        _audio.currentTime = target
        store.updateTime(target)
        store.seekTarget = null
      }
    },
  )

  // ─── Watch: playback speed ───
  const stopWatchSpeed = watch(
    () => store.playbackSpeed,
    (speed) => {
      if (_audio) {
        _audio.playbackRate = speed
      }
    },
  )

  // ─── Watch: Full stop (stopAndReset) ───
  const stopWatchType = watch(
    () => store.playbackType,
    (type) => {
      if (type === 'none' && _audio) {
        _audio.pause()
        _audio.src = ''
        _audio.load()
      }
    },
  )

  // ─── Core: Play a URL (story / chatTts) ───
  function _playUrl(url) {
    // ``<audio src>`` cannot set headers; auth rides on the cookie that
    // ``credentials: 'include'`` would attach to a fetch. For same-origin
    // GETs the browser attaches the cookie automatically, so we just
    // need a cache-busting timestamp.
    const fullUrl = `${url}${url.includes('?') ? '&' : '?'}t=${Date.now()}`

    if (!_audio) {
      _audio = new Audio()
      _audio.preload = 'auto'
      _bindEvents(_audio)
    } else {
      _audio.pause()
    }

    _audio.src = fullUrl
    _audio.playbackRate = store.playbackSpeed
    _audio.play().then(() => {
      store.setPlayingState(true)
      _setupMediaSession()
      _startProgressSaver()
      _broadcastPlay()
    }).catch(err => {
      console.error('[AudioPlayer] Play failed:', err)
      // iOS Safari: requires user gesture
      if (err.name === 'NotAllowedError') {
        store.setBuffering(false)
        store.isPaused = true
      }
    })
  }

  // ─── Core: Play notifTts URL (shares _audio singleton, doesn't touch story store state) ───
  function _playNotifTts(audioUrl) {
    // Same cookie-only auth as _playUrl above.
    const fullUrl = `${audioUrl}${audioUrl.includes('?') ? '&' : '?'}t=${Date.now()}`

    if (!_audio) {
      _audio = new Audio()
      _audio.preload = 'auto'
      _bindEvents(_audio)
    } else {
      _audio.pause()
    }

    _audio.src = fullUrl
    _audio.playbackRate = 1.0 // notifTts always plays at 1x
    _audio.play().then(() => {
      store.notifTtsState = 'playing'
    }).catch(err => {
      console.error('[AudioPlayer] NotifTts play failed:', err)
      store.onNotifTtsError()
    })
  }

  // ─── Audio events → store ───
  function _bindEvents(audio) {
    audio.addEventListener('timeupdate', () => {
      store.updateTime(audio.currentTime)
    })

    audio.addEventListener('loadedmetadata', () => {
      if (audio.duration && isFinite(audio.duration)) {
        store.updateDuration(audio.duration)
      }
    })

    audio.addEventListener('durationchange', () => {
      if (audio.duration && isFinite(audio.duration)) {
        store.updateDuration(audio.duration)
      }
    })

    audio.addEventListener('canplay', () => {
      store.setBuffering(false)
      // Apply pending seek (from saved progress restore)
      if (store.pendingSeekPosition !== null) {
        const pos = store.pendingSeekPosition
        audio.currentTime = pos
        store.updateTime(pos)
        store.pendingSeekPosition = null
      }
    })

    audio.addEventListener('waiting', () => {
      store.setBuffering(true)
    })

    audio.addEventListener('playing', () => {
      if (store.playbackType === 'notifTts') {
        store.notifTtsState = 'playing'
        return
      }
      store.setPlayingState(true)
    })

    audio.addEventListener('pause', () => {
      if (store.playbackType === 'notifTts') {
        // notifTts pause handled by _onNotifTtsPause
        return
      }
      if (store.playbackType !== 'none') {
        store.isPlaying = false
        store.isPaused = true
      }
    })

    audio.addEventListener('ended', () => {
      if (store.playbackType === 'notifTts') {
        store.onNotifTtsEnded()
        return
      }
      store.isPlaying = false
      store.isPaused = false
      store.saveProgress()
      // Auto-next chapter
      nextTick(() => {
        store.nextChapter()
      })
    })

    audio.addEventListener('error', (e) => {
      if (store.playbackType === 'notifTts') {
        store.onNotifTtsError()
        return
      }
      const err = audio.error
      console.error('[AudioPlayer] Audio error:', err?.code, err?.message)
      store.setBuffering(false)
      // Retry once after 2s for network errors
      if (err?.code === MediaError.MEDIA_ERR_NETWORK) {
        setTimeout(() => {
          if (store.currentAudioUrl) {
            _playUrl(store.currentAudioUrl)
          }
        }, 2000)
      }
    })
  }

  // ─── BroadcastChannel: pause other tabs ───
  function _initBroadcastChannel() {
    if (_broadcastChannel) return
    try {
      _broadcastChannel = new BroadcastChannel(BROADCAST_CHANNEL_NAME)
      _broadcastChannel.onmessage = (event) => {
        const { type } = event.data
        if (type === 'play' && _audio && !_audio.paused) {
          // Another tab started playing → pause this tab
          _audio.pause()
          store.isPlaying = false
          store.isPaused = true
        }
      }
    } catch (_) {
      // BroadcastChannel not supported — skip
      console.warn('[AudioPlayer] BroadcastChannel not supported')
    }
  }

  // ─── NotifTts pause/resume via window events ───
  // NotificationDetail fires these so it can pause/resume without reaching _audio directly.
  function _onNotifTtsPause() {
    if (store.playbackType === 'notifTts' && _audio && !_audio.paused) {
      _audio.pause()
      store.notifTtsState = 'paused'
    }
  }

  function _onNotifTtsResume() {
    if (store.playbackType === 'notifTts' && _audio) {
      _audio.play().then(() => {
        store.notifTtsState = 'playing'
      }).catch(() => {})
    }
  }

  window.addEventListener('notif-tts-pause', _onNotifTtsPause)
  window.addEventListener('notif-tts-resume', _onNotifTtsResume)

  function _broadcastPlay() {
    try {
      _broadcastChannel?.postMessage({ type: 'play', tabId: _tabId })
    } catch (_) {}
  }

  const _tabId = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`

  // ─── MediaSession API (lock screen controls) ───
  function _setupMediaSession() {
    if (!('mediaSession' in navigator)) return

    navigator.mediaSession.metadata = new MediaMetadata({
      title: store.currentChapterLabel || store.currentChapterFile || 'Audio',
      artist: store.currentStoryTitle || 'Jarvis Stories',
      album: 'Jarvis Audio Reader',
    })

    navigator.mediaSession.setActionHandler('play', () => {
      _audio?.play()
      store.setPlayingState(true)
    })

    navigator.mediaSession.setActionHandler('pause', () => {
      _audio?.pause()
      store.isPlaying = false
      store.isPaused = true
    })

    navigator.mediaSession.setActionHandler('previoustrack', () => {
      store.prevChapter()
    })

    navigator.mediaSession.setActionHandler('nexttrack', () => {
      store.nextChapter()
    })

    navigator.mediaSession.setActionHandler('seekto', (details) => {
      if (details.seekTime != null && _audio) {
        _audio.currentTime = details.seekTime
        store.updateTime(details.seekTime)
      }
    })

    navigator.mediaSession.setActionHandler('seekbackward', (details) => {
      const offset = details.seekOffset || 10
      store.skipBackward(offset)
    })

    navigator.mediaSession.setActionHandler('seekforward', (details) => {
      const offset = details.seekOffset || 30
      store.skipForward(offset)
    })
  }

  // ─── Progress saving interval (localStorage every 10s) ───
  function _startProgressSaver() {
    _stopProgressSaver()
    _progressInterval = setInterval(() => {
      if (store.isPlaying) {
        store.saveProgress()
      }
    }, 10_000)
  }

  function _stopProgressSaver() {
    if (_progressInterval) {
      clearInterval(_progressInterval)
      _progressInterval = null
    }
  }

  // ─── Beforeunload: save progress before tab close ───
  function _onBeforeUnload() {
    store.saveProgress()
    // sendBeacon for progress API. Same-origin POST → browser attaches
    // the session cookie automatically; no API key in the URL.
    if (store.currentRequestId) {
      try {
        const body = JSON.stringify({
          id: store.currentRequestId,
          current_time: Math.floor(store.currentTime),
        })
        navigator.sendBeacon(
          '/api/library/progress',
          new Blob([body], { type: 'application/json' }),
        )
      } catch (_) {}
    }
  }
  window.addEventListener('beforeunload', _onBeforeUnload)

  // ─── Public API ───
  function play(url) {
    _playUrl(url)
  }

  function pause() {
    _audio?.pause()
    store.isPlaying = false
    store.isPaused = true
  }

  function resume() {
    _audio?.play().catch(() => {})
    store.setPlayingState(true)
  }

  function destroy() {
    _stopProgressSaver()
    stopWatchUrl()
    stopWatchNotifTts()
    stopWatchPlaying()
    stopWatchSeek()
    stopWatchSpeed()
    stopWatchType()
    window.removeEventListener('beforeunload', _onBeforeUnload)
    window.removeEventListener('notif-tts-pause', _onNotifTtsPause)
    window.removeEventListener('notif-tts-resume', _onNotifTtsResume)
    if (_audio) {
      _audio.pause()
      _audio.src = ''
      _audio = null
    }
    _broadcastChannel?.close()
    _broadcastChannel = null
  }

  // Cleanup on component unmount
  onUnmounted(() => {
    // Don't destroy the audio — it should persist across route changes.
    // Only cleanup watches & event listeners.
    stopWatchUrl()
    stopWatchNotifTts()
    stopWatchPlaying()
    stopWatchSeek()
    stopWatchSpeed()
    stopWatchType()
  })

  return {
    play,
    pause,
    resume,
    destroy,
  }
}

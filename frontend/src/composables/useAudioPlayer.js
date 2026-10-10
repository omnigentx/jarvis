/** Singleton HTML audio + store sync, progress persistence and media controls. */
import { watch, onUnmounted } from 'vue'
import { useToast } from './useToast.js'
import { useLang } from './useLang.js'
import { useAudioPlayerStore } from '../stores/audioPlayer.js'
import { waitForAudioReady } from '../utils/audioGeneration.js'
import { configureAudioMediaSession } from '../utils/audioMediaSession.js'

// Singleton audio element — only one audio playback at a time
let _audio = null
let _progressInterval = null
let _broadcastChannel = null
const BROADCAST_CHANNEL_NAME = 'jarvis-audio'
// Network-error retry budget per playback. Reset on each successful play.
// Without a cap, a permanently-failing URL retries every 2s forever.
let _networkRetries = 0
const MAX_NETWORK_RETRIES = 3

export function useAudioPlayer() {
  const store = useAudioPlayerStore()
  const toast = useToast()
  const { t } = useLang()
  let generationWait = null
  let retryTimer = null
  let completionObservation = null
  let endedSource = null

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
    { flush: 'sync' },
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
      if (type === 'none') { generationWait?.abort(); completionObservation?.abort(); clearTimeout(retryTimer) }
      if (type === 'none' && _audio) {
        _audio.pause()
        _audio.src = ''
        _audio.load()
      }
    },
  )

  // ─── Core: Play a URL (story / chatTts) ───
  function _playUrl(url, retry = false) {
    generationWait?.abort()
    completionObservation?.abort()
    completionObservation = null
    endedSource = null
    if (!retry) { clearTimeout(retryTimer); _networkRetries = 0 }
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
      if (store.currentAudioUrl !== url) return
      store.setPlayingState(true)
      _setupMediaSession()
      _startProgressSaver()
      _broadcastPlay()
    }).catch(err => {
      console.error('[AudioPlayer] Play failed:', err)
      if (store.currentAudioUrl !== url || err.name === 'AbortError') return
      store.setPlayingState(false)
      // iOS Safari: requires user gesture
      if (err.name === 'NotAllowedError') {
        store.setBuffering(false)
        store.isPaused = true
      } else {
        store.currentAudioUrl = null // Next user retry reopens the source.
      }
    })
  }

  // Probe whether a story's TTS audio is still being generated server-side.
  // A premature 'ended' on a live stream (buffer underrun) is NOT a real
  // end-of-chapter — the backend signals in-progress generation via the
  // X-TTS-Generating header on a HEAD. Returns true = still generating.
  async function _isStillGenerating(url) {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), 15_000)
    try {
      const res = await fetch(url, { method: 'HEAD', credentials: 'include', cache: 'no-store', signal: controller.signal })
      if (!res.ok) throw new Error(`Audio status ${res.status}`)
      return res.headers.get('X-TTS-Generating') === '1'
    } catch (_) {
      throw new Error('Unable to determine audio generation status')
    } finally { clearTimeout(timer) }
  }

  // On underrun, await pushed completion and resume from the saved position.
  async function _resumeAfterUnderrun() {
    const url = store.currentAudioUrl
    const resumeAt = store.currentTime
    store.setBuffering(true)
    generationWait?.abort()
    const controller = new AbortController()
    generationWait = controller
    try {
      await waitForAudioReady(url, { signal: controller.signal })
    } catch (error) {
      if (error.name !== 'AbortError' && store.currentAudioUrl === url) {
        store.setPlayingState(false)
        console.error('[AudioPlayer] Generation recovery failed:', error)
      }
      return
    }
    if (store.currentAudioUrl !== url) return
    // Reload the now-complete file and seek back to where we left off, so the
    // user hears the continuation — not a replay from the start.
    store.pendingSeekPosition = resumeAt
    _playUrl(url)
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
      if (store.playbackType === 'story' && !store.currentAudioReady && !completionObservation) {
        const url = store.currentAudioUrl
        const controller = new AbortController()
        completionObservation = controller
        waitForAudioReady(url, { signal: controller.signal }).then(() => {
          if (store.currentAudioUrl === url) store.currentAudioReady = true
        }).catch(error => {
          if (error.name !== 'AbortError') console.warn('[AudioPlayer] Completion observation failed:', error)
        })
      }
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

    audio.addEventListener('ended', _handleEnded)

    audio.addEventListener('error', (e) => {
      // Clearing src during close/destroy can emit a native error.
      if (store.playbackType === 'none') return
      if (store.playbackType === 'notifTts') {
        store.onNotifTtsError()
        return
      }
      const err = audio.error
      console.error('[AudioPlayer] Audio error:', err?.code, err?.message)
      store.setPlayingState(false)
      // Retry on network errors, but cap it — a permanently-broken URL must
      // not retry every 2s forever. After the budget is spent, surface the
      // failure (pause) instead of silently looping.
      if (err?.code === MediaError.MEDIA_ERR_NETWORK && _networkRetries < MAX_NETWORK_RETRIES) {
        _networkRetries++
        const failedUrl = store.currentAudioUrl
        retryTimer = setTimeout(() => {
          if (failedUrl && store.currentAudioUrl === failedUrl) _playUrl(failedUrl, true)
        }, 2000)
      } else if (err?.code === MediaError.MEDIA_ERR_NETWORK) {
        console.error('[AudioPlayer] Network retries exhausted — stopping playback')
        store.isPlaying = false
        store.isPaused = true
        store.currentAudioUrl = null
      } else {
        store.currentAudioUrl = null
      }
      if (!store.currentAudioUrl) {
        toast.error(t('audio.playbackFailed'), { description: t('audio.playbackRetry'), duration: 8000 })
      }
    })
  }

  function _advanceChapter() {
    store.isPlaying = false
    store.isPaused = false
    store.saveProgress()
    // Prepared source + sync URL watcher: native play happens in this task.
    const result = store.nextChapter()
    result?.catch(error => console.error('[AudioPlayer] Next chapter failed:', error))
  }

  function _handleEnded() {
    if (store.playbackType === 'notifTts') { store.onNotifTtsEnded(); return }
    if (!_audio?.ended) return
    const url = store.currentAudioUrl
    if (!url || endedSource === url) return
    endedSource = url
    if (store.playbackType !== 'story' || store.currentAudioReady) {
      _advanceChapter()
      return
    }
    // Only unknown/live sources need a network probe. Cached chapters never
    // give the OS an idle network round trip before starting the next track.
    _isStillGenerating(url).then(generating => {
      if (store.currentAudioUrl !== url) return
      if (generating) _resumeAfterUnderrun()
      else _advanceChapter()
    }).catch(error => {
      if (store.currentAudioUrl === url) store.setPlayingState(false)
      console.error('[AudioPlayer] Status probe failed:', error)
    })
  }

  function _onVisible() {
    if (document.visibilityState === 'visible' && _audio?.ended && store.playbackType === 'story') _handleEnded()
  }
  document.addEventListener('visibilitychange', _onVisible)
  window.addEventListener('pageshow', _onVisible)

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
    configureAudioMediaSession(store, () => _audio)
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
    generationWait?.abort()
    completionObservation?.abort()
    document.removeEventListener('visibilitychange', _onVisible)
    window.removeEventListener('pageshow', _onVisible)
    clearTimeout(retryTimer)
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

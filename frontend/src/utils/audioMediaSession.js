/** Native lock-screen controls are optional; missing actions never stop audio. */
export function configureAudioMediaSession(store, getAudio) {
  const session = globalThis.navigator?.mediaSession
  if (!session) return
  try {
    session.metadata = new MediaMetadata({
      title: store.currentChapterLabel || store.currentChapterFile || 'Audio',
      artist: store.currentStoryTitle || 'Jarvis Stories', album: 'Jarvis Audio Reader',
    })
    session.playbackState = 'playing'
  } catch (error) { console.warn('[AudioPlayer] Media metadata unsupported:', error.name) }
  const safely = action => () => {
    try { Promise.resolve(action()).catch(error => console.warn('[AudioPlayer] Media action failed:', error.name)) }
    catch (error) { console.warn('[AudioPlayer] Media action failed:', error.name) }
  }
  const actions = {
    play: safely(async () => {
      const audio = getAudio()
      if (!audio) return
      try { await audio.play(); store.setPlayingState(true) }
      catch (error) { store.setPlayingState(false); throw error }
    }),
    pause: () => { getAudio()?.pause(); store.isPlaying=false; store.isPaused=true; session.playbackState='paused' },
    previoustrack: safely(() => store.prevChapter()),
    nexttrack: safely(() => store.nextChapter()),
    seekto: details => { if (details.seekTime != null) store.seekTo(details.seekTime) },
    seekbackward: details => store.skipBackward(details.seekOffset || 10),
    seekforward: details => store.skipForward(details.seekOffset || 30),
  }
  for (const [action, handler] of Object.entries(actions)) {
    try { session.setActionHandler(action, handler) }
    catch (error) { console.info('[AudioPlayer] Media action unsupported:', action, error.name) }
  }
}

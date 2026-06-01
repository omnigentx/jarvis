/**
 * audioPlayer store — chat playback routing (single source of truth).
 *
 * Covers playFromChat()/playChatTts(): the ONE decision point that funnels
 * every chat surface's audio into the singleton player, so a story started
 * from chat behaves like one started from the library, and a plain TTS reply
 * can never overlap a story (only one audio element exists).
 */
import { test, beforeEach } from 'node:test'
import assert from 'node:assert/strict'
import { createPinia, setActivePinia } from 'pinia'

// The store reads localStorage at setup (_loadSpeed) — shim it for node.
globalThis.localStorage = {
  _m: {},
  getItem(k) { return k in this._m ? this._m[k] : null },
  setItem(k, v) { this._m[k] = String(v) },
  removeItem(k) { delete this._m[k] },
}

// playChapter() POSTs /api/stories/{id}/{file}/play — stub the response so
// the story branch resolves without a live backend.
globalThis.fetch = async () =>
  new Response(
    JSON.stringify({ audio_url: '/api/tts/story_X_001_X.txt', status: 'ready', duration: 12 }),
    { status: 200, headers: { 'content-type': 'application/json' } },
  )

const { useAudioPlayerStore } = await import('./audioPlayer.js')

beforeEach(() => {
  setActivePinia(createPinia())
})

// A story reply must become full story playback — and play even when the
// "read replies aloud" toggle is OFF, because the user explicitly asked to
// listen. (playChapter sets the playlist state synchronously, before its
// await, so we can assert it immediately.)
test('playFromChat: story reply → story playback, ignores read-aloud toggle', () => {
  const store = useAudioPlayerStore()
  store.playFromChat(
    {
      audio: '/api/tts/story_X_001_X.txt',
      story: { story_id: 'X', story_title: 'X', chapter_file: '001_X.txt', chapter_files: ['001_X.txt', '002_X.txt'] },
    },
    false, // read-aloud OFF
  )
  assert.equal(store.playbackType, 'story')
  assert.equal(store.currentStoryId, 'X')
  assert.equal(store.currentChapterFile, '001_X.txt')
  assert.deepEqual(store.chapterFiles, ['001_X.txt', '002_X.txt'])
  assert.equal(store.canPlayNext, true)
  assert.equal(store.isMiniPlayerVisible, true)
})

// A plain reply plays as ephemeral chatTts only when read-aloud is ON.
test('playFromChat: plain reply → chatTts when read-aloud is ON', () => {
  const store = useAudioPlayerStore()
  store.playFromChat({ audio: '/api/tts/abc' }, true)
  assert.equal(store.playbackType, 'chatTts')
  assert.equal(store.currentAudioUrl, '/api/tts/abc')
  assert.equal(store.currentRequestId, null) // ephemeral — no progress persistence
  assert.equal(store.isMiniPlayerVisible, true)
})

// Plain reply must stay silent when the user has read-aloud OFF.
test('playFromChat: plain reply is silent when read-aloud is OFF', () => {
  const store = useAudioPlayerStore()
  store.playFromChat({ audio: '/api/tts/abc' }, false)
  assert.equal(store.playbackType, 'none')
  assert.equal(store.isMiniPlayerVisible, false)
})

// Neither audio nor story → nothing happens.
test('playFromChat: no audio and no story → no-op', () => {
  const store = useAudioPlayerStore()
  store.playFromChat({}, true)
  assert.equal(store.playbackType, 'none')
})

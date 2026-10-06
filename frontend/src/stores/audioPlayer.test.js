/**
 * audioPlayer store — chat playback routing (single source of truth).
 *
 * Covers playFromChat()/playChatTts(): the ONE decision point that funnels
 * every chat surface's audio into the singleton player, so a story started
 * from chat behaves like one started from the library, and a plain TTS reply
 * can never overlap a story (only one audio element exists).
 */
import { test, beforeEach, afterEach } from 'node:test'
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

// The story branch starts a 15s API-save setInterval (audioPlayer.js
// _startApiSaveTimer), fired *after* playChapter's awaited fetch resolves —
// i.e. after the synchronous test body returns. Left running, it keeps Node's
// event loop alive and `node --test` never exits. Flush the pending play
// promise, then stopAndReset() (which clears the timer) on the same store
// instance — Pinia returns the singleton for the still-active pinia.
afterEach(async () => {
  await new Promise((resolve) => setTimeout(resolve, 0))
  useAudioPlayerStore().stopAndReset()
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

test('switching chapter clears old duration, time and pending seek', async () => {
  const store = useAudioPlayerStore()
  store.currentStoryId = 'old'
  store.currentChapterFile = '01.txt'
  store.currentTime = 10
  store.duration = 31
  store.pendingSeekPosition = 10
  await store.playChapter('new', 'Synthetic', '02.txt', ['02.txt'])
  assert.equal(store.currentTime, 0)
  assert.equal(store.duration, 12) // New source metadata, never the old 31s duration
  assert.equal(store.pendingSeekPosition, null)
})
test('restored same chapter keeps its saved seek position', async () => {
  const store = useAudioPlayerStore()
  store.currentStoryId = 'saved'
  store.currentChapterFile = '01.txt'
  store.pendingSeekPosition = 12
  await store.playChapter('saved', 'Synthetic', '01.txt', ['01.txt'])
  assert.equal(store.pendingSeekPosition, 12)
})


test('resume from mini-player reloads missing playlist without opening Stories', async () => {
  const oldFetch = globalThis.fetch
  globalThis.fetch = async path => new Response(JSON.stringify(
    path.endsWith('/chapters') ? [{file:'01.txt'}, {file:'02.txt'}] :
      {audio_url:'/api/tts/resume_01',status:'ready'}
  ), {headers:{'content-type':'application/json'}})
  try {
    const store = useAudioPlayerStore()
    store.currentStoryId = 'synthetic'
    store.currentChapterFile = '01.txt'
    store.pendingSeekPosition = 4
    await store.playChapter('synthetic','Synthetic','01.txt')
    assert.deepEqual(store.chapterFiles,['01.txt','02.txt'])
    assert.equal(store.canPlayNext,true)
    assert.equal(store.pendingSeekPosition,4)
  } finally { globalThis.fetch = oldFetch }
})

test('prepared next chapter switches source synchronously without a boundary request', async () => {
  const oldFetch = globalThis.fetch
  const calls = []
  globalThis.fetch = async path => {
    calls.push(path)
    return new Response(JSON.stringify({audio_url:path.includes('02.txt')?'/api/tts/second':'/api/tts/first',status:'ready'}),
      {headers:{'content-type':'application/json'}})
  }
  try {
    const store = useAudioPlayerStore()
    await store.playChapter('synthetic','Synthetic','01.txt',['01.txt','02.txt'])
    await new Promise(resolve=>setImmediate(resolve))
    const before = calls.length
    const next = store.nextChapter()
    assert.equal(store.currentAudioUrl,'/api/tts/second')
    assert.equal(calls.length,before)
    await next
  } finally { globalThis.fetch = oldFetch }
})

test('late play response cannot replace a newer selected chapter or restart after stop', async () => {
  const oldFetch=globalThis.fetch
  const pending=[]
  globalThis.fetch = path => new Promise(resolve=>pending.push({path,resolve}))
  const reply = url=>new Response(JSON.stringify({audio_url:url,status:'ready'}),{headers:{'content-type':'application/json'}})
  try {
    const store=useAudioPlayerStore()
    const one=store.playChapter('synthetic','Synthetic','01.txt',['01.txt','02.txt'])
    const two=store.playChapter('synthetic','Synthetic','02.txt',['01.txt','02.txt'])
    pending[1].resolve(reply('/api/tts/two'));await two
    pending[0].resolve(reply('/api/tts/one'));await one
    assert.equal(store.currentAudioUrl,'/api/tts/two')
    const again=store.playChapter('synthetic','Synthetic','01.txt',['01.txt','02.txt'])
    store.stopAndReset()
    pending.at(-1).resolve(reply('/api/tts/old'));await again
    assert.equal(store.currentAudioUrl,null)
    assert.equal(store.playbackType,'none')
  } finally {globalThis.fetch=oldFetch}
})

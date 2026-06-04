/**
 * playbackDoneTracker — barge-in SSoT timing logic.
 *
 * Verifies the client only reports ``playback_done`` (which lets the server
 * lower ``bot_speaking``) once the user has TRULY stopped hearing the bot:
 * after tts_end AND the local playback queue drained. Mis-timing here is the
 * root cause of both the original bug ("TTS keeps playing while I talk" — fires
 * too late / never) and a potential regression (firing during the tail would
 * let a real barge-in be ignored). Pure logic → no AudioContext/WS shims.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'

import { createPlaybackDoneTracker } from './playbackDoneTracker.js'

function make() {
  let sends = 0
  const t = createPlaybackDoneTracker(() => { sends++ })
  return { t, sends: () => sends }
}

test('does NOT fire while chunks are still playing (production not ended)', () => {
  const { t, sends } = make()
  t.ttsStart()
  t.chunkEnded(2)            // a chunk ended but 2 still queued, no tts_end yet
  t.chunkEnded(0)            // queue empty BUT production hasn't ended
  assert.equal(sends(), 0)  // more chunks may still arrive — must not fire
})

test('fires once after tts_end AND queue drains', () => {
  const { t, sends } = make()
  t.ttsStart()
  t.ttsEnd(3)               // server done, but 3 chunks still buffered
  assert.equal(sends(), 0)
  t.chunkEnded(2)
  t.chunkEnded(1)
  assert.equal(sends(), 0)  // still hearing the bot
  t.chunkEnded(0)           // last chunk done → user stopped hearing bot
  assert.equal(sends(), 1)
})

test('idempotent — never sends twice for one turn', () => {
  const { t, sends } = make()
  t.ttsStart()
  t.ttsEnd(1)
  t.chunkEnded(0)           // fires
  t.chunkEnded(0)           // late/duplicate onended — ignored
  t.ttsEnd(0)               // duplicate tts_end — ignored
  assert.equal(sends(), 1)
})

test('short reply: queue already drained before tts_end → fires on tts_end', () => {
  const { t, sends } = make()
  t.ttsStart()
  t.chunkEnded(0)           // the only chunk finished before tts_end landed
  assert.equal(sends(), 0)  // production not ended yet
  t.ttsEnd(0)               // now production ends with empty queue → fire
  assert.equal(sends(), 1)
})

test('flush (barge-in) suppresses playback_done for the interrupted turn', () => {
  const { t, sends } = make()
  t.ttsStart()
  t.ttsEnd(2)
  t.flush()                 // user barged in; server already lowered bot_speaking
  t.chunkEnded(0)           // stop()-triggered onended must NOT send
  assert.equal(sends(), 0)
})

test('new turn re-arms after a completed turn', () => {
  const { t, sends } = make()
  // Turn 1
  t.ttsStart(); t.ttsEnd(0)
  assert.equal(sends(), 1)
  // Turn 2 — must be able to fire again
  t.ttsStart(); t.ttsEnd(1)
  t.chunkEnded(0)
  assert.equal(sends(), 2)
})

test('new turn re-arms after a flushed (barged-in) turn', () => {
  const { t, sends } = make()
  t.ttsStart(); t.ttsEnd(1); t.flush()
  assert.equal(sends(), 0)
  // Next reply must still report playback_done.
  t.ttsStart(); t.ttsEnd(0)
  assert.equal(sends(), 1)
})

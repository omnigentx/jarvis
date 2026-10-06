import { test } from 'node:test'
import assert from 'node:assert/strict'
import { waitForAudioReady } from './audioGeneration.js'

class Stream {
  static last
  constructor(url) { this.url = url; this.closed = false; Stream.last = this }
  addEventListener(name, fn) { this.listener = fn }
  close() { this.closed = true }
  emit(status) { this.listener({ data: JSON.stringify({ status }) }) }
}
globalThis.EventSource = Stream
globalThis.window = { location: { origin: "http://test" } }

test('generation ready resolves one push subscription without polling', async () => {
  const result = waitForAudioReady('/api/tts/synthetic?t=1')
  const stream = Stream.last
  assert.equal(stream.url, 'http://test/api/tts/synthetic/status-stream')
  stream.emit('generating')
  stream.emit('ready')
  await result
  assert.equal(stream.closed, true)
})
test('generation failure rejects and closes its stream', async () => {
  const result = waitForAudioReady('/api/tts/synthetic')
  Stream.last.emit('error')
  await assert.rejects(result, /generation failed/)
  assert.equal(Stream.last.closed, true)
})
test('switching chapter aborts an outstanding generation wait', async () => {
  const controller = new AbortController()
  const result = waitForAudioReady('/api/tts/synthetic', { signal: controller.signal })
  controller.abort()
  await assert.rejects(result, { name: 'AbortError' })
  assert.equal(Stream.last.closed, true)
})
test('a silent stream cannot keep the player waiting indefinitely', async () => {
  const result = waitForAudioReady('/api/tts/synthetic', { timeoutMs: 5 })
  await assert.rejects(result, /timed out/)
  assert.equal(Stream.last.closed, true)
})
test('late disconnect after ready cannot reconnect a completed subscription', async () => {
  const result = waitForAudioReady('/api/tts/synthetic')
  const source = Stream.last
  source.emit('ready')
  await result
  source.onerror()
  assert.equal(source.closed, true)
  assert.equal(Stream.last, source)
})

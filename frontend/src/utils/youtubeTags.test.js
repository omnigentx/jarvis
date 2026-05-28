import { test } from 'node:test'
import assert from 'node:assert/strict'

import { parseYoutubeTags, youtubeEmbedUrl } from './youtubeTags.js'

test('no tag → text unchanged, videoIds empty', () => {
  const out = parseYoutubeTags('Hello world')
  assert.equal(out.text, 'Hello world')
  assert.deepEqual(out.videoIds, [])
})

test('single tag at end → tag stripped, id extracted', () => {
  // TODO(i18n): VN literal intentionally kept — exercises Unicode/diacritic handling in parser
  const out = parseYoutubeTags('Đang phát Bài Ca. [[[PLAY: dQw4w9WgXcQ]]]')
  assert.equal(out.text, 'Đang phát Bài Ca.')
  assert.deepEqual(out.videoIds, ['dQw4w9WgXcQ'])
})

test('tag in middle of sentence → cleanly stripped', () => {
  const out = parseYoutubeTags('Now playing [[[PLAY: abc12345DEF]]] for you.')
  assert.equal(out.text, 'Now playing  for you.')
  assert.deepEqual(out.videoIds, ['abc12345DEF'])
})

test('multiple distinct tags → all ids in order', () => {
  const out = parseYoutubeTags(
    'Playlist: [[[PLAY: aaaaaa1111A]]] then [[[PLAY: bbbbbb2222B]]]'
  )
  assert.deepEqual(out.videoIds, ['aaaaaa1111A', 'bbbbbb2222B'])
  assert.equal(out.text.includes('PLAY:'), false)
})

test('duplicate ids are deduped while preserving order', () => {
  const out = parseYoutubeTags(
    '[[[PLAY: dQw4w9WgXcQ]]] then again [[[PLAY: dQw4w9WgXcQ]]]'
  )
  assert.deepEqual(out.videoIds, ['dQw4w9WgXcQ'])
})

test('whitespace inside the tag is tolerated', () => {
  const out = parseYoutubeTags('[[[PLAY:   abc12345DEF   ]]]')
  assert.deepEqual(out.videoIds, ['abc12345DEF'])
  assert.equal(out.text, '')
})

test('id with hyphen and underscore (valid YouTube chars)', () => {
  const out = parseYoutubeTags('[[[PLAY: a_b-c-1_2-3]]]')
  assert.deepEqual(out.videoIds, ['a_b-c-1_2-3'])
})

test('invalid id (too short) → tag NOT extracted, but text is left as-is', () => {
  // The regex requires 6+ chars; we don't strip what the regex doesn't match,
  // so a malformed tag stays visible — that's the fail-loud signal that
  // either the backend contract drifted or the LLM hallucinated.
  const out = parseYoutubeTags('Bad tag [[[PLAY: abc]]] here')
  assert.deepEqual(out.videoIds, [])
  assert.equal(out.text.includes('[[[PLAY: abc]]]'), true)
})

test('other [[[TAG:...]]] tags are NOT stripped (only PLAY)', () => {
  // READ_LOCAL / READ_LIBRARY / READ_STORY tags belong to the TTS pipeline
  // and are stripped by the backend before the bubble text reaches us. If
  // one slips through, we want it visible (fail-loud) rather than silently
  // hidden by an over-eager regex.
  const out = parseYoutubeTags('Reading [[[READ_LOCAL: chapter1]]] now')
  assert.deepEqual(out.videoIds, [])
  assert.equal(out.text, 'Reading [[[READ_LOCAL: chapter1]]] now')
})

test('null / undefined / non-string input → safe fallback', () => {
  assert.deepEqual(parseYoutubeTags(null), { text: '', videoIds: [] })
  assert.deepEqual(parseYoutubeTags(undefined), { text: '', videoIds: [] })
  assert.deepEqual(parseYoutubeTags(42), { text: '', videoIds: [] })
})

test('trailing whitespace from stripped tag is trimmed', () => {
  const out = parseYoutubeTags('Playing now.   [[[PLAY: dQw4w9WgXcQ]]]   ')
  assert.equal(out.text, 'Playing now.')
})

test('tag on its own line → leftover blank line is collapsed', () => {
  const out = parseYoutubeTags('Line 1   \n[[[PLAY: dQw4w9WgXcQ]]]\nLine 3')
  // The trailing spaces before \n are removed, the empty line that the
  // tag leaves behind is acceptable — we don't aggressively re-flow.
  assert.equal(out.videoIds.length, 1)
  assert.equal(out.text.includes('Line 1'), true)
  assert.equal(out.text.includes('Line 3'), true)
  assert.equal(out.text.includes('PLAY:'), false)
})

test('youtubeEmbedUrl builds nocookie URL with id encoded + autoplay', () => {
  // autoplay=1 + rel=0 are user-facing behaviour, not implementation
  // detail — pin them so a future refactor that drops either flag is a
  // visible diff in the PR rather than a silent UX regression.
  assert.equal(
    youtubeEmbedUrl('dQw4w9WgXcQ'),
    'https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ?autoplay=1&rel=0'
  )
})

test('youtubeEmbedUrl encodes characters defensively', () => {
  // Defence-in-depth: even though the parser only emits sanitized ids,
  // the embed helper must not blindly concatenate. If a caller bypasses
  // the parser, encodeURIComponent prevents URL injection.
  assert.equal(
    youtubeEmbedUrl('a/b'),
    'https://www.youtube-nocookie.com/embed/a%2Fb?autoplay=1&rel=0'
  )
})

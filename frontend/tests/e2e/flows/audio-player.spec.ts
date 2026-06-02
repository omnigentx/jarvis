/**
 * E2E flow: Stories library + audio player.
 *
 * Guards the core reader path: pick a story, trigger playback, and handle a
 * missing chapter. Audio decoding itself is mocked — we assert on the API
 * contract (right chapter id in the play POST, right chapter-detail fetch on
 * missing) instead of on `<audio>` playback state, which is brittle in
 * headless Chromium.
 *
 * Coverage:
 *  1. /stories renders the fixture's stories; clicking one navigates to the
 *     chapter list and fires the chapter-list fetch for that story id.
 *  2. Clicking a chapter's play button POSTs
 *     /api/stories/:id/:filename/play — the path itself carries the
 *     "chapter id" contract.
 *  3. Navigating Story Reader to a filename the backend doesn't have renders
 *     the error text from the response body (not a silent empty page).
 *
 * Structure mirrors setup-wizard.spec.ts.
 */

import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mockBackend, NetworkRecorder, seedApiKey } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')
const NOISE = join(FIXTURES, '_app_boot_noise.yaml')

test('stories library renders fixture stories and navigates to chapter list', async ({
  page,
}) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'stories_list.yaml'),
  ])
  const recorder = new NetworkRecorder()
  await recorder.attach(page)

  await page.goto('/stories')

  // All three stories from the fixture render as title spans in StoryCard.vue
  // (.story-card__title), not heading elements.
  await expect(
    page.locator('.story-card__title', { hasText: 'Alpha Story' })
  ).toBeVisible()
  await expect(
    page.locator('.story-card__title', { hasText: 'Beta Tale' })
  ).toBeVisible()
  await expect(
    page.locator('.story-card__title', { hasText: 'Gamma Chronicles' })
  ).toBeVisible()

  // Boot contract: /stories mounts → GET /api/stories.
  recorder.assertContains('GET', '/api/stories')

  // Click the first story — card @click bubbles from the title.
  await page.locator('.story-card__title', { hasText: 'Alpha Story' }).click()

  await expect(page).toHaveURL(/\/stories\/alpha_story$/)

  // ChapterList mounts → fetches chapters for this story id.
  await expect
    .poll(() =>
      recorder.calls.find(
        (c) =>
          c.method === 'GET' && c.path === '/api/stories/alpha_story/chapters'
      )
    )
    .toBeTruthy()

  // Each chapter gets its own row; the first chapter's title is derived from
  // the filename "0001_prologue.txt" → "Prologue".
  await expect(page.locator('#chapter-0001_prologue\\.txt')).toBeVisible()
  await expect(page.locator('#chapter-0002_chapter_two\\.txt')).toBeVisible()
  await expect(page.locator('#chapter-0003_chapter_three\\.txt')).toBeVisible()

  expect(backend.unexpected.length).toBe(0)
})

test('play button fires POST /api/stories/:id/:filename/play with the right chapter id', async ({
  page,
}) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'stories_list.yaml'),
  ])
  const recorder = new NetworkRecorder()
  await recorder.attach(page)

  await page.goto('/stories/alpha_story')

  // Wait for the chapter row we want before clicking its play button.
  const firstRow = page.locator('#chapter-0001_prologue\\.txt')
  await expect(firstRow).toBeVisible()

  // ChapterRow renders two buttons per row ("read text" + "play audio"); pick
  // the play one by its explicit test id. Also has aria-label="Play audio"
  // for a11y — both added in components/stories/ChapterRow.vue.
  const playBtn = firstRow.locator('[data-testid="chapter-play"]')
  await playBtn.click()

  // Contract assertion: POST fires against the expected path (chapter id is
  // encoded in the URL, per backend stories.py::play_local_chapter).
  const playPath = '/api/stories/alpha_story/0001_prologue.txt/play'
  await expect
    .poll(() =>
      recorder.calls.find((c) => c.method === 'POST' && c.path === playPath)
    )
    .toBeTruthy()
  recorder.assertContains('POST', playPath)

  // The audio element then fetches the audio_url returned by play — this is
  // the proof the store consumed the response (not silently dropped it).
  await expect
    .poll(() =>
      recorder.calls.find(
        (c) =>
          c.method === 'GET' &&
          c.path === '/api/tts/story_alpha_story_0001_prologue.txt'
      )
    )
    .toBeTruthy()

  expect(backend.unexpected.length).toBe(0)
})

test('negative control: missing chapter renders error text, not blank content', async ({
  page,
}) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, [
    NOISE,
    join(FIXTURES, 'stories_chapter_missing.yaml'),
  ])
  const recorder = new NetworkRecorder()
  await recorder.attach(page)

  await page.goto('/stories/alpha_story/read/9999_missing.txt')

  // Reader calls the chapter-text endpoint on mount — that's where the error
  // body is served.
  await expect
    .poll(() =>
      recorder.calls.find(
        (c) =>
          c.method === 'GET' &&
          c.path === '/api/stories/alpha_story/chapters/9999_missing.txt'
      )
    )
    .toBeTruthy()

  // UI MUST surface the error (fail-loud). The reader sets
  // content.value = data.content || data.error — so the paragraph area
  // contains the backend's "Chapter not found." string.
  await expect(page.getByText('Chapter not found.')).toBeVisible()

  expect(backend.unexpected.length).toBe(0)
})

import { test, expect } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { seedApiKey, mockBackend } from '../harness'
const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')

test('pregen reconnect restores failure/progress then ready without REST polling', async ({ page }) => {
  await seedApiKey(page)
  await mockBackend(page, [join(FIXTURES, '_app_boot_noise.yaml'), join(FIXTURES, 'stories_list.yaml')])
  let streams = 0
  let chapterReads = 0
  page.on('request', req => { if (new URL(req.url()).pathname.endsWith('/alpha_story/chapters')) chapterReads++ })
  await page.route('**/api/stories/pregen-stream?*', async route => {
    streams++
    const data = streams === 1 ? {
      ready: ['0001_prologue.txt'],
      failures: [{ chapter_file: '0002_chapter_two.txt', retry_at: Date.now() / 1000 + 60 }],
      generating: { story_id: 'alpha_story', chapter_file: '0003_chapter_three.txt', completed_chunks: 4, total_chunks: 8 },
    } : { ready: ['0001_prologue.txt', '0002_chapter_two.txt', '0003_chapter_three.txt'], failures: [], generating: null }
    await route.fulfill({ contentType: 'text/event-stream', body: `event: snapshot\ndata: ${JSON.stringify(data)}\n\nevent: queue_update\ndata: {"queue":[]}\n\n` })
  })
  await page.goto('/stories/alpha_story')
  const failed = page.locator('#chapter-0002_chapter_two\\.txt')
  const progressing = page.locator('#chapter-0003_chapter_three\\.txt')
  await expect(failed.getByRole('status')).toContainText('Incomplete')
  await expect(progressing.getByRole('status')).toContainText('4/8')
  await expect(failed.locator('[data-status="error"]')).toBeVisible()
  await expect(progressing.locator('[data-status="generating"]')).toBeVisible()
  await expect(page.locator('.chapter-list__count--ok')).toContainText('1')
  await page.reload()
  await expect(page.locator('.chapter-list__count--ok')).toContainText('3')
  await expect(failed.locator('[data-status="ready"]')).toBeVisible()
  await expect(progressing.locator('[data-status="ready"]')).toBeVisible()
  await expect(failed.getByRole('status')).toHaveCount(0)
  expect(chapterReads).toBe(2)
})

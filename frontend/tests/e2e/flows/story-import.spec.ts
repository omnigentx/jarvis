/** Synthetic-only UI regressions; actual backend acceptance recorded separately. */
import { test, expect } from '@playwright/test'
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { mockBackend, seedApiKey } from '../harness'
const noise = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures', '_app_boot_noise.yaml')
for (const mobile of [false, true]) {
 test(`folder preview and retry (${mobile ? 'mobile' : 'desktop'})`, async ({ page }) => {
  if (mobile) await page.setViewportSize({ width: 390, height: 844 })
  await seedApiKey(page)
  await mockBackend(page, [noise])
  let imported = false
  const requests: string[] = []
  await page.route('**/api/stories**', async route => {
   const path = new URL(route.request().url()).pathname
   if (path.endsWith('/import')) {
    requests.push(route.request().postData() || '')
    if (requests.length === 1) return route.fulfill({ status: 503, json: { detail: { code: 'import_failed' } } })
    imported = true
    return route.fulfill({ json: { id: 'synthetic-import', title: 'Synthetic import', chapters: 3 } })
   }
   if (path.endsWith('/chapters')) return route.fulfill({ json: [{ file: '000001_Alpha.txt', preload: {} }] })
   return route.fulfill({ json: imported ? [{ id: 'synthetic-import', title: 'Synthetic import', chapters: 3 }] : [] })
  })
  const folder = await mkdtemp(join(tmpdir(), 'synthetic-story-'))
  try {
   await mkdir(join(folder, 'sub'))
   for (const name of ['10_End.txt', '2_Beta.txt', '2_Alpha.txt', 'ignore.json', 'sub/1.txt']) await writeFile(join(folder, name), 'Synthetic text')
   await page.goto('/stories')
   await page.getByRole('button', { name: 'Import story', exact: true }).click()
   const dialog = page.getByRole('dialog', { name: 'Import story' })
   await expect(dialog.getByRole('button', { name: 'Import story', exact: true })).toBeDisabled()
   await dialog.locator('input[webkitdirectory]').setInputFiles(folder)
   await dialog.getByLabel('Story title').fill('Synthetic import')
   await expect(dialog.locator('li')).toHaveText(['2_Alpha.txt', '2_Beta.txt', '10_End.txt'])
   await expect(dialog).toContainText('Skipped 2')
   await dialog.getByRole('button', { name: 'Import story', exact: true }).click()
   await expect(dialog.getByRole('alert')).toContainText('Import failed')
   await expect(dialog.getByLabel('Story title')).toBeDisabled()
   await dialog.getByRole('button', { name: 'Retry import' }).click()
   await expect(page).toHaveURL(/stories\/synthetic-import$/)
   const key = (body: string) => body.match(/name="request_id"\r\n\r\n([^\r]+)/)?.[1]
   expect(key(requests[0])).toBeTruthy()
   expect(key(requests[1])).toBe(key(requests[0]))
   expect(requests[0]).not.toContain('sub/1.txt')
   expect(requests[0]).not.toContain('ignore.json')
   expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy()
  } finally { await rm(folder, { recursive: true, force: true }) }
 })
}
test('non-text folder explains disabled import', async ({ page }) => {
 await seedApiKey(page)
 await mockBackend(page, [noise])
 await page.route('**/api/stories', route => route.fulfill({ json: [] }))
 const folder = await mkdtemp(join(tmpdir(), 'synthetic-empty-story-'))
 try {
  await writeFile(join(folder, 'ignore.json'), '{}')
  await page.goto('/stories')
  await page.getByRole('button', { name: 'Import story', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: 'Import story' })
  await dialog.locator('input[webkitdirectory]').setInputFiles(folder)
  await expect(dialog.getByRole('alert')).toContainText('Choose between 1 and 1,000')
  await expect(dialog).toContainText('Skipped 1')
  await expect(dialog.getByRole('button', { name: 'Import story', exact: true })).toBeDisabled()
 } finally { await rm(folder, { recursive: true, force: true }) }
})

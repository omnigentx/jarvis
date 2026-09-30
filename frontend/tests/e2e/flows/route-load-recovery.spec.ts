import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { mockBackend, seedApiKey } from '../harness'

const fixtures = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')
const screens = [
  ['Meeting room', '/meetings', 'MeetingsView'],
  ['Settings', '/settings', 'SettingsView'],
  ['Token usage', '/token-usage', 'TokenUsage'],
  ['Scheduler', '/scheduler', 'SchedulerDashboard'],
  ['MCP servers', '/mcp-servers', 'McpServersView'],
  ['Approvals', '/approvals', 'ApprovalsView'],
]

test.beforeEach(async ({ page }) => {
  await seedApiKey(page)
  await mockBackend(page, ['_app_boot_noise.yaml', 'chat_streaming_happy.yaml', 'route_recovery.yaml'].map(f => join(fixtures, f)))
})

for (const [label, path, chunk] of screens) {
  test(`${label}: failed chunk preserves draft and offers bounded recovery`, async ({ page }) => {
    let documents = 0
    page.on('request', r => { if (r.isNavigationRequest() && r.frame() === page.mainFrame()) documents++ })
    await page.route(`**/assets/${chunk}-*.js`, route => route.fulfill({ status: 404, body: 'Not found' }))
    await page.goto('/chat')
    const draft = page.locator('textarea')
    await draft.fill('Unsent requirement — do not lose this')
    await page.getByRole('link', { name: label, exact: true }).click()
    const notice = page.getByRole('alert').filter({ hasText: 'This page could not be loaded' })
    await expect(notice).toBeVisible()
    await expect(page).toHaveURL(/\/chat$/)
    await expect(draft).toHaveValue('Unsent requirement — do not lose this')
    expect(documents).toBe(1)
    await notice.getByRole('button', { name: 'Keep working' }).click()
    await expect(notice).toHaveCount(0)
    await expect(draft).toHaveValue('Unsent requirement — do not lose this')
    await page.getByRole('link', { name: label, exact: true }).click()
    await expect(notice).toBeVisible()
    await notice.getByRole('button', { name: 'Reload page' }).click()
    await expect(page).toHaveURL(new RegExp(`${path}$`))
    // Still missing after navigation: visible recovery, no reload loop.
    await expect(notice).toBeVisible()
    await expect(page.getByRole('button', { name: 'Reload page' })).toBeEnabled()
    expect(documents).toBe(2)
  })
}

for (const width of [1280, 390]) {
  test(`Meeting room recovers after missing chunk is restored (${width}px)`, async ({ page }, info) => {
    const missingChunk = '**/assets/MeetingsView-*.js'
    await page.route(missingChunk, route => route.fulfill({ status: 404, body: 'Not found' }))
    await page.goto('/chat')
    await page.getByRole('link', { name: 'Meeting room', exact: true }).click()
    const notice = page.getByRole('alert').filter({ hasText: 'This page could not be loaded' })
    await expect(notice).toBeVisible()
    await page.setViewportSize({ width, height: 844 })
    const box = await notice.boundingBox()
    expect(box!.x).toBeGreaterThanOrEqual(0)
    expect(box!.x + box!.width).toBeLessThanOrEqual(width)
    await info.attach(`recovery-notice-${width}`, { body: await page.screenshot(), contentType: 'image/png' })
    await page.unroute(missingChunk)
    await notice.getByRole('button', { name: 'Reload page' }).click()
    await expect(page).toHaveURL(/\/meetings$/)
    await expect(page.getByRole('heading', { name: 'Meeting Room', exact: true })).toBeVisible()
    await expect(notice).toHaveCount(0)
    await info.attach(`meetings-recovered-${width}`, { body: await page.screenshot(), contentType: 'image/png' })
  })
}

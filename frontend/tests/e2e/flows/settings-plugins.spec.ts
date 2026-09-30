import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { mockBackend, seedApiKey } from '../harness'

const noise = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures', '_app_boot_noise.yaml')
const candidate = { id: 'sample', name: 'review', repo: 'openai/plugins', commit: 'a'.repeat(40), digest: 'b'.repeat(64), status: 'needs_approval', skills: [{ name: 'review', description: 'Review changes' }], blockers: [], bindings: [], server_names: [] }

for (const mobile of [false, true]) {
  test(`plugin content review and truthful activation (${mobile ? 'mobile' : 'desktop'})`, async ({ page }, info) => {
    if (mobile) await page.setViewportSize({ width: 390, height: 844 })
    await seedApiKey(page)
    await mockBackend(page, [noise])
    await page.route('**/api/plugins**', async route => {
      const path = new URL(route.request().url()).pathname
      let result: unknown = { plugins: [candidate] }
      if (path.endsWith('/content')) result = { content: '<script>window.pluginExecuted = true</script>', digest: candidate.digest }
      if (path.endsWith('/activate')) result = { ...candidate, status: 'activation_failed' }
      await route.fulfill({ json: result })
    })
    await page.goto('/settings')
    await page.getByRole('button', { name: 'Plugins', exact: true }).click()
    const card = page.getByTestId('plugin-sample')
    await expect(card).toBeVisible()
    await card.getByRole('button', { name: 'Review skill', exact: true }).click()
    await expect(page.locator('pre')).toContainText('<script>')
    expect(await page.evaluate(() => (window as any).pluginExecuted)).toBeUndefined()
    await card.getByLabel('Target agent').fill('Jarvis')
    await card.getByRole('button', { name: 'Activate', exact: true }).click()
    await expect(card).toContainText('Activation failed')
    await expect(card.getByText('Ready', { exact: true })).not.toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy()
    await page.screenshot({ path: info.outputPath(`plugins-${mobile ? 'mobile' : 'desktop'}.png`), fullPage: true })
  })
}

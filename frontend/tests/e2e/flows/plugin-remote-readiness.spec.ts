import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { mockBackend, seedApiKey } from '../harness'

const noise = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures', '_app_boot_noise.yaml')

for (const mobile of [false, true]) {
  for (const connected of [false, true]) {
    test(`remote account readiness overrides stale binding (${mobile ? 'mobile' : 'desktop'}, ${connected ? 'connected' : 'disconnected'})`, async ({ page }) => {
      if (mobile) await page.setViewportSize({ width: 390, height: 844 })
      await seedApiKey(page)
      await mockBackend(page, [noise])
      await page.route('**/api/plugins**', async route => {
        const path = new URL(route.request().url()).pathname
        let json: any = { plugins: [{ id: 'rovo', name: 'atlassian-rovo', status: 'ready', policy_configured: true,
          repo: 'openai/plugins', commit: 'a'.repeat(40), digest: 'b'.repeat(64), skills: [],
          blockers: ['mcp_requires_policy_review'], bindings: [{ agent: 'Jarvis', status: 'ready' }],
          server_names: ['rovo'], remote_servers: [{ name: 'rovo', url: 'https://mcp.atlassian.com/v1/mcp/authv2' }] }] }
        if (path.endsWith('/targets')) json = { targets: [{ agent: 'Jarvis', label: 'Jarvis' }] }
        if (path.endsWith('/remote')) json = { servers: [{ name: 'rovo', status: connected ? 'connected' : 'disconnected', flow_id: null }] }
        await route.fulfill({ json })
      })
      await page.goto('/settings')
      await page.getByRole('button', { name: 'Plugins', exact: true }).click()
      const card = page.getByTestId('plugin-rovo')
      await expect(card.locator('.remote-connection')).toContainText(connected ? 'Account connected' : 'Account not connected')
      await card.getByLabel('Target agent').selectOption('Jarvis')
      if (connected) {
        await expect(card.locator('header .status')).toHaveText('Ready')
        await expect(card.getByRole('button', { name: 'Activate', exact: true })).toBeEnabled()
      } else {
        await expect(card.locator('header .status')).not.toHaveText('Ready')
        await expect(card.getByRole('button', { name: 'Activate', exact: true })).toBeDisabled()
        await expect(card.getByRole('button', { name: 'Connect Atlassian account', exact: true })).toBeVisible()
      }
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy()
    })
  }
}

import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { mockBackend, seedApiKey } from '../harness'
const fixtures = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')
test('cost estimate discloses configuration source, fallback and historical limits', async ({ page }) => {
  await seedApiKey(page)
  await mockBackend(page, [join(fixtures, '_app_boot_noise.yaml'), join(fixtures, 'token_usage_with_data.yaml')])
  await page.goto('/token-usage')
  const notice = page.getByRole('note', { name: 'Cost provenance' })
  await expect(notice).toContainText('model_pricing.yaml')
  await expect(notice).toContainText('fallback')
  await expect(notice).toContainText('invoice')
  await expect(notice).toContainText('Historical')
})

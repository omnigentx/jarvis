import { expect, test } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { mockBackend, seedApiKey } from '../harness'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')

test('agent model UI applies, resets, and preserves state on canary failure', async ({ page }) => {
  await seedApiKey(page)
  const backend = await mockBackend(page, [
    join(FIXTURES, '_app_boot_noise.yaml'),
    join(FIXTURES, 'agent_model_selection.yaml'),
  ])
  const changes: Array<{ model_id: string, expected_revision: number }> = []
  await page.route('**/api/agents/Jarvis/model', async route => {
    changes.push(route.request().postDataJSON())
    await route.fallback()
  })

  await page.goto('/agents/Jarvis')
  const selection = page.getByRole('region', { name: 'Model selection' })
  const input = selection.getByRole('combobox', { name: 'Model' })
  await expect(selection).toContainText('Configured: openai.coding-agent · Active: none')

  await input.fill('openai.cx/gpt-6-luna')
  await selection.getByRole('button', { name: 'Apply model' }).click()
  await expect(selection).toContainText('Configured: openai.cx/gpt-6-luna · Active: none')
  await selection.getByRole('button', { name: 'Use default' }).click()
  await expect(selection).toContainText('Configured: openai.coding-agent · Active: none')
  await expect(selection.getByRole('button', { name: 'Use default' })).toHaveCount(0)

  await input.fill('openai.kr/gpt-5.6-luna')
  await selection.getByRole('button', { name: 'Apply model' }).click()
  await expect(selection.getByRole('alert')).toContainText('failed a bounded inference probe')
  await expect(selection).toContainText('Configured: openai.coding-agent · Active: none')
  expect(changes).toEqual([
    { run_id: '', model_id: 'openai.cx/gpt-6-luna', expected_revision: 0 },
    { run_id: '', model_id: '', expected_revision: 1 },
    { run_id: '', model_id: 'openai.kr/gpt-5.6-luna', expected_revision: 2 },
  ])
  expect(backend.unexpected).toEqual([])
})

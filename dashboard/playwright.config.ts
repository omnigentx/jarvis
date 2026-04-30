import { defineConfig, devices } from '@playwright/test'

/**
 * Playwright config for dashboard E2E.
 *
 * Modes:
 *  - default:           mocked backend via tests/e2e/fixtures/*.yaml
 *  - PW_RECORD=1:       proxy to REAL backend (webServer URL below), capture
 *                       responses into fixture YAML
 *  - PW_SMOKE=1:        run tests/e2e/smoke/ against real backend (no mock)
 *
 * Traces upload on failure — open with `npx playwright show-trace`.
 */

const RECORD_MODE = !!process.env.PW_RECORD
const SMOKE_MODE = !!process.env.PW_SMOKE
const PREVIEW_PORT = Number(process.env.PW_PORT || 3000)

export default defineConfig({
  testDir: './tests/e2e',
  testMatch: SMOKE_MODE ? '**/smoke/**/*.spec.ts' : '**/flows/**/*.spec.ts',
  timeout: 30_000,
  expect: { timeout: 5_000 },
  fullyParallel: !RECORD_MODE, // Serialize recording to avoid fixture write races
  forbidOnly: !!process.env.CI,
  retries: 0, // fail-loud, never mask flakiness
  workers: RECORD_MODE ? 1 : undefined,
  reporter: [
    ['list'],
    ['html', { open: 'never', outputFolder: 'playwright-report' }],
  ],
  use: {
    baseURL: process.env.PW_BASE_URL || `http://localhost:${PREVIEW_PORT}`,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    // Seed the X-API-Key localStorage before every test via addInitScript —
    // the dashboard reads it synchronously on load.
    storageState: undefined,
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: {
    command:
      RECORD_MODE || SMOKE_MODE
        // Record + smoke modes need real backend running. Dev server proxies /api
        // to backend at :8000 (see vite.config.js). Caller is responsible for
        // docker-compose up -f docker-compose.e2e.yaml beforehand.
        ? 'npm run dev'
        // Default mocked mode: preview the built static bundle so `/api/*`
        // intercepts are deterministic (no Vite HMR noise).
        : `npm run preview -- --port ${PREVIEW_PORT}`,
    url: `http://localhost:${PREVIEW_PORT}`,
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
    stdout: 'ignore',
    stderr: 'pipe',
  },
})

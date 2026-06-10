/**
 * Auth helpers for E2E.
 *
 * Post-cookie-only-migration there is no localStorage credential to
 * seed. ``seedApiKey`` is kept as a NO-OP shim so existing specs that
 * call it for historical reasons still compile; do NOT rely on it for
 * authentication in new specs. The authoritative way to authenticate
 * in fixture-mocked tests is to make the fixture return
 * ``GET /api/auth/whoami -> {authenticated: true}``, which the auth
 * store treats as a successful boot probe.
 */

import type { Page } from '@playwright/test'

const DEFAULT_TEST_KEY = 'test-api-key-e2e'

// eslint-disable-next-line @typescript-eslint/no-unused-vars
export async function seedApiKey(
  _page: Page,
  _key: string = DEFAULT_TEST_KEY,
): Promise<void> {
  // No-op. The dashboard no longer reads ``localStorage.jarvis_api_key``.
  // Kept for backwards compat with specs that haven't migrated yet.
}

// eslint-disable-next-line @typescript-eslint/no-unused-vars
export async function clearApiKey(_page: Page): Promise<void> {
  // No-op. See seedApiKey.
}

/**
 * Seed a CSRF cookie before the app loads. Mirrors what
 * ``POST /api/auth/login`` would set in production. Use this for tests
 * that need to exercise authenticated-state behavior (probe whoami =
 * authenticated, mutations that carry X-CSRF-Token, …) without going
 * through the actual login flow.
 *
 * The session cookie itself is httpOnly and signed with the backend's
 * JWT_SECRET, which the harness can't realistically forge. Tests that
 * need a valid session use a fixture-mocked /api/auth/whoami that
 * always returns ``authenticated:true`` instead of forging the cookie.
 */
export async function seedCsrfCookie(
  page: Page,
  token: string = 'test-csrf-token-e2e'
): Promise<string> {
  await page.context().addCookies([
    {
      name: 'jarvis_csrf',
      value: token,
      // Cookies need a URL or domain+path. Use the same origin Playwright
      // navigates to so the browser will send it on /api/* requests.
      url: process.env.PW_BASE_URL || 'http://localhost:3000',
      sameSite: 'Lax',
    },
  ])
  return token
}

/**
 * A successful modal login (API key or passkey) makes the app reload
 * itself (App.vue's RESTORED listener, ``from === 'unauthenticated'``) so
 * every view's mount-time fetch replays with the fresh cookie. The
 * reload's boot probe re-hits ``/api/auth/whoami`` — waiting for that
 * response is the deterministic "reload finished" signal.
 *
 * Start the wait BEFORE triggering the login click, then await it after:
 *
 *     const settled = waitForPostLoginReload(page)
 *     await page.getByRole('button', { name: /continue/i }).click()
 *     await settled
 *
 * The fixture's whoami entry must be a sequence (``[false, true]``) so
 * the post-reload probe authenticates instead of re-opening the gate.
 */
export async function waitForPostLoginReload(page: Page): Promise<void> {
  await page.waitForResponse((r) => r.url().includes('/api/auth/whoami'))
  await page.waitForLoadState()
}

/**
 * Wipe everything the dashboard stores about auth: localStorage key,
 * session cookie, csrf cookie. Use in beforeEach for tests that simulate
 * a fresh-browser scenario.
 */
export async function clearAllAuth(page: Page): Promise<void> {
  await clearApiKey(page)
  const ctx = page.context()
  // Drop cookies for the test origin; clearCookies() with no args clears all.
  await ctx.clearCookies()
}

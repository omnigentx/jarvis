/**
 * Playwright virtual authenticator helpers.
 *
 * Chromium speaks the WebAuthn API over CDP. Enabling it + installing
 * a virtual authenticator lets the test call
 * ``navigator.credentials.create()`` and ``.get()`` without any user
 * gesture — the in-process virtual device signs everything
 * automatically (``automaticPresenceSimulation: true``).
 *
 * We use ``ctap2`` + ``internal`` to simulate a platform authenticator
 * (Touch ID / Windows Hello), which is the most common real-world
 * passkey shape and the one the SettingsAuth UI labels as
 * "Touch ID / built-in".
 */

import type { Page } from '@playwright/test'

export type VirtualAuthenticatorHandle = {
  authenticatorId: string
  /** Remove the authenticator. Tests don't strictly need this (Page
   *  teardown drops the CDP session) but explicit cleanup avoids
   *  cross-test bleed if Page is reused. */
  remove(): Promise<void>
  /** Read the credentials currently stored on the virtual device.
   *  Useful when a test wants to assert "the authenticator now holds
   *  N resident credentials". */
  listCredentials(): Promise<unknown[]>
}

export async function installVirtualAuthenticator(
  page: Page,
): Promise<VirtualAuthenticatorHandle> {
  const client = await page.context().newCDPSession(page)
  await client.send('WebAuthn.enable')
  const { authenticatorId } = await client.send(
    'WebAuthn.addVirtualAuthenticator',
    {
      options: {
        protocol: 'ctap2',
        transport: 'internal',
        hasResidentKey: true,
        hasUserVerification: true,
        isUserVerified: true,
        automaticPresenceSimulation: true,
      },
    },
  )
  return {
    authenticatorId,
    async remove() {
      await client.send('WebAuthn.removeVirtualAuthenticator', {
        authenticatorId,
      })
    },
    async listCredentials() {
      const result = (await client.send('WebAuthn.getCredentials', {
        authenticatorId,
      })) as { credentials: unknown[] }
      return result.credentials
    },
  }
}

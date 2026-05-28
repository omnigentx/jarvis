/**
 * Public harness surface. Flow specs should import from this module only —
 * internal schema/recorder implementation stays private.
 */

export { mockBackend } from './mock-backend'
export type { MockBackend, RequestRecord } from './mock-backend'

export { seedApiKey, clearApiKey, seedCsrfCookie, clearAllAuth } from './auth'

export { installVirtualAuthenticator } from './passkey'
export type { VirtualAuthenticatorHandle } from './passkey'

export { NetworkRecorder } from './network-recorder'
export type { RecordedRequest } from './network-recorder'

export type { Fixture, FixtureResponse, SseEvent } from './fixture-schema'

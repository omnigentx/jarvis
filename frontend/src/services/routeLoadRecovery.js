import { shallowRef } from 'vue'

// Browser engines and Vite report module/CSS download failures differently.
// Do not turn unrelated runtime or API failures into a reload suggestion.
export function isRouteAssetError(error) {
  const message = typeof error?.message === 'string' ? error.message : ''
  return /Failed to fetch dynamically imported module|error loading dynamically imported module|Importing a module script failed|Unable to preload CSS|Loading (?:CSS )?chunk .+ failed/i.test(message)
}

/** Recover an obsolete lazy route without silently discarding unsent work.
 * Full navigation retries cached rejected import promises and fetches the new
 * entry document. Recovery is user initiated, so offline/broken builds cannot
 * trigger automatic reload loops. No storage, timers or polling are needed.
 */
export function createRouteLoadRecovery(router, location = window.location) {
  const failure = shallowRef(null)
  router.onError((error, to) => {
    if (!isRouteAssetError(error)) return
    const href = router.resolve(to).href
    const target = new URL(href, location.origin)
    if (target.origin !== location.origin) return
    failure.value = { href: target.pathname + target.search + target.hash }
  })
  router.afterEach((_to, _from, navigationFailure) => {
    if (!navigationFailure) failure.value = null
  })
  return {
    failure,
    dismiss() { failure.value = null },
    reload() {
      if (failure.value) location.assign(failure.value.href)
    },
  }
}

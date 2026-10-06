import { buildSSEUrl } from '../api.js'

/** Wait for authoritative completion using push, with bounded reconnects. */
export function waitForAudioReady(audioUrl, { signal, timeoutMs = 282_000 } = {}) {
  return new Promise((resolve, reject) => {
    let source, retryTimer, attempts = 0, settled = false
    let timeout = setTimeout(() => finish(new Error('Audio generation timed out')), timeoutMs)
    const abort = () => finish(new DOMException('Playback changed', 'AbortError'))
    function finish(error) {
      if (settled) return
      settled = true
      source?.close()
      clearTimeout(timeout)
      clearTimeout(retryTimer)
      signal?.removeEventListener('abort', abort)
      if (error) reject(error)
      else resolve()
    }
    function connect() {
      if (settled) return
      try {
        source = new EventSource(buildSSEUrl(`${audioUrl.split('?')[0]}/status-stream`), { withCredentials: true })
      } catch (error) { finish(error); return }
      source.addEventListener('status', event => {
        try {
          const { status } = JSON.parse(event.data)
          if (status === 'generating') {
            clearTimeout(timeout)
            timeout = setTimeout(() => finish(new Error('Audio generation timed out')), timeoutMs)
          } else if (status === 'ready') finish()
          else if (status === 'error') finish(new Error('Audio generation failed'))
        } catch (error) { finish(error) }
      })
      source.onerror = () => {
        if (settled) return
        source.close()
        if (++attempts > 3) finish(new Error('Audio status connection failed'))
        else retryTimer = setTimeout(connect, 1000 * 2 ** (attempts - 1))
      }
    }
    signal?.addEventListener('abort', abort, { once: true })
    if (signal?.aborted) abort()
    else connect()
  })
}

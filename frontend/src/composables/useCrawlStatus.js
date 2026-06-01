/**
 * useCrawlStatus — track a background story-crawl job's progress.
 *
 * There is NO crawl SSE stream (only TTS pre-gen has one), so we POLL
 * GET /api/stories/crawl/status/{job_id} on a timer until the job reaches a
 * terminal state. The job_id arrives as a structured field on the chat 'done'
 * event (chat.py → resolve_story_playback extracts it from the
 * [[[CRAWL_STARTED: id]]] tag before that tag is stripped from the bubble).
 *
 * Shared singleton: a crawl started from chat should keep updating even if the
 * user navigates away, and only one crawl banner makes sense at a time.
 */
import { ref, computed } from 'vue'
import { apiFetch } from '../api.js'

const TERMINAL = new Set(['completed', 'failed', 'cancelled', 'not_found', 'error'])
// 'needs_attention' is NOT terminal — the worker paused and woke the agent to
// self-heal; we keep polling so the banner flips back to running once fixed.
const POLL_MS = 3000

const jobId = ref(null)
const status = ref(null)        // pending | running | completed | failed | cancelled | error
const storyTitle = ref('')
const current = ref(0)
const total = ref(0)
const message = ref('')
let _timer = null

const isActive = computed(() => !!jobId.value && !TERMINAL.has(status.value))
// Worker paused on an anomaly and is being self-healed by the agent.
const needsAttention = computed(() => status.value === 'needs_attention')
const percent = computed(() =>
  total.value > 0 ? Math.min(Math.round((current.value / total.value) * 100), 100) : 0,
)

function _stopTimer() {
  if (_timer) { clearInterval(_timer); _timer = null }
}

async function _poll() {
  if (!jobId.value) return
  try {
    const data = await apiFetch(`/api/stories/crawl/status/${encodeURIComponent(jobId.value)}`)
    status.value = data.status || 'error'
    storyTitle.value = data.story_title || storyTitle.value
    current.value = data.current || 0
    total.value = data.total || 0
    message.value = data.message || ''
    if (TERMINAL.has(status.value)) _stopTimer()
  } catch (_) {
    // Transient network blip — keep polling; a persistent failure just leaves
    // the banner on its last known state, which the user can dismiss.
  }
}

/** Begin tracking a crawl job. Replaces any currently-tracked job. */
function track(id) {
  if (!id || id === jobId.value) return
  _stopTimer()
  jobId.value = id
  status.value = 'pending'
  storyTitle.value = ''
  current.value = 0
  total.value = 0
  message.value = ''
  _poll()
  _timer = setInterval(_poll, POLL_MS)
}

/** Ask the backend to cancel the tracked crawl, then stop polling. */
async function cancel() {
  if (!jobId.value) return
  try {
    await apiFetch(`/api/stories/crawl/cancel/${encodeURIComponent(jobId.value)}`, { method: 'POST' })
  } catch (_) { /* best-effort */ }
  status.value = 'cancelled'
  _stopTimer()
}

/** Hide the banner (clears tracking state). */
function dismiss() {
  _stopTimer()
  jobId.value = null
  status.value = null
}

export function useCrawlStatus() {
  return { jobId, status, storyTitle, current, total, message, isActive, needsAttention, percent, track, cancel, dismiss }
}

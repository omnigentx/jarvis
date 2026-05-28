/**
 * Parse `[[[PLAY: <videoId>]]]` tags out of an agent message.
 *
 * The backend's music-playback skill (backend/tools/media_server.py and
 * backend/.fast-agent/skills/music-playback/SKILL.md) emits this tag when
 * the agent decides to play a YouTube video. The frontend's job is to:
 *
 *   1. Strip the literal tag from the displayed bubble text.
 *   2. Render an embedded YouTube player below the message for each id.
 *
 * Why a regex instead of a markdown extension: the tag format is fixed by
 * the backend contract and never appears mid-word; a single regex pass is
 * trivial to verify and keeps the rendering surface free of new deps.
 *
 * Video IDs follow YouTube's own format: 11 chars from [A-Za-z0-9_-].
 * We allow 6–32 chars to stay forgiving for test fixtures and possible
 * format changes, while still rejecting obviously bogus payloads.
 */

const TAG_RE = /\[\[\[PLAY:\s*([A-Za-z0-9_-]{6,32})\s*\]\]\]/g

export function parseYoutubeTags(text) {
  if (typeof text !== 'string' || !text) {
    return { text: typeof text === 'string' ? text : '', videoIds: [] }
  }
  const ids = []
  for (const match of text.matchAll(TAG_RE)) {
    ids.push(match[1])
  }
  // Strip every match (including invalid/short ones a permissive build
  // might have let through earlier in the regex) so users never see the
  // raw tag, even if id validation rejects it.
  const cleaned = text.replace(TAG_RE, '').replace(/[ \t]+\n/g, '\n').trim()
  // Dedupe while preserving order — duplicate ids would render redundant
  // iframes; YouTube's own UX is one player per unique video.
  const seen = new Set()
  const videoIds = ids.filter((id) => {
    if (seen.has(id)) return false
    seen.add(id)
    return true
  })
  return { text: cleaned, videoIds }
}

export function youtubeEmbedUrl(videoId) {
  // `youtube-nocookie.com` is the privacy-enhanced variant — recommended
  // by Google for embedded players, identical UX, no cookie set until the
  // user actually presses play.
  return `https://www.youtube-nocookie.com/embed/${encodeURIComponent(videoId)}`
}

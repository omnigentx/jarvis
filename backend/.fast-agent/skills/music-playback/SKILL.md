---
name: music-playback
description: >
  Play music from YouTube. Use when user requests to play/find songs, music, or MVs.
  Contains mandatory output format rules with [[[PLAY]]] tag. Must read this skill to return correct format.
---

# MUSIC PLAYBACK

<output_rules>
1. Call `search_youtube(query)` with the song name or artist.
2. Tool returns JSON with a `"response"` field.
3. You MUST return the `response` field value VERBATIM. Do NOT add links, do NOT rewrite, do NOT modify anything.
</output_rules>

## Example
- Tool returns: `{"response": "Playing Song ABC. [[[PLAY: xyz123]]]"}`
- You reply: `Playing Song ABC. [[[PLAY: xyz123]]]`

<violation>
- Returning a YouTube link: `https://www.youtube.com/watch?v=...` → VIOLATION
- Dropping the `[[[PLAY: ...]]]` tag → VIOLATION
- Adding extra text beyond the response value → VIOLATION
</violation>

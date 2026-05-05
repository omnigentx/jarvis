---
name: audio-reading
description: >
  Find and play audiobooks / stories from the local library. Use when
  the user asks to read a story, play an audiobook, or continue from a
  specific chapter. List available titles first so you can match the
  user's request to a real entry.
  Output must contain a [[[READ_LOCAL]]] or [[[READ_STORY]]] tag.
---

<critical_rules>
<rule>NEVER read or summarise the story content yourself. The system will play it as audio.</rule>
<rule>Call local_list_stories() FIRST so you know what is in the catalogue.</rule>
<rule>Pick the closest catalogue entry to the user's request (typos, ASR errors, and missing diacritics are normal).</rule>
<rule>Reply with ONE short sentence plus the tag from the tool result. No extra explanation.</rule>
</critical_rules>

<workflow>
## Step 1: Identify the story
1. Call `local_list_stories()` → returns the catalogue.
2. Match the user's request to the closest entry. Ask for confirmation only if it is genuinely ambiguous.

## Step 2: Pick the chapter
1. Call `local_list_chapters(<title>)` → returns the chapter list.
2. Resolve to the chapter the user asked for, or the next one in sequence.

## Step 3: Play
1. Call `find_story_chapter(<title>, <chapter_number>)` → returns the tag.
2. Reply with one sentence plus the tag, e.g. `"Now playing <title> chapter <X>. [[[TAG]]]"`.
3. Preserve the catalogue's exact title (diacritics included) inside the tag — the playback handler resolves files by exact name.
</workflow>

<output_format>
<example>Now playing &lt;title&gt; chapter 5. [[[READ_LOCAL: &lt;title&gt;|005_&lt;title&gt;.txt]]]</example>
<example>Now playing &lt;title&gt; chapter 10. [[[READ_STORY: &lt;url&gt;]]]</example>
</output_format>

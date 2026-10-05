# Local story folder import — acceptance

Tracking: SCRUM-61. All text, chapter names and screenshots here use generated synthetic stories. No private source story is included.

## Scope and design

Stories → Import story creates a **new story**. Select a folder or select multiple `.txt` files. Only direct children of the selected folder are included; subfolders and non-text files are counted as skipped. Preview uses natural filename ordering and keeps duplicate numeric prefixes. Stored names use sequential `000001_…txt` numbering so chapters cannot overwrite one another and the existing reader order remains correct.

Limits: UTF-8/UTF-8 BOM; 1–1,000 chapters; 2 MiB/file; 12 MiB text total; 14 MiB multipart request (below the existing 16 MiB proxy limit). Empty/binary/invalid UTF-8, unsafe paths, duplicate filenames and unexpected fields are rejected. No LLM/MCP is used for import.

Existing cookie auth/CSRF, `apiFetch`, story metadata table, story list/reader/delete and TTS discovery are reused. The API parses a bounded multipart stream, runs filesystem/DB work off the event loop, and broadcasts count-only progress via ActivityStreamManager. The dialog uses the existing auth-aware SSE composable with reconnect/backoff. POST completion is authoritative if SSE drops; there is no new polling.

## Retry, storage and cleanup

A browser-generated UUID identifies the import. After the first attempt, title/files are frozen; Retry reuses that UUID and identical payload. A content/order/title digest rejects changed-payload reuse with HTTP409. Concurrent retries serialize publication under one fixed file lock.

Multipart handles close on success/error and on interrupted/cancelled body parsing. Staged chapters live in `data/.story-imports/stage-*` (outside the story/TTS scan). They are atomically renamed to `data/stories/import-<uuid>` after validation. Normal failure rolls back SQLite and removes published/staged files. Abrupt-process leftovers older than24hours are swept on the next import, excluding active locked stages. No periodic cleanup/polling is introduced; if no further imports occur, crash leftovers remain until the next import or operator cleanup. One fixed publication lock remains. Completed chapters persist until the user deletes the story through the existing flow.

A process crash after rename but before metadata commit can leave a **complete** story directory visible through the legacy filesystem fallback. A same-key retry repairs its metadata. This is recoverable publication, not a cross-filesystem/SQLite transaction or a claim of atomicity across power loss. A failed/closed tab does not retain its retry ID after page reload; users should check the library before starting a fresh import after ambiguous completion.

## TDD and verification

- Initial service/API/helper tests failed before their modules existed.
- Regression RED: folder containing only unsupported files had no explanatory alert. GREEN: skipped count + actionable file-count alert, disabled button.
- Backend: **28 passed**, 2 existing dependency deprecation warnings. Actual multipart → publication → chapter read → delete; auth rejection; bounded request/file/aggregate sizes; invalid input; concurrent idempotency; rollback; metadata recovery; active/expired staging cleanup; interrupted/cancelled multipart handle cleanup; count-only SSE privacy.
- Bulk service test: **489 synthetic chapters**, 10,562,400 input bytes; exact chapter count, no remaining stages.
- Frontend unit suite: **250 passed**.
- Frontend production build: pass (existing chunk warnings).
- Automated Playwright UI regression: **3 passed** (desktop +390×844 mobile preview/failed upload/same-ID retry; unsupported-only selection). These use intercepted HTTP, and are not substituted for real backend acceptance.

## Direct computer-use acceptance (real local backend)

Full `server:app` on8050, Vue UI on3040, isolated SQLite/story storage. Test provider credentials were used; no chat/LLM generation was requested. Authentication used the actual login UI/session cookie and normal CSRF-protected FormData upload.

1. Chrome/macOS **native folder picker** selected a generated folder with3direct chapters,1nested chapter and1JSON file. The extension chooser lacked file-URL permission, so the native picker was used without changing extension security settings.
2. Preview showed `2_Alpha.txt`, `2_Beta.txt`, `10_End.txt`; skipped count2; confirm button enabled.
3. Import navigated to the new story, exactly3chapters in preview order.
4. Reader opened chapter1 and displayed exactly `Synthetic first chapter.`
5. At an actual390×844 viewport, native multi-file selection imported another3chapter story successfully. Document width390; dialog width356; no horizontal overflow. This is Chrome responsive emulation, not physical iOS/Android picker certification.
6. Existing background TTS discovery reported all3chapters ready. Audio playback itself is outside this import acceptance.

Evidence:

- [Desktop preview](desktop-preview.png)
- [Desktop imported story](desktop-imported.png)
- [Desktop reader](desktop-reader.png)
- [Mobile dialog](mobile-dialog.png)
- [Mobile imported story](mobile-imported.png)

CI must be verified on the submitted commit before marking the PR ready. Production upload smoke is a deployment check; no production import is claimed.

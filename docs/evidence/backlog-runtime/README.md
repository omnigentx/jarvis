# Runtime reliability acceptance — SCRUM-50/45/49/48/51/46

Base: `a4c0526` (PR168 merged). Tests use pinned fast-agent submodule `8ac9227`.
All runtime tests are local and isolated. No production deployment or retrospective billing change.

## Changes and root causes

| Ticket | Root cause / change | Evidence |
|---|---|---|
| SCRUM-50 | Error dictionaries returned as ordinary FastMCP success. Return canonical `CallToolResult(isError=true)` for domain/transport failures; pending approval remains nonerror. Authorization unchanged. | Actual SDK `call_tool` serialization tests; 403 incident replay screenshot `mcp-denial-replay.png`. |
| SCRUM-45 | Lifespan hardcoded `data/jarvis.db` while configured SQLAlchemy engine used another DB. Bridge factory uses canonical engine database. | Two real SQLite DBs, misleading env override, only canonical events selected and delivered to the real push subscriber. |
| SCRUM-49 | Isolated producer emits `turns[-1]` per call; parent subtracted prior call as cumulative. Versioned per-call adapter and SQLite unique source event identity; duplicate replay produces neither row nor SSE. | Nine actual incident call values: input **75,621**, output **622**, cache-hit **40,192**, versus old input **9,655**, output **170**. Recreated bridge + real SQLite dedupe, same-run model switch/cache/reasoning tests; legacy-column migration preserves two old rows across repeated boot. |
| SCRUM-48 | Queued wake failure was reported as queued/started; startup did not reconcile raw unread team inboxes. Report message ID/wake outcome; one startup scan wakes latest scoped run with existing framework liveness/launch guards, retaining pause intent. | Durable MessageBus tests; real-LLM initial/live/dead-process recovery, same team/member/workspace/context marker. |
| SCRUM-51 | `resumed` lifecycle event did not reach cycle gate; second idle transition couldn't reopen/close worker cycle. Dispatch resume/pause events to gate. Idle report no longer declares work finished. | Real registry/cycle-store resumed→idle and repeated-idle tests; live worker wake reaches PM. |
| SCRUM-46 | Python 3.13 `Server.wait_closed()` waits active transports; lifespan waited socket stop before terminating idle children that held connections. Close owned client transports first. Restart resets stopping state; inode ownership protection remains. | Real UDS idle client stops under 0.5s; same-instance restart; existing live-peer/inode tests. Real Uvicorn backend SIGTERM/restart twice: **0.755s / 0.529s**, socket removed/client EOF, no forced kill (`backend-sigterm.json`). |

## Results and boundaries

- Backend full suite: **2,510 pass, 7 skip, 5 deselected, 1 expected failure**; see `backend-suite.txt` (final clean run replaces earlier report).
- Frontend unit: **244 pass**.
- Mocked UI E2E: plugins/monitor **8 pass**; token usage/provenance desktop + mobile **8 pass**.
- Real LLM team: **passed, 20.43s**, cleanup passed. PM and Dev retain marker through live wake and unread-inbox recovery after both child processes stop. `live-inbox-recovery.json` and selected `live-lifecycle-usage.json` are sanitized evidence.
- Computer-use screenshots: actual denial replay, pricing notice desktop and 390px mobile. Token UI screenshot uses empty fixture, so **$0.00 is not measured live-team cost**.
- UI labels USD as configuration-derived estimate; aliases can use generic fallback rates, not verified invoice cost. Existing historical rows remain untouched; source traces lacking provenance cannot be reconstructed honestly.
- Untimestamped legacy usage can be persisted but cannot safely deduplicate; timestamped current producer events have stable identity across replay/restart. Unsupported cumulative contract is rejected explicitly.
- Team recovery does not provide universal exactly-once LLM execution across a crash during a provider request. It reuses framework launch guards and durable inbox acknowledgement rather than inventing transactional provider behavior.
- Existing meeting bridge DB polling is unchanged; this patch adds no polling. Moving legacy meeting transport to push is separate work.
- First live attempt restored responses but failed an incorrect harness assumption that TeamSession's chain-anchor run ID must rewrite. Corrected to canonical registry `get_latest`/AgentChannel liveness; continuity assertions remain. Second full live run passed.
- First broader suite lacked initialized Atlassian submodule and two mocks returned no MessageBus message ID. Fixed test setup/contracts. A later concurrent standalone-backend attempt generated a secrets file and caused three environment-specific tests to fail; final suite runs with clean fixture environment, separately from backend startup.

## Review and rollout

Token schema migration is additive: nullable event ID and unique index; legacy NULL rows preserved. SQLite uniqueness serializes duplicate delivery. This touches widely-used token persistence and DB initialization (GitNexus HIGH/CRITICAL reported before changes); broad regression suite covers direct dependents. No prompt edits or permission relaxation.

Run backend regression suite with `cd backend && UV_FROZEN=1 uv run pytest tests/ -q`.
Run live harness with `UV_FROZEN=1 uv run python scripts/verify_team_resume_live.py --secrets <local-secret-file> --venv <backend-venv> --output <evidence-dir> --recover-inbox`.
For UI tests: build frontend, then `PW_PORT=4197 PW_FULL_MATRIX=1 npm run test:e2e -- tests/e2e/flows/token-provenance.spec.ts tests/e2e/flows/token-usage.spec.ts --project=chromium-desktop --project=chromium-mobile`.

Standalone backend SIGTERM harness initially waited for suppressed Uvicorn INFO logging instead of the actual `Jarvis backend ready` marker; fixed readiness detection. Exit status `-15` is expected: installed Uvicorn `server.py:314-331` explicitly re-raises the captured SIGTERM after graceful shutdown. Final two-round run validates this behavior with cleanup evidence. During static MCP discovery, the known SCRUM-44 finance dependency incompatibility (`mcp` 2.x removed FastMCP) also reproduced; it is outside these six tickets.

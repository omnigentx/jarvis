# PR 163 session routing acceptance (in progress)

## Latest acceptance checkpoint (2026-09-28)

- Jarvis Chat created a second independent session `14688e5f` alongside
  `450bcd26`, visible together in Monitor. The spawner enforces globally
  unique display names: it rejected requested duplicates Frankie [PM] and
  Eden [Dev], then assigned Bailey [PM] and River [Dev]. The user changed the
  requirement through Chat; revisions 1 and 2 reached team B's PM inbox and
  did not appear in team A's inbox. [Creation](team-b-created-ui.png) ·
  [revision](team-b-revision-ui.png) · [two-team Monitor](monitor-team-b-after-frontend.png).
- Team B PM created Jira [SCRUM-35](https://omnigentx.atlassian.net/browse/SCRUM-35)
  and [Confluence page 98855](https://omnigentx.atlassian.net/wiki/spaces/SCRUM/pages/98855).
  River completed a read-only Monitor inspection. SCRUM-36 duplicated the
  task after River tried the wrong local ports; it was closed with an explicit
  duplicate note. SCRUM-35 is Done; SCRUM-32 remains In Progress for PR review.
- A backend shutdown emitted a role-only lifecycle event that overwrote two
  stored PM display names with `pm`. On worker close, event
  `14688e5f:worker_cycle_closed:3` woke `pm`, not Bailey. The code now takes
  the canonical name from the registered spawn configuration, repairs legacy
  rows at startup, and resolves the orchestrator against the session roster.
  Startup log repaired runs `44a26c4f` and `f04aae05`. A second real Monitor
  inject to River produced `ACK-REPAIR-02`; event
  `14688e5f:worker_cycle_closed:4` woke **Bailey [PM]**. Bailey connected
  9/9 MCP servers, read SCRUM-35, replied, idled, and event
  `14688e5f:full_cycle_closed:4` notified the user.
  [Monitor screenshot](team-b-wake-after-repair.png). The screenshot shows
  the visible team; the run IDs and routing outcome come from
  `backend/core/logs/spawn_activity.log` and the session inbox JSONL.
- The merged team frontend patch keys Monitor identity and history by session.
  A built-in inject now sends `target=static` so a same-named team member
  cannot intercept it. A regression HTTP test checks that routing. New
  role-only event, repair, and stale-roster tests are included. Focused
  backend **53 passed**; full non-Cloud backend **2,292 passed, 4 skipped,
  1 expected failure**; frontend unit **227 passed**, build passed; focused
  desktop/mobile browser matrix **4 passed**. The full backend suite retained
  a multiprocessing child after printing its summary; terminating that child
  allowed exit code 0. This test-harness cleanup remains backlog.
- Recorded team B usage at this checkpoint: **58 model calls**, **251,752
  uncached input**, **198,400 cache-hit input**, **3,358 output** tokens,
  estimated **USD 0.877**. Team A recorded 1,553 calls, 1,355,201 uncached
  input, 1,690,624 cache-hit input, 115,729 output, USD 5.669 estimate.
  A single River `ACK-REPAIR-02` resume consumed 41,115 input for 10 output
  tokens. Actual Monitor turns show full PM skill and Jira tool responses
  repeated in agent history; the cost number alone does not establish which
  content is removable. A follow-up must compare task success and tool calls
  with and without bounded history, then retain only proven improvements.

### Remaining backlog, prioritized

1. **P1 — Prevent repeated full-context resumes.** Measure the River 41k/10
   turn and PM 33k input turn by content section; test a bounded-history
   strategy against the same multi-step task before changing defaults.
2. **P1 — Atomic per-session team identity.** The spawner still forbids
   duplicate display names globally, so true same-name *natural UI* E2E
   could not run. Scoped socket/HTTP tests prove isolation for injected
   fixtures; they do not prove same-name team creation through Chat.
3. **P2 — Keep team task status aligned.** Team B finished SCRUM-35 while
   Jira still read To Do; Codex reconciled it manually. The PM workflow
   should transition at actual phase boundaries and close duplicates.
4. **P2 — Reduce redundant PM/QE exchanges and tool output.** Review the
   repeated no-reply emails, large skill fetches, and verbose Jira responses
   in actual turns. Use a controlled A/B task with quality and cost metrics.
5. **P2 — Repair test-harness child shutdown.** Full pytest passes but leaves
   a multiprocessing child alive until termination.

This checkpoint proves independent-team operation and the observed worker-to-PM
cycle after restart. It does not establish exactly-once side effects after a
crash, nor a natural UI same-name-team creation path.

## Reproduced failure before the fix

Two `AgentChannel("Alex", same_dir)` servers resolved to the same socket path.
The second server replaced the first path. One `wake` reached only the second
listener: `same_socket_path=true`, `sent=true`, `first_received=null`,
`second_received="wake"`. The production path used display-name-only
`auto_wake_if_idle`, `find_by_name`, and `has_running_resume`, so a revision
queued in one team's inbox could awaken another team's same-name PM.

## Changes under verification

- fast-agent uses `(session_id, agent_name, run_id)` for team channels and
  verifies socket ownership before removal. A session-scoped wake validates
  the expected run and raises on ambiguous or failed routing.
- Jarvis revision delivery and startup reconciliation pass the owning session
  and root run to that wake API. Startup checks exact unread message IDs in
  each session inbox; it does not use the former 30-second wake lease.
- `/api/agents/{name}/inject` returns HTTP 409 when the name belongs to more
  than one identity unless `session_id` selects the intended team.

## Evidence collected so far

- fast-agent spawn suite at commit `65e922c9`: **95 passed**. It includes two
  real same-name subprocesses, a >30-second concurrent launch, SIGKILL and
  restart recovery, and a pid-less launch-owner crash within the grace window.
- Jarvis focused service/route suite: **28 passed** with the updated submodule.
  The two-socket service test delivered revision A only to team A and revision B
  only to team B. An HTTP-to-real-socket test rejected a name-only inject with
  409, sent `?session_id=team-b` to B's inbox and socket, and observed no wake
  or message in A's inbox.
- Localhost Jarvis served from a separate checkout on backend `127.0.0.1:8013`
  and Vite `127.0.0.1:3006`, using an isolated copy of the local database with
  old team/run/scheduler rows removed. The UI accepted login and returned `OK`
  to a no-tool chat turn. [Screenshot](local-chat-ok.png). Atlassian remains
  configured for the Cloud test tenant.
- Through the local Chat UI, Jarvis spawned team `450bcd26` with Frankie [PM],
  Eden [Dev], and Finley [QE]. While PM was working, the user sent two further
  chat turns. Jarvis recorded revisions 1 and 2 as delivered (`921ca1d9` and
  `1106e099`); the PM's next displayed turn contained revision 1, then PM
  updated SCRUM-32 and Confluence page 65990 and forwarded both revisions to
  Dev/QE. [Chat screenshot](chat-two-revisions.png) ·
  [Monitor screenshot](pm-revisions-delivered-ui.png).
- QE first navigated to port 3000 and got `ERR_CONNECTION_REFUSED`. The real
  frontend was on 3006. After receiving the corrected URL, QE's separate
  browser lacked local authentication and got HTTP 401. These are test setup
  issues, not proof that recipient routing passed or failed. PM recorded them
  on SCRUM-32 and the Confluence page.
- Dev created an isolated worktree but `git submodule update --init --recursive`
  timed out while cloning fast-agent. Dev then read the remote PR #163 at its
  old commit and concluded that the scoped API contract was absent. The local
  fix had not been pushed at that time; this exposed a handoff sequencing gap.
  No frontend code or Dev PR was produced at that point.

## Not yet accepted

- The current team has received revisions and updated Jira/Confluence, but
  restart recovery and a complete two-team UI E2E still need direct proof.
  A reviewable frontend PR has not yet been produced by the test team.
- The whole monitor/store still keys agents by display name and can collapse
  same-name agents. The API guard prevents misrouting but does not make those
  agents separately selectable in the UI.
- A crash after an agent performs a side effect but before acknowledging its
  inbox may replay the directive. Exactly-once semantic application requires
  idempotency at downstream tools or task boundaries.
- The local memory engine logged that `sentence_transformers` was missing;
  dense recall and knowledge graph were disabled. This is a separate test
  environment limitation, not evidence that session routing works or fails.

PRs 159 and 163 remain Draft until the above acceptance gates are evidenced.

## Additional safety checks (2026-09-27)

- Fast-agent #14 `69af80e9` moves inbox acknowledgment from the
  `before_llm_call` hook to `after_llm_call` after a non-cancelled model
  response. A failed call leaves the message unread for a new runner; a
  cancellation leaves it unread; a same-runner retry does not append the same
  instruction twice. The focused hook tests and spawn suites passed
  **113/113**; the fast-agent PR's latest CI jobs passed. This reduces message
  loss, but side effects after the model response still require idempotency.
- Jarvis #163 `7b965f2` rejects name-only pause, resume, and delete with HTTP
  409 when records span multiple team sessions. Tests first failed against
  the old routes, then passed; multiple runs in the *same* session remain
  pauseable. This is a fail-closed guard, not a scoped-control API.
- `delete_team` had a separate cross-team risk: after deleting by team name,
  it called `delete_by_name()` for each member and purged activity/memory by
  name. With shared member names that could remove another team's data.
  Commit `1f6c197` rejects member-name or team-name collisions before any
  cleanup. Both collision regression cases first failed on old code and now
  pass. Session-scoped deletion and per-session memory ownership remain
  backlog; concurrent create/delete must also be handled atomically.
- A read-only probe of the localhost `/api/agents/activities/recent` path
  returned **1,883 rows / 1,102,081 JSON bytes**. The service ran **124 SQL
  queries** for 123 historical agent names in 125.2 ms server-side; one HTTP
  request took 152.2 ms. The current Monitor does not render that activity
  buffer: its canonical turn history comes from `useAgentTurns`. Removing the
  unused prefetch is under review. These are response/DB measurements, not
  claims of lower LLM token usage.
- The team has a frontend identity patch in its isolated worktree. It has not
  yet been integrated into #163 or accepted by a two-team browser run. The
  localhost backend process is still serving the earlier code until the
  active Dev task finishes and a controlled restart is safe.

## Follow-up on 2026-09-27 (current code `e53e1d1`)

- The first direct Monitor inject (`fa01cf79`) **was processed**; its raw
  JSONL line still said `unread`, but the separate processed-ID file and Dev's
  turns proved consumption. The second inject (`fa403a9a`) reproduced a real
  loss: its ID was marked processed, the child was killed, and no matching user
  turn appeared. In fast-agent #14 `f97959d8`, the keepalive path now calls
  `agent.send`, attempts the context snapshot, and only then acknowledges the
  pending IDs. An interrupted send remains unacknowledged and retryable.
- A new injection through the real localhost Monitor woke Eden [Dev]. The
  subprocess log at 22:15:07 shows the Dashboard task as user `message_turn`
  zero, before any tool calls. At 22:15:01 all 11 configured MCP servers were
  connected. Dev then read a clone at `5554d00` and initially repeated the
  missing-contract verdict. After an explicit correction through Monitor, Dev
  cloned the `55d78e0` PR checkout locally. PM subsequently stopped Dev because
  the history/SSE contract was still incomplete at that revision. This is
  evidence of a moving-head handoff problem and unnecessary agent/tool cost,
  not a successful frontend implementation.
- Backend `e53e1d1` now scopes the roster, message history, full-turn reads,
  live turn cache, and subprocess `message_turn` SSE by session. A name-only
  history read for duplicate names returns 409; a wrong explicit session
  returns 404. Session-isolation regression tests first failed on the old
  implementation, then passed after the change. Focused suite: **65 passed**;
  complete backend suite: **2,275 passed, 1 skipped, 5 deselected, 1 xfailed**.
  The suite printed its result but retained a multiprocessing test child; after
  terminating that child, pytest exited 0. This cleanup defect remains.
- After restarting the README-equivalent localhost backend on `e53e1d1`, the
  real HTTP request for Eden [Dev] with `session_id=450bcd26` returned 94 turns;
  full turn zero returned 200. The same name with `session_id=wrong` returned
  404. The browser Monitor reconnected and showed the same team
  ([screenshot](monitor-after-restart.png)). This screenshot shows reconnection
  and the team's current status, not duplicate-name UI acceptance.
- Parent Jarvis #159 and fast-agent #14 latest CI jobs are green. PM has been
  injected with the new `e53e1d1` contract and forwarded it to Dev for a
  separate-workspace frontend implementation. The two-team same-name UI E2E,
  QE verdict, screenshots, and task-level cost/quality analysis remain open.

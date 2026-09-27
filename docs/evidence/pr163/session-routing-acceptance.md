# PR 163 session routing acceptance (in progress)

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

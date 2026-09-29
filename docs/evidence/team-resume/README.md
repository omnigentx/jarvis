# SCRUM-39 — Production team resume failure

## Confirmed incident

Read-only SSH investigation on 2026-09-29. Production container: `jarvis_backend`.
Persisted conversation: `/app/.fast-agent/sessions/2609281444-J3pKGw/history_Jarvis.json`.
Source checksum of `fast_agent/spawn/servers/agent_spawner_server.py` matches the
local source at fast-agent `69af80e9617df7edc72853155454b4a295ccfab9`:
`484ddd5009fb2b32c10034996600e31e019aa433d1b602511073f77f733cc394`.

Timeline (UTC; add seven hours for Vietnam):

- 03:25:49: Toby PM receives a message and resumes. This incident does **not**
  establish that all wake mechanisms are broken.
- 03:43:17: Jarvis calls `resume_team_tool` for session `52051546`.
- Result: four idle members skipped as "still running (status=idle)"; three
  reserved roles skipped because they have no spawn record. Tool reports
  `status=resumed`, `resumed_agents=0`, `skipped_agents=7`.
- 03:43:32: Jarvis calls `spawn_team_tool`, creating replacement session
  `aed46b2a`, team `jarvis-clear-backlog-report`.
- 04:28:56: retry on the original session returns the same false-success result.

`production-resume.json` contains the actual two tool results, timestamps and
source checksum. Prompts and unrelated conversation contents are excluded.

## Root cause and fix

The team tool required `record.is_terminal`. The registry deliberately does not
classify `idle` as terminal, while the individual `resume_spawn` already accepts
idle. Thus the team guard rejects the exact state the individual tool supports.
An unconditional success return and sprint-status write conceal the failure.

The fix preserves team/member identity and workspace:

- Accept idle and terminal resumable members.
- For a live member, enqueue into its session inbox and signal its actual Unix
  socket. Report `queued`; do not launch a duplicate process or claim completion.
- For a dead member, call the existing snapshot-based `resume_spawn`.
- Preserve available/reserved roles; do not start unrequested agents.
- Report `not_resumed`, `partial`, `queued`, or `resumed` from actual outcomes.
- Keep the session state unchanged if no member accepts work.
- Serialize concurrent team-resume calls across MCP processes using nonblocking
  file locking. A competing call reports `busy`. Lock files are empty and remain
  for the session lifetime; deleting an in-use lock would break mutual exclusion.
- Reject mismatched agent/team records and surface individual restore errors.

No prompt changes, polling, production DB updates, or production restarts.

## Verification

Command (from repository root, with this checkout's fast-agent on PYTHONPATH):

```sh
PYTHONPATH="$PWD/backend/fast-agent/src:$PWD/backend" python -m pytest \
  backend/tests/test_services/test_team_resume_incident.py \
  backend/tests/test_services/test_inject_resume.py \
  backend/tests/test_services/test_auto_wake_session_path.py \
  backend/tests/test_services/test_get_team_result_fail_loud.py -q
```

- `red.txt`: unmodified production-equivalent code fails the incident replay:
  expected four restarts, actual zero.
- `green.txt`: 39 tests pass after the fix (21 new and 18 adjacent regressions).
- New cases cover idle/terminal states, active/paused states, missing records,
  unsupported lifecycle, identity/session mismatch, absent snapshot, launch
  failure, partial outcomes, wake failure, empty input, unknown session and
  concurrent requests.
- Real Unix socket + MessageBus test verifies the follow-up content and wake
  delivery, with no spawn and no run-ID change.
- Real SQLite snapshot/registry test calls the actual `resume_spawn`, verifies
  history, team session, name, workspace, skills, server overrides, successor
  chain and rejection of a repeat launch while the successor is running.

Initial deterministic-suite limits: the dead-process integration substitutes the LLM process launcher. This
is deterministic regression/integration evidence, **not** a live-LLM team E2E
or production-deployment acceptance. Live acceptance was subsequently completed below. `queued` confirms enqueue/signal, not that
an agent finished its task. Cross-path launch races involving other tools are
outside this session-lock guarantee.

## Separate observations / follow-up

The replacement team's empty report and recurring LadybugDB vector-extension
load failures need separate investigation; neither is established as the cause
of the confirmed idle-state rejection. Existing `send_team_message` inbox/wake
behavior and cross-tool launch idempotency deserve follow-up coverage. Do not
promote the previous agents' speculative architecture report into confirmed
root causes without a reproduction.

## Live LLM acceptance completed (2026-09-29 16:07 UTC / 23:07 ICT)

The opt-in `backend/scripts/verify_team_resume_live.py` now runs the real team
spawn/member/resume tools, real isolated Python runners, real email/meeting MCP
startup, production SpawnProgressBridge + event socket, SQLite and local 9router
(`openai.coding-agent`). No LLM or process launcher substitution is used.
The harness owns a temporary project/DB and waits on pushed lifecycle events.

Final run: **17.86 seconds, passed, cleanup passed**, session `742a6c89`.
See `live-result.json` for the actual session ID, tool results and model replies.

| Phase | Verified behavior |
|---|---|
| Initial | PM + Dev both produce the randomly generated memory marker |
| Live resume | 2 queued, 0 restarted, same run IDs; both reply with LIVE and the original marker |
| Dead resume | Stop the owned runner processes; 2 agents restored with successor run IDs, same session/names/workspace; both reply with RESTORED and the original marker |
| Teardown | Test child processes stopped, socket clients closed, temporary credential copies removed; harness exits 0 |

The follow-up prompts do not contain the marker. Dead-agent restoration uses
SQLite history; `resume_spawn` reconstructs roster context without the initial
project brief. PM roll-up wake events are allowed to finish before the next test
phase, rather than mistaking an earlier idle event for current idleness.

### Additional defect caught by the live test

The first candidate queried `AgentChannel` using only agent name. The current
runtime sockets are scoped by `(session_id, agent_name, run_id)`, so the probe
incorrectly classified live agents as dead. `live-scope-failure.json` preserves
the failed run showing two restarts instead of two queued deliveries. Fixed both
liveness probe and wake delivery to use the same session/run identity as the
runner. The real-socket regression test now creates a scoped socket, preventing
the previous unscoped test fixture from masking this defect.

Setup attempts also exposed missing email/meeting MCP specs and helper-module
links in the isolated harness, and an unclosed parent socket during teardown.
These harness defects were corrected; they are not attributed to production.
`live-events.json` contains filtered actual model/lifecycle/token events. The
model made zero tool calls in this narrow memory-continuity scenario: this
acceptance covers team resume, not broader Jira/Confluence or coding workflows.

Reproduce explicitly (uses live model quota):

```sh
python backend/scripts/verify_team_resume_live.py \
  --secrets /path/to/local/fastagent.secrets.yaml \
  --venv /path/to/existing/backend/.venv \
  --output /tmp/resume-acceptance-evidence
```

Supersedes the earlier live-LLM limitation above. Production rollout remains a
separate post-merge step; no production code or data was changed by acceptance.

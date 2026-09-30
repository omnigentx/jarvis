# SCRUM-37 — backend pytest child cleanup

## Reproduction before the change

On 2026-09-28, the consolidated PR #159 worktree ran
`UV_FROZEN=1 uv run pytest -q tests/`. Pytest printed
`2291 passed, 1 skipped, 5 deselected, 1 xfailed` in 122.65 seconds,
but its process (PID 43177) did not exit. Two direct children remained:
`multiprocessing.resource_tracker` (PID 43606) and
`multiprocessing.spawn` (PID 43607). The spawned process loaded
CTranslate2, torch and audio libraries. `backend/core/logs/jarvis.log`
at 00:56:27 recorded `STT service rebuilt: backend=faster_whisper`
immediately before WebSocket auth test entries. Terminating only PID 43607
let pytest return exit code 0.

The auth fixture in `backend/tests/test_routes/test_ws_voice_auth.py` let
accepted WebSocket connections create the real STT recorder. The test only
needs an accepted socket and a status event to distinguish it from HTTP 4401.

## Change and verification

The auth fixture now injects a lightweight STT double that emits `ws_status`
when its hook is installed. It retains the real FastAPI WebSocket, auth checks,
route startup and cleanup code. It does not start the speech model.

On an isolated worktree from `origin/main` (`5554d00`):

- Focused auth and single-owner suites: 21 passed in 4.17 seconds.
- Full backend suite: `2235 passed, 4 skipped, 5 deselected, 1 xfailed`
  in 143.31 seconds; command returned exit code 0 without manual signals.
- Process check after completion found no `pytest`, `resource_tracker`, or
  `multiprocessing.spawn` process from this worktree.

The before and after totals differ because the reproduction used the later
PR #159 branch while verification used `origin/main`. This establishes the
teardown fix on main; CI must validate the PR's exact pushed head.

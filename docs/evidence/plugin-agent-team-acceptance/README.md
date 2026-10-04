# MR #174: actual plugin and team acceptance

2026-10-04: localhost backend 8048 / frontend 3038, isolated `data/rovo-e2e/jarvis.db`, real Atlassian Cloud. Supplements `../official-plugin-matrix/README.md` and `../rovo-live-oauth/README.md`.

## Evidence matrix

| Case | Result | Evidence |
|---|---|---|
| Jarvis installs official Rovo for itself and immediately reads SCRUM-55 | PASS | `agent-self-rovo.jpg` |
| PM activates official frontend-design for Dev; live Dev immediately reads vendor skill | PASS, no Dev restart | `pm-subordinate-ready.jpg`, `dev-immediate-skill-use.jpg` |
| Dev attempts to activate a plugin for PM | PASS: 403, no PM binding | `team-tool-input-output.json` |
| Dev installs/activates official migration skill for itself and immediately reads it | PASS | `dev-self-install-use.jpg` |
| Invalid credential against actual Rovo | PASS: anonymous HTTP-200 catalog rejected; token cleared | `live-rejected-credential.json` |
| Forced local expiry, real vendor refresh, recreated providers | PASS: one refresh, rotated credentials, two successful reads | `live-refresh-after-restart.json` |
| Actual host stdio worker and real vendor, fault/recovery, Settings SSE | PASS: 27 tools and SCRUM-55 before/after, rejected tokens removed | `live-worker-ui.json`, `remote-disconnected-push.jpg`, `remote-disabled-activation.jpg`, `remote-restored-push.jpg` |
| Connected/disconnected account overrides stale ready binding | PASS desktop/mobile, controlled UI regression | `remote-ui-red.txt`, `remote-ui-green.txt` |
| HTTPS callback route state/success/replay/no cache/referrer/code echo | PASS real TLS verification; controlled OAuth code, not vendor exchange | backend HTTPS route regression |
| Callback proxy credential-log privacy, unavailable upstream | PASS real Docker Nginx | `proxy-red.txt`, `proxy-green.txt` |
| PM/self Rovo add → review → activate → real issue read | PASS, same live runtime | `pm-rovo-approval.jpg`, `pm-rovo-immediate-use.jpg` |
| PM/subordinate Rovo add → review → activate → real Dev issue read | PASS after correcting stale run-ID detour; no restart for immediate use | `dev-rovo-approval.jpg`, `pm-rovo-subordinate-ready.jpg`, `dev-rovo-immediate-use.jpg`, `team-rovo-tool-input-output.json` |
| Deployed production HTTPS OAuth smoke | NOT RUN; pre-release gate | Production inspected read-only |

Pinned official packages and actual plugin content were used. The two-role template controls test composition; it does not replace plugin content. Approval steps used explicit UI continuation prompts: automatic continuation is not claimed (SCRUM-53).

## Root causes and fixes

- Rejected credentials remained persisted when SDK reconnect failed. Invalidate credentials durably, preserve registration, emit safe `plugin_status` through the existing event socket/ActivityStream. CAS prevents stale workers clearing rotated credentials; duplicate invalidations emit no extra event.
- Rovo initializes with HTTP 200 for invalid credentials but exposes only four generic tools. Require its authenticated account catalog marker. Recheck the observed missing-tool error once; never retry a write. Ordinary issue permission errors retain credentials.
- Recreated SDK providers do not restore absolute expiry/issuer discovery. Persist public issuer/resource binding and absolute expiry encrypted. Narrow host-owned RFC6749 refresh uses the existing cross-process lock; SDK still owns consent/PKCE/MCP, no private context mutation. Denial clears credentials; transient failure retains them. The first live refresh exposed unread streamed response; added regression and reran successfully.
- Stale binding readiness hid account disconnection. Remote account component emits readiness to parent; disconnected packages cannot show green Ready or enable activation.
- Production Nginx logged callback code/state in both access/error output during failure. Callback-only query-free access logs retain route/status/timing; duplicate raw error logging is suppressed for that route. CI runs real Docker privacy/cache regressions.

## Tests and limits

Focused backend regression after credential race fix: **243 passed, 3 skipped**, 5 dependency warnings. Frontend plugin UI regression: **17 passed**, including 390px mobile. Docker deployment regressions: **2 passed, 0 skipped**. Final GitHub CI must correspond to the final source commit, not earlier `36ed362`.

Expiry and invalid-credential tests inject faults into local state. They do not prove naturally elapsed expiry or an external vendor grant revocation. No production data or OAuth grant was revoked. Original valid local credentials were restored after the worker fault and real issue-read control passed again.

## Harness failures

My first launcher omitted `SPAWN_REGISTRY_DB` while setting isolated `JARVIS_DB_PATH`; plugin and team databases/socket roots diverged. Corrected both variables, stopped only owned test processes and resumed their saved context. Migration then assigned old PIDs `None`, causing correct fail-closed ambiguous-runtime rejection; restored known terminated historical PIDs. These were harness mistakes, not default startup defects. See `environment-failure.json`.

## Cost and backlog

`team-tool-input-output.json` holds real arguments/results/bindings; vendor skill bodies use hashes/lengths. The run includes harness failures/recovery: PM 18 LLM turns, 176,542 input / 1,612 output; Dev 19 turns, 167,205 input / 1,396 output. Effective counters sum 132,547; maximum contexts 11,678 / 13,054. Not a clean A/B; no trustworthy router price resolved, so no dollar-cost or savings claim.

Repeated acknowledgments/status wakeups belong to SCRUM-11/18/21/16. A textual `to=functions.email...INVALID` artifact occurred before actual tool success: provider/model investigation, not a concluded prompt bug. SCRUM-57 tracks capability-aware delegation; SCRUM-58 build-ios compatibility. GoogleNews startup fails on selectolax 1.0's removed Modest parser: separate dependency issue. The team Rovo grant was confirmed and exercised below.

## Deployment preflight

Production read-only inspection: `JARVIS_PUBLIC_URL` unset. Deployment copies `~/jarvis-data/.env` into `backend/.env`; configure the exact HTTPS public origin in that persistent file before deploying. Do not hardcode one deployment's origin in generic code. Local verified-TLS callback regression does not replace production consent smoke.


## Follow-up credential race audit

A deterministic regression reproduced rejected rotated credentials remaining in storage: the catalog's initial credential snapshot predates refresh or another worker's consent, so CAS correctly protects the newer token but the guard had not validated it. `list_account_tools` now performs exactly one extra catalog validation only when persisted credentials changed during the rejected request. An unchanged rejected token uses one call and is removed; a valid new credential is preserved; a rejected new credential is removed only after its own catalog fails. No tool operation is retried and repeated rotations cannot create an unbounded retry loop. This guard is shared by browser consent and remote-worker catalog/runtime calls.

`catalog-rotation-red.txt` shows the persisted rejected token assertion failing before the fix; `catalog-rotation-green.txt` covers both recovery/denial and single-use refresh across two independent token stores. `catalog-rotation-regression.txt`: **243 passed, 3 skipped**, 5 warnings. These race tests use controlled transport/storage responses, not live external revocation; existing real vendor evidence establishes the anonymous-catalog contract. Full CI needs the new source HEAD after this fix.

After backend restart with the race fix, direct chat UI called the actual Rovo `getJiraIssue` once and returned SCRUM-55 summary/status (tool duration 0.6s). See `rovo-after-catalog-race-fix.jpg`. Direct computer-use mobile evidence at 390×844 (document width 390): `rovo-mobile-connected.jpg` and `rovo-mobile-activation.jpg`; selecting existing authorized Jarvis enables the full-width Activate button. No additional agent grant was made.


## Final team Rovo acceptance and runtime identity correction

The user explicitly instructed continuation after the named PM/Dev read/search grant request. The exact existing session `c2bdc284` was resumed, no replacement team. Avery PM runtime `08d27d13` added Rovo for itself, received a scoped content review, activated, and immediately read actual SCRUM-55 summary/status. PM then added and activated the same pinned Rovo package for existing Dev runtime `6aa42004`; the reviewed digest and stable role binding matched the UI.

The first Dev response to PM's internal email took an unnecessary detour: it called plugin_activate with historical run `86554e79` and received the expected 403. This is **not** a successful autonomous first attempt. A subsequent UI instruction corrected the run ID and asked for the installed Rovo tool; Dev immediately read actual SCRUM-55 without restart or another activation. The new tool was present; the failure was not a missing Rovo catalog. We preserve the failing screenshot `dev-rovo-stale-run-denied.jpg` and actual input/output in `team-rovo-tool-input-output.json`.

The service inventory response omitted current runtime identity, so it could not answer Dev's requested run-ID lookup. It now returns `target_agent`, `run_id`, and stable `binding` from the existing authorized target resolver, including null run ID for static agents. No prompt or permission checks were weakened. Two RED tests failed with missing target_agent, then GREEN passed, and plugin-management/runtime-control regression passed 14 tests. Post-restart actual UI verification is recorded separately. This metadata improvement does not prove every model will avoid unnecessary activation calls; capability-aware delegation remains SCRUM-57.

The additional pre-restart acceptance runs used PM 13 turns (192,497 input / 1,149 output, effective 55,281, max context 17,729), Dev 6 turns (102,216 input / 515 output, effective 43,848, max context 19,696). They include explicit review continuations and the stale-run detour; not a clean A/B and no dollar cost claim.

Post-restart actual UI PASS: Dev resumed as `e65454bc`; plugin_list returned that exact current ID plus stable role binding, then Dev used real Rovo getJiraIssue in the next model call, reading SCRUM-55 successfully. No activation retry or replacement team. See `dev-rovo-runtime-identity-after-restart.jpg` and `runtime-identity-after-restart.json`. Final source CI is linked in the PR comment after the evidence commit.

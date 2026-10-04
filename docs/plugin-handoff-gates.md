# Plugin acceptance and handoff gates

## Freeze the requested outcome before coding

List the plugin identity/version/source commit and digest, requested agents/teams, user task and supported capabilities. Record unsupported required capabilities as **blocking**, not optional follow-ups. Scope reduction requires an explicit user decision; it cannot be inferred from merging a partial fix.

## Required acceptance matrix

For the exact requested plugin, record each case as PASS / FAIL / BLOCKED / NOT RUN, with commit, environment, timestamp, artifact and test substitution details:

1. Manual add through UI: source review, acquisition, compatibility, authorization, target review and activation.
2. Agent-initiated add for itself and permitted subordinate; human review must resume the intended operation when required.
3. Immediate real tool/skill use in the existing chat/runtime and existing live team. ACK alone is insufficient. Result must establish the user outcome, not a fixture marker or unrelated built-in tool.
4. Single target and all-current-target scope, partial failure, authorization denial/expiry/revocation, stopped runtime, disconnect/reconnect, restart/resume and changed content/policy.
5. Desktop/mobile: directly inspect spacing, readable next steps, enabled/disabled reasons, confirmation and actual success/failure. Capture the final tested UI.
6. Cost/performance claims: compare equivalent multi-step outcomes and include real calls/usage/latency. No claim when not measured.

Fixtures remain valuable regression tests. They must be labeled and cannot replace acquisition, authentication and real external use when those are required by the task.

## Before Ready for review

- Reproduce the user's failure with the same package/digest; preserve failing evidence.
- TDD behavior/security regressions cover the root cause, not just the implementation.
- The exact final head passes relevant tests and CI. Skipped or xfailed tests are disclosed.
- Every required product case passes; otherwise the functional PR stays draft. An independently reviewable partial change is labeled partial and does not complete the larger acceptance task.
- PR comment states completed and incomplete outcomes separately, links artifacts, and records each substitution. Jira and Confluence reflect the same scope.

## Before release handoff

Verify the deployed commit and asset version, service health, then perform an authorized read-only smoke on the deployed user flow. Capture evidence of actual plugin use. A local fixture, merged PR, green CI or healthy backend does not complete production functional acceptance. If login or a new security grant requires the user, label the case blocked and hand off that step precisely; never weaken controls to turn the gate green.

Retagging an explicitly requested release is an operational action, not evidence that unmet product requirements are now complete. Release notes must identify the unresolved user-visible limitation prominently.

## Incident correction

When a user reports the same failure after handoff, compare original acceptance case, evidence substitutions, actual package/transport and deployed revision before another fix. Correct the previous acceptance claim in the PR/Jira/Confluence record. Do not close the incident with only more descriptive error text while the requested outcome remains impossible.

Current failing reference case: `atlassian-rovo` 1.0.6; see `docs/evidence/plugin-handoff-audit/README.md`.

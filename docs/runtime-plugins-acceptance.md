# Runtime plugins acceptance contract — SCRUM-42

## Product contract

Jarvis imports Claude, Codex and portable Agent Plugins through one normalized
descriptor. Jarvis ships no third-party plugin package. Files are fetched only
at runtime from an approved repository at an immutable commit. Inspection does
not execute scripts, hooks, installers or MCP servers.

Installation and activation are distinct. Ready means that every required
capability has been acknowledged by the selected live runtime. Persisting an
attachment, modifying an agent definition, or importing a manifest is not Ready.
No backend restart or replacement team is acceptable as activation evidence.

Current authentication is single-tenant. The initial implementation must not
accept a client-supplied owner ID or claim multi-user isolation. Global sharing
and executable components require separate approved policy and verified adapters.

## Required evidence

1. Unit contracts for each format, overlay precedence, path containment,
   archive limits, symlinks, unsupported capabilities and namespacing.
2. Lifecycle integration: approval hash changes, negative/no-op live ack,
   rollback, interruption, concurrent installs, cross-process operation locks,
   per-agent bindings, disable/uninstall, missing package on resume and cleanup.
3. Live activation through fast-agent instruction refresh and MCP attachment,
   including an already-running subprocess team at the next safe boundary.
4. Local UI install then immediate use in the same chat/team. Desktop/mobile
   screenshots and readable before/after traces accompany the PR.
5. Security tests: unapproved source, credential exposure, dependency changes,
   SSRF/redirects, malformed configs, malicious hook/install code, prompt
   injection attempting unauthorized actions, and resource exhaustion.
6. Quality and cost comparison on the same multi-step task. Count actual tool
   calls, model usage, latency and results; do not infer value from package size.

## Runtime files

Source bundles are disposable files under a private runtime directory. Persist
source identity, commit, content hash, review decision and bindings in SQLite.
Do not put downloaded packages under tracked skills or in the Docker image.
Keep active package versions until their last live binding is released. Proposed
unused retention is 24 hours; cleanup must never remove an in-use package.
Plugin output/data has a separate lifecycle and must not be deleted by package
cleanup. A missing or modified package blocks activation until the exact version
has been downloaded and verified again.

## Current gate

This document defines the acceptance target, not a statement of completion.
Parser/archive/lifecycle tests do not establish subprocess activation, sandbox
security, production readiness, or UI acceptance. PR stays draft until all
required evidence is available. Unknown components are reported and blocked;
they are never silently discarded to make an install appear successful.

## Acceptance report

See `docs/evidence/runtime-plugins/README.md` for implemented scope, actual live
UI/team/MCP evidence, tests, A/B results and tracked limitations. Local acceptance
is complete. PR168 remains Draft until its final CI passes; dependency PR19 must
be merged first. Unsupported host contributions fail closed rather than being
silently omitted. Production deployment remains a separate operator action.


## Handoff correction — 2026-10-04

The user's `atlassian-rovo` 1.0.6 workflow has **not passed**. Previous acceptance covered supported skills and sandboxed stdio MCP, not this zero-skill HTTP/host-connector package. See `docs/evidence/plugin-handoff-audit/README.md` for exact-source reproduction and `docs/plugin-handoff-gates.md` for required outcome-specific gates. UI fixes and passing fixtures do not close this functional gap.

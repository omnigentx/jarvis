<!--
Thanks for contributing to Jarvis! Please fill in the sections below.
For trivial PRs (typo, comment-only) feel free to delete sections that don't apply.
-->

## Summary

<!-- 1-3 sentences: what does this PR change and why? -->

## Linked issue

<!-- e.g. Closes #123 / Refs #456. If there's no issue, briefly justify the change here. -->

## Type of change

- [ ] Bug fix (non-breaking)
- [ ] New feature (non-breaking)
- [ ] Breaking change (API / config / DB schema)
- [ ] Refactor / internal cleanup (no behavior change)
- [ ] Docs only
- [ ] CI / build / tooling
- [ ] Test-only

## How was this tested?

<!--
Be specific. "Looks good locally" is not enough.
- Unit tests:        which ones, count, result
- Integration tests: which ones, against what (real DB / FastAgent / filesystem — NOT just mocks)
- E2E tests:         which spec(s), result. If a flow can't be exercised in tests, say so.
- Manual:            exact steps you ran
-->

## Screenshots / recordings (UI changes only)

<!-- Before/after screenshots, or a short screen recording. Required for any dashboard change. -->

## Checklist

- [ ] My code follows the style of this project (no unrelated reformatting / refactors).
- [ ] I added tests that cover my change, OR explained why tests are not applicable.
- [ ] All existing tests pass locally.
- [ ] Docs updated (README / CONTRIBUTING / inline docstrings) where behavior changed.
- [ ] No secrets, API keys, personal IPs, or PII in the diff.
- [ ] If this touches a submodule (`backend/fast-agent`, `backend/figma-ui-mcp`, `backend/mcp-atlassian`, `backend/RealtimeSTT`, `backend/RealtimeTTS`), the submodule PR is linked above and I bumped the pinned commit in this PR.
- [ ] If this changes config (`fastagent.config.yaml`, `.env`, `docker-compose.yaml`), I updated `fastagent.secrets.yaml.example` / `SELF_HOSTING.md` accordingly.

## Risk / rollout notes

<!--
Anything reviewers and ops should watch out for:
- Migrations or one-time setup steps needed on deploy?
- Backwards-compat concerns?
- Feature flag / gradual rollout plan?
- Known follow-ups deferred to a later PR?
-->

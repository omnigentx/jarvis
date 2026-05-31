# Skill licensing policy

Skills live in `backend/.fast-agent/skills/<skill-name>/`. Each is either
**in-house** (authored in this repository) or **vendored** (copied from an
upstream Apache-2.0 source). The license of each skill is decided by the
presence and content of its `LICENSE.txt`:

| Status                                 | License        | Where to find it             |
| -------------------------------------- | -------------- | ---------------------------- |
| `LICENSE.txt` present, Apache 2.0      | Apache 2.0     | Sibling `LICENSE.txt`        |
| `LICENSE.txt` absent (in-house)        | MIT (root)     | `/LICENSE` at the repo root  |
| `LICENSE.txt` absent (documented exc.) | See note below | This file                    |

The default for **any skill without a sibling `LICENSE.txt`** is the
repo's root MIT LICENSE. This rule covers the ~54 in-house skills
(e.g. `terminal-execution`, `jarvis-infra`, `music-playback`,
`personal-assistant`, …) and stays stable regardless of which skills are
added later.

## Apache 2.0 — vendored from upstream

Twelve skills vendored from Anthropic ship their own `LICENSE.txt`:

```
algorithmic-art        brand-guidelines       canvas-design
claude-api             frontend-design        internal-comms
mcp-builder            skill-creator          slack-gif-creator
theme-factory          web-artifacts-builder  webapp-testing
```

Source: <https://github.com/anthropics/skills>. Vendored as exact copies
in commit `dd410c4` — see [`NOTICE`](NOTICE) for attribution.

## Documented exceptions

### `doc-coauthoring`

`backend/.fast-agent/skills/doc-coauthoring/` is vendored from Anthropic
but ships **without** a `LICENSE.txt` because the upstream copy did not
include one at vendor time. Anthropic publishes their skills under
Apache 2.0, so `doc-coauthoring` is treated as Apache 2.0 here as well
— the missing `LICENSE.txt` is mirrored from upstream rather than a
licensing decision on our side. If the upstream skill later adds a
`LICENSE.txt`, re-vendor and the default Apache-2.0 path applies.

## What about contributing a new skill?

Contributors authoring a new skill **in this repository** do NOT need to
drop a `LICENSE.txt` into the skill directory — the root MIT LICENSE
covers it automatically by the rule above.

If you VENDOR a third-party skill, please:

1. Add its upstream `LICENSE` / `LICENSE.txt` next to the skill's
   `SKILL.md` so the per-directory rule keeps working.
2. Update [`NOTICE`](NOTICE) with the source URL and (if Apache-2.0)
   the attribution.
3. If you modify any vendored file, mark it with a "Modified from
   upstream" header so Apache 2.0 §4(b) is satisfied.

---
name: kaizen
description: "RESERVED — Continuous improvement methodology. Apply small iterative improvements, error-proof designs, follow established patterns, avoid over-engineering."
# STATUS: Reserved for cross-role use. Can be added to any role that needs quality discipline.
---

# Kaizen: Continuous Improvement

Adapted from [NeoLabHQ/context-engineering-kit](https://github.com/NeoLabHQ/context-engineering-kit/tree/master/plugins/kaizen/skills/kaizen).

> **⚠️ RESERVED:** Available for cross-role use when needed.

## Core Philosophy

Small improvements, continuously. Error-proof by design. Follow what works. Build only what's needed.

## The Four Pillars

### 1. Continuous Improvement
- Make smallest viable change that improves quality
- One improvement at a time — verify before next
- Always leave code better than you found it
- Iterative refinement: make it work → make it clear → make it efficient

### 2. Error-Proofing (Poka-Yoke)
- Make invalid states unrepresentable (use types)
- Validate at system boundaries, safe everywhere else
- Early returns prevent deep nesting
- Fail at startup, not in production

### 3. Standardized Work
- Follow existing codebase patterns (consistency over cleverness)
- New pattern only if significantly better
- Automate standards: linters, type checks, tests, CI/CD
- Document "why", not "what", in comments

### 4. Just-In-Time (YAGNI)
- Build for current requirements, not imaginary futures
- **Current need → Simple solution**
- Abstract only when pattern proven across 3+ cases
- Optimize based on measurement, not assumptions

## Quick Decision Guide

| Question | Action |
|---|---|
| Is this needed NOW? | If no → don't build it |
| Does a library solve this? | If yes → use it |
| Can I explain in one sentence? | If no → simplify |
| Is there a pattern for 3+ cases? | If yes → abstract |
| Have I measured the bottleneck? | If no → don't optimize |

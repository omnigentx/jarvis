---
name: architecture
description: >
  Architecture decision framework. Use when an SA needs to analyse
  requirements, evaluate trade-offs, write an ADR, or pick design
  patterns.
---

# ARCHITECTURE DECISION FRAMEWORK

## Core principle
> "Simplicity is the ultimate sophistication."
> Start simple. Add complexity ONLY when justified.

## Decision tree

```
What needs to be decided?
├── Pick a technology → Trade-off analysis
├── Component design → Separation of Concerns
├── Database design → Normalise vs denormalise
├── API design → REST vs WebSocket vs gRPC
└── Scaling → Vertical vs horizontal
```

## ADR template (Architecture Decision Record)

```markdown
# ADR-[N]: [Title]
## Status: [Proposed | Accepted | Deprecated]
## Context: the problem to solve
## Decision: what we chose
## Consequences:
- ✅ Pros: ...
- ❌ Cons: ...
## Alternatives considered:
1. Option A: ... (rejected because ...)
2. Option B: ... (rejected because ...)
```

## Design patterns (when to use)

| Pattern | When | Example in Jarvis |
|---------|------|-------------------|
| MCP (Model Context Protocol) | Tool integration | All tools |
| Agent pattern | Task delegation | Root → sub-agents |
| Event-driven | Async processing | Crawl jobs, TTS |
| Repository | Data access | History, stories |

## Pre-finalisation checklist
- [ ] Requirements are clear
- [ ] Trade-off analysis documented
- [ ] Considered simpler alternatives
- [ ] ADR written for each significant decision

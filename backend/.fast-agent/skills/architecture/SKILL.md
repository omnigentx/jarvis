---
name: architecture
description: >
  Framework ra quyết định kiến trúc. Dùng khi SA cần phân tích requirements,
  đánh giá trade-offs, viết ADR, hoặc chọn design patterns.
---

# ARCHITECTURE DECISION FRAMEWORK

## Nguyên tắc cốt lõi
> "Simplicity is the ultimate sophistication."
> Bắt đầu đơn giản. Thêm complexity CHỈ KHI cần thiết.

## Decision Tree

```
Cần quyết định gì?
├── Chọn technology → Trade-off analysis
├── Thiết kế component → Separation of Concerns
├── Database design → Normalize vs Denormalize
├── API design → REST vs WebSocket vs gRPC
└── Scaling → Vertical vs Horizontal
```

## ADR Template (Architecture Decision Record)

```markdown
# ADR-[N]: [Tiêu đề]
## Status: [Proposed | Accepted | Deprecated]
## Context: Vấn đề cần giải quyết
## Decision: Quyết định gì
## Consequences:
- ✅ Pros: ...
- ❌ Cons: ...
## Alternatives Considered:
1. Option A: ... (rejected vì ...)
2. Option B: ... (rejected vì ...)
```

## Design Patterns (khi nào dùng)

| Pattern | Khi nào | Ví dụ trong Jarvis |
|---------|---------|-------------------|
| MCP (Model Context Protocol) | Tool integration | Tất cả tools |
| Agent pattern | Task delegation | Root → Sub-agents |
| Event-driven | Async processing | Crawl jobs, TTS |
| Repository | Data access | History, stories |

## Checklist trước khi finalize
- [ ] Requirements đã rõ ràng
- [ ] Trade-off analysis documented
- [ ] Đã xem xét alternatives đơn giản hơn
- [ ] ADR written cho mỗi quyết định quan trọng

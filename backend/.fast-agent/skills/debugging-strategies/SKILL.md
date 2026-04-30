---
name: debugging-strategies
description: >
  Systematic debugging framework. Dùng khi Dev cần tìm root cause của bug,
  debug performance issues, hoặc phân tích lỗi production.
---

# SYSTEMATIC DEBUGGING

## 4-Phase Process

### Phase 1: REPRODUCE
- Xác nhận bug tồn tại
- Ghi lại exact steps to reproduce
- Capture: error message, stack trace, logs

### Phase 2: HYPOTHESIZE
- Đọc error message kỹ — thường chứa clue
- Liệt kê 2-3 hypotheses có khả năng nhất
- Xếp hạng theo likelihood

### Phase 3: TEST
- Test hypothesis có likelihood cao nhất TRƯỚC
- Binary search: chia code thành 2 nửa, test từng nửa
- Thêm logging tạm ở suspect points
- **KHÔNG** sửa code trước khi hiểu root cause

### Phase 4: FIX & VERIFY
- Sửa root cause, KHÔNG sửa symptom
- Viết test reproduce bug TRƯỚC khi fix
- Verify fix: test case phải PASS
- Xóa logging tạm

## Decision Tree

```
Bug loại gì?
├── Runtime error → Đọc stack trace, tìm line gây lỗi
├── Logic error → Thêm print/log tại input/output
├── Performance → Profile: đo thời gian từng phase
├── Intermittent → Tìm race condition, check timing
└── Khó reproduce → Thêm defensive logging
```

## ❌ Anti-patterns
- Đoán mò → sửa random code → HY VỌNG nó hoạt động
- Sửa symptom → không fix root cause → bug quay lại
- Không viết test → same bug tái phát sau refactor

## ✅ Chuẩn
- Reproduce → Hypothesis → Test → Fix → Verify → Commit with test

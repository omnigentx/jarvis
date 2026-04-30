---
name: api-design
description: >
  Thiết kế API contracts và schemas. Dùng khi BA cần định nghĩa interfaces
  giữa frontend-backend hoặc giữa các services.
---

# API DESIGN

## Quy trình

1. **Xác định use cases** từ BRD
2. **Chọn pattern**: REST / WebSocket / gRPC
3. **Định nghĩa endpoints**
4. **Viết schema** (request/response types)
5. **Document** error codes và edge cases

## REST API Template

```markdown
### [METHOD] /api/v1/resource

**Mô tả**: Làm gì

**Request**:
- Headers: `Authorization: Bearer <token>`
- Body:
  ```json
  { "field": "type", "description": "..." }
  ```

**Response 200**:
```json
{ "data": {...}, "message": "success" }
```

**Error Codes**:
| Code | Meaning |
|------|---------|
| 400  | Invalid request |
| 401  | Unauthorized |
| 404  | Not found |
```

## Quy tắc
- Endpoints dùng danh từ số nhiều (`/users` không phải `/user`)
- Versioning trong URL (`/api/v1/`)
- Luôn có error response format thống nhất
- Pagination cho list endpoints (`?page=1&limit=20`)

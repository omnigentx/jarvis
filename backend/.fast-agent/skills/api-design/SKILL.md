---
name: api-design
description: >
  Design API contracts and schemas. Use when a BA needs to define
  interfaces between frontend and backend, or between services.
---

# API DESIGN

## Workflow

1. **Identify use cases** from the BRD.
2. **Pick a pattern**: REST / WebSocket / gRPC.
3. **Define endpoints**.
4. **Write schemas** (request/response types).
5. **Document** error codes and edge cases.

## REST API template

```markdown
### [METHOD] /api/v1/resource

**Description**: what it does

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

**Error codes**:
| Code | Meaning |
|------|---------|
| 400  | Invalid request |
| 401  | Unauthorized |
| 404  | Not found |
```

## Rules
- Endpoint paths use plural nouns (`/users`, not `/user`).
- Versioning lives in the URL (`/api/v1/`).
- Always use a single, consistent error-response shape.
- Paginate list endpoints (`?page=1&limit=20`).

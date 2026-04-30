---
name: api-testing
description: >
  Backend API testing with pytest, httpx, and FastAPI TestClient. Covers endpoint
  verification, contract testing, mock patterns, and error scenario validation.
  Use when QE needs to test REST APIs, verify response schemas, or mock external services.
---

# API Testing

Test backend APIs using pytest + httpx (integration) or FastAPI TestClient (unit). Write tests to `/tmp`, execute via `python`.

## Decision Tree

```
API to test → Is it a FastAPI app?
    ├─ Yes → Use TestClient (no server needed)
    │         from fastapi.testclient import TestClient
    │
    └─ No → Use httpx (server must be running)
             import httpx
```

## FastAPI TestClient (Unit Tests)

```python
# /tmp/test_api.py
import pytest
from fastapi.testclient import TestClient
from server import app  # Import your FastAPI app

client = TestClient(app)

def test_health():
    r = client.get("/health")
    assert r.status_code == 200

def test_create_item():
    r = client.post("/items", json={"name": "test", "price": 9.99})
    assert r.status_code == 201
    data = r.json()
    assert data["name"] == "test"
    assert "id" in data

def test_auth_required():
    r = client.get("/protected")
    assert r.status_code == 401

def test_with_auth():
    r = client.get("/protected", headers={"Authorization": "Bearer test-token"})
    assert r.status_code == 200
```

## httpx (Integration Tests)

```python
# /tmp/test_integration.py
import httpx
import pytest

BASE_URL = "http://localhost:8000"  # Adjust to target

def test_endpoint_responds():
    r = httpx.get(f"{BASE_URL}/health")
    assert r.status_code == 200

def test_crud_flow():
    # Create
    r = httpx.post(f"{BASE_URL}/items", json={"name": "test"})
    assert r.status_code == 201
    item_id = r.json()["id"]

    # Read
    r = httpx.get(f"{BASE_URL}/items/{item_id}")
    assert r.status_code == 200

    # Delete
    r = httpx.delete(f"{BASE_URL}/items/{item_id}")
    assert r.status_code == 204
```

## Execution

```bash
# Run all tests
cd <project_dir> && python -m pytest /tmp/test_api.py -v

# Run with coverage
cd <project_dir> && python -m pytest /tmp/test_api.py --cov=. --cov-report=term
```

## Testing Patterns

| Pattern | When | Example |
|---------|------|---------|
| Happy path | Always first | `POST /items` → 201 |
| Error scenarios | Required | Invalid input → 422, Not found → 404 |
| Auth flows | If protected | No token → 401, Bad token → 403 |
| Edge cases | Important | Empty body, large payload, special chars |
| Contract validation | API changes | Verify response schema matches spec |

## Response Schema Validation

```python
def test_response_schema():
    r = client.get("/items/1")
    data = r.json()
    # Verify required fields exist
    assert "id" in data
    assert "name" in data
    assert isinstance(data["id"], int)
    assert isinstance(data["name"], str)
```

## Mocking External Dependencies

```python
from unittest.mock import patch, AsyncMock

def test_with_mocked_service():
    with patch("services.external_api.fetch_data", new_callable=AsyncMock) as mock:
        mock.return_value = {"status": "ok"}
        r = client.get("/dashboard")
        assert r.status_code == 200
        mock.assert_called_once()
```

## Anti-Patterns

| ❌ Don't | ✅ Do |
|----------|-------|
| Test against production | Use TestClient or local server |
| Hardcode auth tokens | Use fixtures or env vars |
| Skip error scenarios | Test 4xx/5xx responses explicitly |
| Ignore response body | Validate schema + data |

---
name: python-patterns
description: >
  Python best practices and patterns. Use when a Dev writes Python code
  for the Jarvis backend: async/await, typing, pytest, FastAPI patterns.
---

# PYTHON PATTERNS FOR JARVIS

## Async / await
```python
# ✅ Correct: use async for I/O operations
async def fetch_data(url: str) -> dict:
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        return response.json()

# ❌ Wrong: blocking I/O inside an async context
def fetch_data(url: str) -> dict:
    return requests.get(url).json()  # BLOCKS the event loop!
```

## Type hints
```python
# ✅ Always use type hints
from typing import Optional
from pydantic import BaseModel

class AgentConfig(BaseModel):
    name: str
    instruction: str
    skills: list[str] = []
    model: Optional[str] = None
```

## FastAPI Patterns
```python
# Router pattern
from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])

@router.get("/{agent_id}")
async def get_agent(agent_id: str) -> AgentResponse:
    agent = await agent_service.get(agent_id)
    if not agent:
        raise HTTPException(404, f"Agent {agent_id} not found")
    return agent
```

## Pytest
```python
# ✅ Fixture-based, descriptive names
import pytest

@pytest.fixture
def sample_skill():
    return {"name": "test", "description": "Test skill"}

def test_skill_loads_correct_description(sample_skill):
    assert sample_skill["description"] == "Test skill"

# ✅ Parametrize for multiple cases
@pytest.mark.parametrize("input,expected", [
    ("hello", "HELLO"),
    ("", ""),
])
def test_uppercase(input, expected):
    assert input.upper() == expected
```

## Error Handling
```python
# ✅ Specific exceptions, logging
import logging
logger = logging.getLogger(__name__)

try:
    result = await risky_operation()
except SpecificError as e:
    logger.error(f"Operation failed: {e}")
    raise HTTPException(500, str(e))
```

"""Factory team-template yaml REST API.

Endpoints (all gated by ``verify_api_key``):

  GET  /api/team-templates                → list yaml factory files
  GET  /api/team-templates/{name}         → read raw + parsed yaml
  PUT  /api/team-templates/{name}         → write yaml (validates + .bak)

Pairs with the running-team API at ``/api/team-sessions/{id}/template/*`` —
see ``routes/team_template.py``. Factory edits do NOT touch running teams;
drift is surfaced through the running-team ``yaml-diff`` endpoint and the
user clicks Reload-from-yaml manually.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.auth import verify_api_key
from services import team_template_factory_service as svc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/team-templates", tags=["team-template-factory"])


class TemplatePutBody(BaseModel):
    content: str = Field(..., max_length=svc.MAX_BYTES)


@router.get("", dependencies=[Depends(verify_api_key)])
async def list_templates():
    """Return every yaml file in the factory directory."""
    return {"templates": svc.list_factory_templates()}


@router.get("/{name}", dependencies=[Depends(verify_api_key)])
async def read_template(name: str):
    try:
        return svc.read_factory_template(name)
    except svc.NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except svc.PathTraversalError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except svc.FactoryTemplateError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.put("/{name}", dependencies=[Depends(verify_api_key)])
async def write_template(name: str, body: TemplatePutBody):
    try:
        return svc.write_factory_template(name, body.content)
    except svc.ValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail={"message": str(exc), "name": name},
        )
    except svc.PathTraversalError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except svc.FactoryTemplateError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

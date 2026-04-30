"""
Token Usage metrics endpoint.

GET /api/metrics/tokens?period=24h|7d|30d|all&agent=<name>
Returns aggregate metrics + per-agent breakdown from SQLite.

Naming convention: all cache fields are unified as `cached_tokens`
(= cache_hit_tokens + cache_read_tokens from DB).
"""

import time
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func

from core.auth import verify_api_key
from core.database import get_db, TokenUsageRecord

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/metrics", tags=["metrics"])

# Period → seconds mapping
PERIOD_SECONDS = {
    "1h": 3600,
    "24h": 86400,
    "7d": 604800,
    "30d": 2592000,
    "all": 0,
}


@router.get("/tokens", dependencies=[Depends(verify_api_key)])
async def get_token_metrics(
    period: str = Query("24h", description="Time period: 1h, 24h, 7d, 30d, all"),
    agent: str = Query(None, description="Filter by agent name"),
):
    """Aggregate token usage metrics with per-agent breakdown.
    
    Returns:
      - totals: aggregate input/output/cached/reasoning/cost
      - agents: per-agent breakdown sorted by total_tokens desc
      - models: per-model breakdown sorted by total_tokens desc
    """
    db = next(get_db())
    try:
        # Build base query with period filter
        now = time.time()
        seconds = PERIOD_SECONDS.get(period, 86400)
        
        query = db.query(TokenUsageRecord)
        if seconds > 0:
            cutoff = now - seconds
            query = query.filter(TokenUsageRecord.created_at >= cutoff)
        
        if agent:
            query = query.filter(TokenUsageRecord.agent_name == agent)
        
        records = query.all()
        
        # Aggregate totals
        totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cached_tokens": 0,
            "reasoning_tokens": 0,
            "est_cost": 0.0,
            "llm_calls": len(records),
        }
        
        # Per-agent breakdown
        agent_map: dict[str, dict] = {}
        # Per-model breakdown
        model_map: dict[str, dict] = {}
        
        for r in records:
            cached = (r.cache_hit_tokens or 0) + (r.cache_read_tokens or 0)
            
            totals["input_tokens"] += r.input_tokens or 0
            totals["output_tokens"] += r.output_tokens or 0
            totals["total_tokens"] += r.total_tokens or 0
            totals["cached_tokens"] += cached
            totals["reasoning_tokens"] += r.reasoning_tokens or 0
            totals["est_cost"] += r.est_cost or 0.0
            
            # Agent breakdown
            aname = r.agent_name or "unknown"
            if aname not in agent_map:
                agent_map[aname] = {
                    "agent_name": aname,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "cached_tokens": 0,
                    "reasoning_tokens": 0,
                    "est_cost": 0.0,
                    "llm_calls": 0,
                }
            ag = agent_map[aname]
            ag["input_tokens"] += r.input_tokens or 0
            ag["output_tokens"] += r.output_tokens or 0
            ag["total_tokens"] += r.total_tokens or 0
            ag["cached_tokens"] += cached
            ag["reasoning_tokens"] += r.reasoning_tokens or 0
            ag["est_cost"] += r.est_cost or 0.0
            ag["llm_calls"] += 1
            
            # Model breakdown
            mname = r.model or "unknown"
            if mname not in model_map:
                model_map[mname] = {
                    "model": mname,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "cached_tokens": 0,
                    "est_cost": 0.0,
                    "llm_calls": 0,
                }
            md = model_map[mname]
            md["input_tokens"] += r.input_tokens or 0
            md["output_tokens"] += r.output_tokens or 0
            md["total_tokens"] += r.total_tokens or 0
            md["cached_tokens"] += cached
            md["est_cost"] += r.est_cost or 0.0
            md["llm_calls"] += 1
        
        # Sort by total_tokens desc
        agents_list = sorted(agent_map.values(), key=lambda x: x["total_tokens"], reverse=True)
        models_list = sorted(model_map.values(), key=lambda x: x["total_tokens"], reverse=True)
        
        # Round cost
        totals["est_cost"] = round(totals["est_cost"], 6)
        for a in agents_list:
            a["est_cost"] = round(a["est_cost"], 6)
        for m in models_list:
            m["est_cost"] = round(m["est_cost"], 6)
        
        return {
            "period": period,
            "totals": totals,
            "agents": agents_list,
            "models": models_list,
        }
    finally:
        db.close()

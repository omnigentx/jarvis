"""
Pricing engine — loads config/model_pricing.yaml and computes estimated cost
for a single LLM call based on token counts.
"""

import re
import logging
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config" / "model_pricing.yaml"
_pricing_rules: list[dict] = []


def _load_pricing():
    """Load (or reload) pricing rules from YAML. Called once at module import."""
    global _pricing_rules
    try:
        if _CONFIG_PATH.exists():
            with open(_CONFIG_PATH) as f:
                data = yaml.safe_load(f)
            _pricing_rules = data.get("models", [])
            logger.info(f"[PRICING] Loaded {len(_pricing_rules)} pricing rules")
        else:
            logger.warning(f"[PRICING] Config not found at {_CONFIG_PATH}, using empty rules")
            _pricing_rules = []
    except Exception as e:
        logger.error(f"[PRICING] Failed to load pricing config: {e}")
        _pricing_rules = []


# Load at import time
_load_pricing()


def reload_pricing():
    """Force-reload pricing config (e.g. after file update)."""
    _load_pricing()


def get_model_pricing(model_name: str) -> Optional[dict]:
    """Find the first matching pricing rule for a model name.
    
    Returns dict with keys: input, output, cached_input (per 1M tokens in USD)
    or None if no rules loaded.
    """
    if not model_name:
        return None
    
    for rule in _pricing_rules:
        pattern = rule.get("pattern", "")
        try:
            if re.search(pattern, model_name, re.IGNORECASE):
                return {
                    "input": rule.get("input", 0),
                    "output": rule.get("output", 0),
                    "cached_input": rule.get("cached_input", 0),
                }
        except re.error:
            continue
    
    return None


def estimate_cost(
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    """Calculate estimated cost in USD for a single LLM call.
    
    Cache-read tokens are subtracted from input_tokens for pricing:
    - cache_read_tokens are priced at cached_input rate
    - remaining input tokens are priced at full input rate
    - output tokens are priced at output rate
    
    Returns cost in USD (float, e.g. 0.0025 = $0.0025).
    """
    pricing = get_model_pricing(model)
    if not pricing:
        return 0.0
    
    # Cache-read tokens get the discounted rate
    full_input = max(0, input_tokens - cache_read_tokens)
    
    cost = (
        (full_input / 1_000_000) * pricing["input"]
        + (cache_read_tokens / 1_000_000) * pricing["cached_input"]
        + (output_tokens / 1_000_000) * pricing["output"]
    )
    
    return round(cost, 8)

---
name: finance
description: >
  Financial and market data lookup. Use when user asks about stock prices, gold prices,
  crypto/coin prices, exchange rates, or market analysis. Uses serpapi with gl=vn, hl=vi.
---

# FINANCIAL DATA LOOKUP

<prerequisite>
ALWAYS call `get_current_time` first to determine if data is current.
</prerequisite>

## Decision tree

```
What is the user asking?
├── Stock price (VNM, FPT, VCB...) → serpapi: "<ticker> stock price today"
├── Gold price                    → serpapi: "<region> gold price today" (use the user's locale)
├── Crypto (BTC, ETH...)          → serpapi: "<coin> price USD"
├── Exchange rate                 → serpapi: "<currency pair> exchange rate today"
├── Market analysis               → serpapi: "<index> today" + summarise
└── Unclear                       → Ask the user to be more specific
```

<rule>
1. Match the serpapi `gl`/`hl` parameters to the user's locale (e.g. `gl=vn`, `hl=vi` when the user is Vietnamese; `gl=us`, `hl=en` for English).
2. Vietnam-listed stocks live on HOSE / HNX / UPCOM.
3. Crypto symbols are universal — query in English (BTC price, ETH price).
</rule>

## Output Format
- Present concisely: price, % change, timestamp of last update
- If multiple sources disagree → state the source explicitly

<violation>
- Guessing prices without searching → VIOLATION
- Returning stale prices without stating the timestamp → VIOLATION
</violation>

## ✅ Correct Examples
- "SJC gold today (Mar 15): Buy 12,150,000 — Sell 12,650,000 VND/tael (source: SJC)"
- "Bitcoin: $67,250, up 2.3% in the last 24 hours"

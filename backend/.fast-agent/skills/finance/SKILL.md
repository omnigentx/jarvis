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

## Decision Tree

```
What does the user ask?
├── Stock price (VNM, FPT, VCB...) → serpapi: "[ticker] stock price today"
├── Gold price → serpapi: "giá vàng SJC hôm nay" or "gold price 9999"
├── Crypto (BTC, ETH...) → serpapi: "[coin] price USD"
├── Exchange rate → serpapi: "tỷ giá USD VND hôm nay"
├── Market analysis → serpapi: "vnindex today" + summarize
└── Unclear → Ask user to be more specific
```

<rule>
1. ALWAYS use `gl=vn`, `hl=vi` for serpapi queries
2. Vietnam stocks: search on HOSE/HNX/UPCOM exchanges
3. Crypto: search in English (BTC price, ETH price)
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

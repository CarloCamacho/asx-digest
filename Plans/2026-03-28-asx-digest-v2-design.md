# ASX Digest v2 — Design Spec
**Date:** 2026-03-28
**Status:** Approved

---

## Goal

Evolve the ASX Digest from a stock-pick extractor into a daily market intelligence briefing. The digest should surface stocks with multi-source conviction AND give sector/macro context — so the reader understands *why* something is showing up, not just *that* it showed up.

Key requirements:
- Morning briefing (forward-looking) and evening wrap-up (recap) from a single script
- Dynamic sector discovery with mining as a permanent staple
- Sentiment = qualitative tone + buzz frequency
- All prices in AUD
- Picks include justification + sector alignment indicator

---

## Architecture

Two-pass Claude analysis per run, time-aware formatting, expanded data sources.

```
Fetch sources (concurrent)
  ├── Existing RSS feeds
  ├── New RSS feeds (ABC Business, Reuters AU, Mining Weekly)
  └── Market snapshot (Yahoo Finance v8 — sectors, commodities)
         ↓
Pass 1 — single Claude call
  Input: all item titles/descriptions + market snapshot
  Output: narrative, mining pulse, sector signals, buzz topics, sentiment
         ↓
Pass 2 — batched Claude calls (existing, lightly modified)
  Input: all items + Pass 1 sector context injected into prompt
  Output: individual picks + optional sector ETF picks
         ↓
Assemble email
  Morning template → forward-looking tone
  Evening template → wrap-up/recap tone
```

No changes to state management, deduplication, or email delivery.

---

## Data Sources

### New RSS feeds (config additions only)

| Key | Name | URL |
|-----|------|-----|
| `abc_business` | ABC Business | `https://www.abc.net.au/news/feed/51120/rss.xml` |
| `reuters_au` | Reuters Australia | `https://feeds.reuters.com/reuters/AUBusinessNews` |
| `mining_weekly` | Mining Weekly | `https://www.miningweekly.com/rss/rss` |

These use the existing RSS pipeline unchanged.

### Market snapshot (new `fetch_market_snapshot()` function)

Fetches via Yahoo Finance v8 chart API (same no-auth endpoint as Price Signals). One call per symbol, concurrent via existing `ThreadPoolExecutor`.

| Item | Symbol | Notes |
|------|--------|-------|
| ASX 200 | `^AXJO` | Overall market, AUD |
| Materials sector | `^AXMJ` | AUD |
| Energy sector | `^AXEJ` | AUD |
| Financials sector | `^AXFJ` | AUD |
| Gold | `GC=F` | USD → AUD |
| Copper | `HG=F` | USD → AUD |
| Oil (Brent) | `BZ=F` | USD → AUD |
| Wheat | `ZW=F` | USD → AUD |
| Lithium | `LITH.AX` | Global X Lithium ETF, already AUD |
| AUD/USD rate | `AUDUSD=X` | Fetched once, used for all conversions |

USD→AUD conversion: `price_aud = price_usd / audusd_rate`. Fetched at run time, not hardcoded.

The snapshot is passed as **context to Claude** — not as digest items to be analysed or deduplicated.

---

## Pass 1 — Market Intelligence

**Trigger:** Once per run, before Pass 2. Takes ~10-20 seconds (single Claude call).

**Run mode detection:**
```python
run_mode = "morning" if datetime.now().hour < 12 else "evening"
```

**Input to Claude:**
- Market snapshot formatted as a price table (all AUD)
- All fetched item titles + first 200 chars of description
- Run mode instruction

**Output schema (JSON, one object):**
```json
{
  "narrative": "2-3 sentences. Morning: what to watch today. Evening: what happened.",
  "mining_pulse": {
    "signal": "bullish|bearish|mixed|quiet",
    "reason": "1-2 sentences specific to mining/resources sector"
  },
  "sectors": [
    {
      "name": "Materials",
      "signal": "bullish|bearish|mixed",
      "reason": "specific reason with numbers where possible"
    }
  ],
  "commodities": [
    {
      "name": "Gold",
      "price_aud": 3241.50,
      "change_pct": 1.2,
      "note": "optional significance"
    }
  ],
  "buzz_topics": ["topic1", "topic2"],
  "sentiment": "bullish|cautiously bullish|mixed|cautiously bearish|bearish|neutral"
}
```

**Rules:**
- `mining_pulse` is always present, even on quiet days ("quiet — no notable moves")
- `sectors` only includes sectors with actual movement — no padding with flat sectors
- `commodities` shows all tracked commodities with their current AUD price and day change
- Evening narrative uses past tense ("today, the market..."); morning uses forward framing ("watch for...")

---

## Pass 2 — Pick Extraction (Changes Only)

### Sector context injection

Pass 1 output prepended to the existing pick extraction prompt:

```
MARKET CONTEXT:
Sentiment: [overall sentiment]
Mining pulse: [signal] — [reason]
Active sectors: [list]
Buzz topics: [list]

Now extract stock picks from the following items...
```

### Sector ETF picks (new signal type)

When multiple sources discuss a sector without naming specific stocks, Claude can emit a sector-level pick using the ETF as the ticker. New optional field added to pick schema:

```json
{
  "ticker": "XMJ",
  "company": "ASX Materials Sector ETF",
  "signal": "WATCH",
  "confidence": "MEDIUM",
  "sector_play": true,
  "summary": "Three sources pointing to materials sector strength without naming specific stocks..."
}
```

`sector_play: true` items render in the Sector Pulse section of the email, not in individual picks.

### Sector alignment field

Each pick gains a `sector_alignment` field populated post-analysis by comparing pick direction against Pass 1 sector signals:

- `confirms` — pick direction matches sector/market signal
- `diverges` — pick direction contradicts sector signal
- `neutral` — sector has no active signal today

---

## Email Template

### Structure (both morning and evening)

```
ASX [Morning Briefing | Evening Wrap] — [Day Date Year]
======================================================

📋 [TODAY'S MARKET OUTLOOK | WHAT HAPPENED TODAY]
[2-3 sentence narrative]
Overall sentiment: [sentiment]

⛏️ MINING PULSE
Signal: [Bullish | Bearish | Mixed | Quiet]
[1-2 sentence reason with commodity prices in AUD]

📊 SECTOR SIGNALS              (omitted if no active sectors)
  [Sector]  ↑↓  [Reason]

💬 BUZZ TOPICS                 (omitted if none)
  topic · topic · topic

──────────────────────────────────────────

🔥 HIGH CONVICTION (2+ sources)   (omitted if none)

  [TICKER] ([Company]) — [BUY|SELL|WATCH]
  Sources: [Source] · [Source]
  Sector alignment: [✅ Confirms | ⚠️ Diverges | —] [sector name + signal]

  Thesis: [2-3 sentences with specific facts, numbers, catalysts]
  Catalysts: [bullet list]
  Key quote: "[verbatim quote from source]"

📌 SINGLE SOURCE PICKS           (omitted if none)

  [TICKER] ([Company]) — [BUY|SELL|WATCH]
  Source: [Source]
  Sector alignment: [✅ Confirms | ⚠️ Diverges | —]
  [Thesis + catalysts + quote, same format as above]

──────────────────────────────────────────
Generated by ASXDigest v2. [N] stocks · [N] sources · [morning|evening] run.
```

### Morning vs evening differences

| Element | Morning | Evening |
|---------|---------|---------|
| Subject line | `ASX Morning Briefing — [date]` | `ASX Evening Wrap — [date]` |
| Narrative framing | Forward-looking ("watch for...") | Recap ("today, the market...") |
| Sector signals tone | "expected to..." | "closed up/down..." |
| Buzz topics label | "What to watch" | "What dominated today" |

### No-picks behaviour

The email sends **even with no picks** if there are notable sector signals or commodity moves. Currently the script skips the email entirely when no picks are found. This changes: the threshold for sending becomes "is there anything worth reporting?" — which includes a notable mining pulse or sector signal, even with zero individual picks.

---

## Config Changes

```json
"sources": {
  "abc_business": { "enabled": true, "url": "https://www.abc.net.au/news/feed/51120/rss.xml", "name": "ABC Business" },
  "reuters_au":   { "enabled": true, "url": "https://feeds.reuters.com/reuters/AUBusinessNews", "name": "Reuters Australia" },
  "mining_weekly":{ "enabled": true, "url": "https://www.miningweekly.com/rss/rss", "name": "Mining Weekly" }
},
"market_snapshot": {
  "enabled": true,
  "symbols": ["^AXJO","^AXMJ","^AXEJ","^AXFJ","GC=F","HG=F","BZ=F","ZW=F","LITH.AX","AUDUSD=X"]
}
```

---

## What Does Not Change

- State management and deduplication logic
- RSS parsing (`parse_rss`)
- Reddit fetching (`fetch_reddit`)
- Email delivery via AgentMail
- Cron schedule
- Existing pick schema fields (ticker, company, signal, confidence, summary, catalysts, risks, price_target, source_quotes)
- Batching strategy for Pass 2

---

## Open Questions / Assumptions

- Reuters AU RSS URL needs verification at implementation time — may have moved
- Mining Weekly RSS needs verification
- `LITH.AX` (Global X Lithium ETF) used as lithium proxy — no direct lithium spot price available on Yahoo Finance
- Iron ore price not directly available on Yahoo Finance; covered via Mining Weekly RSS + Materials sector index (`^AXMJ`) + RIO/BHP/FMG price action in existing Price Signals

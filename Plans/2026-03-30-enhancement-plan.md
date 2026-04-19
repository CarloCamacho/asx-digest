# ASXDigest Enhancement Plan — Delivering Hot, Usable Tips

**Date:** 2026-03-30
**Status:** Proposed

---

## Current Performance Baseline

From today's actual runs:
- **Morning (08:00):** 81 items → 2 picks → 0 high conviction. Runtime: 11 min.
- **Evening (17:00):** 101 items → 1 pick → 0 high conviction. Runtime: 14 min, 5 Claude timeouts.
- **Signal-to-noise ratio:** ~2% of processed items yield a pick.
- **High conviction rate:** 0% (no multi-source corroboration).

The script is doing a lot of work and producing almost nothing useful.

---

## Root Cause Analysis

### Why so few picks?

1. **YouTube sources are wasted.** RSS only captures video titles ("Market Wrap 28 March"). The actual stock picks are spoken IN the videos. Wealth Within videos explicitly name stocks and thesis — verified via transcript extraction:
   > "If you're asking where the next best energy play is on the ASX, then strap in because we reveal three stocks with massive opportunities..."

2. **Noise sources dominate.** ABC Business (20 items/run) and SMH Business (9-20 items/run) are general news. They almost never contain ASX stock picks. They add ~30 items/run and produce 0 picks.

3. **Pre-filter is too permissive.** `looks_like_stock_pick()` uses broad keywords like "buy", "signal", "target" which match general financial news, not stock-specific recommendations.

4. **Haiku misses nuanced picks.** At ~800 chars of description per item, Haiku sometimes fails to identify picks that require reading between the lines or understanding context.

### Why so slow?

5. **Serial batch processing.** 17-21 batches of 5 items, each requiring a full Claude CLI subprocess (cold start + inference). Each batch takes 30-60s.

6. **60-second timeout is too low.** 5 timeouts in one run = 5 batches of potential picks completely lost.

---

## Enhancement Roadmap (Priority Order)

### 🔥 P0: YouTube Transcript Extraction (HIGHEST IMPACT)

**Problem:** YouTube sources produce titles only. The actual stock picks are in the video audio.

**Solution:** Add `youtube-transcript-api` (already installed, verified working) to extract transcripts from YouTube video IDs. For each YouTube RSS entry, fetch the transcript and use it as the `description` field instead of the empty/minimal RSS description.

**Verified working today:**
- Wealth Within: 475 transcript segments, explicit stock picks extracted
- CommSecTV: 60 transcript segments, market wrap with specific sector/stock commentary
- Livewire Markets, Finer Market Points, ASR: untested but same mechanism

**Implementation:**
```python
from youtube_transcript_api import YouTubeTranscriptApi

def fetch_youtube_transcript(video_id, max_chars=2000):
    """Extract transcript text from a YouTube video."""
    try:
        ytt_api = YouTubeTranscriptApi()
        transcript = ytt_api.fetch(video_id, languages=['en-AU', 'en'])
        text = ' '.join([s.text for s in transcript.snippets])
        return text[:max_chars]
    except Exception:
        return None
```

Modify `parse_rss()` to detect YouTube video entries (link contains `youtube.com/watch`) and fetch transcript. Use transcript as description, capped at 2000 chars.

**Before:** YouTube title "Top 3 Energy Stocks on the ASX" → 0 picks
**After:** Full transcript with stock names, thesis, catalysts → 3+ picks per video

**Effort:** ~30 min. No API key needed. No Firecrawl credits consumed.

---

### 🔥 P1: Add CommSecTV as a Source

**Problem:** Missing the most popular ASX daily wrap channel (57.7K subscribers, daily posts).

**Solution:** Add CommSecTV YouTube RSS to config. Channel ID verified: `UC8Jc66lwfOT1CeXaEy3zEMw`

**Content type:** Daily "Market Close" and "Market Open" videos. CommSecTV does morning AND evening wraps, perfectly matching our 8am/5pm schedule. Content focuses on ASX-200 moves, sector analysis, and specific stock mentions with reasons.

**Transcript verified today:** "Market Close 30 Mar 26" — 60 segments, market-specific content about ASX performance, sector moves, and tensions affecting stocks.

**Note:** CommSecTV is market commentary, not explicit stock picks. Its value is:
1. Enriching the market intelligence pass (Pass 1) with professional analyst perspective
2. Providing sector signals that improve sector alignment scoring
3. Occasionally naming specific stocks that moved significantly

**Config addition:**
```json
"commsec_tv_youtube": {
  "enabled": true,
  "url": "https://www.youtube.com/feeds/videos.xml?channel_id=UC8Jc66lwfOT1CeXaEy3zEMw",
  "name": "CommSecTV (YouTube)"
}
```

**Before:** No CommSecTV data
**After:** Daily professional market wraps feeding both intelligence and pick extraction

**Effort:** 2 min config change (transcript extraction from P0 handles the rest).

---

### P2: Smarter Pre-Filter

**Problem:** `looks_like_stock_pick()` lets through ~100 items/run, but only 1-3 contain actual picks. Keywords like "buy", "target", "signal" match general news.

**Solution:** Two-tier filtering:

**Tier 1 — Source-based routing:**
- YouTube sources with transcripts → always include (transcripts are pick-rich)
- Price Signals → always include (already data-driven)
- Reddit r/ASX_Bets → always include (community picks)
- Stocks Down Under, Stockhead, Motley Fool, Market Herald, ShareCafe → apply keyword filter
- ABC Business, SMH Business, Mining.com → strict filter (require ticker pattern like 3-letter code + "ASX")

**Tier 2 — Enhanced keyword filter:**
```python
# Require at least TWO of these indicators, not just one
ASX_PICK_STRONG = [
    r'\b[A-Z]{3}\b',  # 3-letter ticker pattern
    r'asx[:\s]', r'\(asx', r'\.ax\b',  # ASX reference
]
ASX_PICK_WEAK = [
    "buy", "pick", "tip", "recommend", "target", "accumulate",
    "breakout", "entry", "upside", "price target",
]
```

Require a strong pattern + weak keyword, or two strong patterns. This eliminates articles like "RBA signals rate hold" (matches "signal" but no ticker).

**Before:** 81-101 items sent to Claude → 1-2 picks
**After:** ~20-30 high-quality items sent to Claude → more picks per item, faster runtime

**Effort:** ~20 min.

---

### P3: Parallel Batch Processing

**Problem:** 17-21 sequential Claude calls taking 30-60s each = 11-14 min total runtime.

**Solution:** Use `concurrent.futures.ThreadPoolExecutor` for batch analysis (already used for fetching). Process 3-4 batches simultaneously:

```python
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
    futures = []
    for i in range(0, len(filtered_items), batch_size):
        batch = filtered_items[i:i + batch_size]
        futures.append(executor.submit(analyze_batch, batch, claude, market_context=market_context))
    for future in concurrent.futures.as_completed(futures):
        all_picks.extend(future.result())
```

**Before:** 17 batches × 45s avg = ~13 min
**After:** 17 batches / 4 workers × 45s = ~3-4 min (with smarter pre-filter: ~8 batches / 4 = ~2 min)

**Effort:** ~15 min.

---

### P4: Increase Timeout and Add Retry

**Problem:** 5 Claude timeouts in a single run = 25 items lost. 60-second timeout is too short for complex batches.

**Solution:**
- Increase timeout from 60s to 120s
- Add single retry on timeout (with slightly smaller batch)
- Log which items were in timed-out batches for debugging

```python
except subprocess.TimeoutExpired:
    log.warning(f"Claude timeout on batch of {len(items)} items — retrying with split batch")
    # Retry each half separately
    mid = len(items) // 2
    return analyze_batch(items[:mid], claude, market_context=market_context) + \
           analyze_batch(items[mid:], claude, market_context=market_context)
```

**Before:** 5 timeouts → 25 items lost
**After:** Timeouts recovered via split retry, at most 5 items lost per hard failure

**Effort:** ~10 min.

---

### P5: Firecrawl Evaluation

**Verified findings:**

| Use Case | Firecrawl Useful? | Recommendation |
|-----------|------------------|----------------|
| YouTube transcripts | NO — use `youtube-transcript-api` (free, unlimited) | Skip |
| RSS feeds | NO — `urllib` + `xml.etree` works perfectly | Skip |
| HotCopper | NO — even Firecrawl only gets nav chrome, not discussions | Skip |
| ASX.com.au announcements | MAYBE — could bypass Incapsula protection | Low priority |
| Article full-text extraction | YES — could extract full Stockhead/Motley Fool articles beyond RSS snippet | Medium priority |

**Best use of Firecrawl:** Extract full article text from Stockhead, Motley Fool, and Market Herald links when the RSS description is truncated. Currently the script only gets ~1000 chars of description from RSS. Full articles often have detailed analysis with specific price targets and catalysts.

**Credit budget:** 624 credits available. At 1 credit/scrape, that's ~6 scrapes/run × 2 runs/day × ~50 days. Only scrape when the pre-filter flags an item as likely containing a pick.

**Implementation:** Add an optional `fetch_full_article(url)` function that uses Firecrawl CLI to get markdown content from article URLs, gated by credit budget and pre-filter confidence.

```python
def fetch_full_article(url, max_chars=3000):
    """Use Firecrawl to get full article text (1 credit per call)."""
    try:
        result = subprocess.run(
            ["firecrawl", "scrape", url, "--format", "markdown"],
            capture_output=True, text=True, timeout=30
        )
        return result.stdout[:max_chars] if result.returncode == 0 else None
    except Exception:
        return None
```

**Before:** RSS snippets ~1000 chars, missing detailed analysis
**After:** Full articles ~3000 chars with price targets and catalysts (for high-priority items)

**Effort:** ~30 min. Conservative credit usage.

---

### P6: Source Effectiveness Audit

Based on log analysis, here's which sources actually produce picks:

| Source | Items/Run | Picks Found | Pick Rate | Verdict |
|--------|-----------|-------------|-----------|---------|
| Wealth Within (YT) | 0-1 | 0 (title only) | 0% | **FIX** via P0 |
| Livewire Markets (YT) | 0-1 | 0 (title only) | 0% | **FIX** via P0 |
| Rask (YT) | 0-1 | 0 (title only) | 0% | **FIX** via P0 |
| Finer Market Points (YT) | 0-1 | 0 (title only) | 0% | **FIX** via P0 |
| ASR (YT) | 0-1 | 0 (title only) | 0% | **FIX** via P0 |
| Stocks Down Under | 5 | Occasional | ~5% | Keep |
| Stockhead | 20 | Occasional | ~3% | Keep, enhance with P5 |
| Market Herald | 12 | Rare | ~1% | Keep for Pass 1 intel |
| ShareCafe | 10 | Rare | ~2% | Keep |
| Motley Fool AU | 20 | Occasional | ~5% | Keep, enhance with P5 |
| ABC Business | 20 | 0 | 0% | **DEMOTE** to intel-only |
| SMH Business | 9-20 | 0 | 0% | **DEMOTE** to intel-only |
| Mining.com | 0-2 | 0 | 0% | Keep for mining pulse |
| r/ASX_Bets | 1-2 | Rare | ~5% | Keep |
| Price Signals | 0-5 | Data-driven | 100% | Keep |

**Demoting ABC/SMH:** Feed them to Pass 1 (market intelligence) but skip Pass 2 (pick extraction) for these sources. This saves 6-8 Claude calls per run.

**Before:** All sources treated equally → Claude wastes time on news articles
**After:** Pick sources → Pass 2, intel sources → Pass 1 only

**Effort:** ~15 min.

---

### P7: Better Prompt Engineering for Pick Extraction

**Problem:** The current prompt is decent but can be improved:

1. **Add explicit ASX ticker validation** — require the model to confirm the ticker exists on ASX
2. **Add source reliability weighting** — YouTube analyst channels should be treated as higher signal than Reddit
3. **Add negative examples** — show what NOT to extract (general market commentary, non-ASX stocks)

**Enhanced prompt additions:**
```
IMPORTANT:
- Only extract ASX-listed stocks (2-5 letter codes traded on the Australian Securities Exchange)
- Do NOT extract US stocks, ETFs traded on other exchanges, or non-stock recommendations
- General market commentary ("the ASX fell today") is NOT a stock pick
- A stock being mentioned in passing is NOT a recommendation — there must be a clear directional thesis
- Source reliability: YouTube analyst channels > specialist sites > general news > Reddit
```

**Before:** Haiku sometimes extracts non-ASX stocks or vague mentions
**After:** Tighter extraction with fewer false positives

**Effort:** ~10 min.

---

### P8: Model Selection — Selective Sonnet Upgrade

**Problem:** Haiku is fast but may miss nuanced picks, especially from longer transcript content.

**Solution:** Use Haiku for most batches but upgrade to Sonnet for:
1. YouTube transcript batches (longer, denser content requiring comprehension)
2. High-priority article batches (Firecrawl full-text from P5)

```python
model = "claude-sonnet-4-5-20250514" if any(
    item.get("has_transcript") or item.get("has_full_article")
    for item in batch
) else "claude-haiku-4-5-20251001"
```

**Before:** Haiku for everything → misses nuanced picks in long content
**After:** Sonnet for rich content, Haiku for short snippets → better picks where it matters

**Cost impact:** ~2-4 Sonnet calls per run instead of 0. Roughly 3x cost per Sonnet call. Modest total increase.

**Effort:** ~10 min.

---

### P9: Pick Quality Scoring

**Problem:** All picks are treated equally. A vague Reddit mention gets the same weight as a detailed analyst recommendation.

**Solution:** Post-extraction scoring:

```python
def score_pick(pick):
    score = 0
    # Source quality
    source_weights = {
        "Wealth Within (YouTube)": 3, "Livewire Markets (YouTube)": 3,
        "CommSecTV (YouTube)": 2, "Stocks Down Under": 2,
        "Stockhead": 2, "Motley Fool Australia": 2,
        "r/ASX_Bets": 1, "Price Signals": 2,
    }
    score += source_weights.get(pick["source"], 1)
    # Content quality
    if pick.get("price_target"): score += 2
    if len(pick.get("catalysts", [])) >= 2: score += 1
    if pick.get("source_quotes"): score += 1
    if pick["confidence"] == "HIGH": score += 2
    return score
```

Display score in email (e.g., quality stars: ★★★★☆). Helps recipients quickly identify the strongest picks.

**Before:** All picks look equally weighted
**After:** Best picks visually distinguished, worst filtered out below threshold

**Effort:** ~20 min.

---

### P10: State Cleanup and Weekend Handling

**Problem:** `state.json` grows indefinitely (currently tracking hundreds of seen IDs). Weekend runs fetch 0 items from most sources but still run the full pipeline.

**Solution:**
1. **State pruning:** Remove seen_ids older than `max_age_hours` on each run
2. **Weekend detection:** On Saturday/Sunday, skip sources that don't publish (ABC, SMH, Market Herald) and reduce batch frequency

```python
is_weekend = datetime.now().weekday() >= 5
weekend_skip_sources = {"ABC Business", "SMH Business", "The Market Herald", "ShareCafe"}
```

**Before:** state.json grows forever; weekends waste time on empty sources
**After:** State stays lean; weekends run faster

**Effort:** ~15 min.

---

### P11: Historical Pick Tracking

**Problem:** No way to know if previous picks were good or bad. Can't improve without feedback.

**Solution:** Log all picks to a CSV/JSONL file with the pick date, ticker, signal, price at pick time, and source. Periodically (weekly?) check current price vs pick price to calculate hit rate.

```python
# Append to picks_history.jsonl after each run
pick_record = {
    "date": datetime.now().isoformat(),
    "ticker": pick["ticker"],
    "signal": pick["signal"],
    "price_at_pick": pick.get("close"),  # from Price Signals data
    "source": pick["source"],
    "confidence": pick["confidence"],
}
```

A separate weekly script could then check which picks gained/lost value and email a "Pick Scorecard".

**Before:** No historical tracking, no accuracy measurement
**After:** Data to measure and improve pick quality over time

**Effort:** ~30 min for logging, ~2 hours for scorecard script.

---

## Implementation Priority Matrix

| Priority | Enhancement | Impact on Tips | Effort | Dependencies |
|----------|------------|----------------|--------|--------------|
| **P0** | YouTube transcript extraction | 🔥🔥🔥🔥🔥 | 30 min | `youtube-transcript-api` (installed) |
| **P1** | Add CommSecTV | 🔥🔥🔥 | 2 min | P0 |
| **P2** | Smarter pre-filter | 🔥🔥🔥 | 20 min | None |
| **P3** | Parallel batch processing | ⚡⚡⚡ (speed) | 15 min | None |
| **P4** | Timeout increase + retry | ⚡⚡ (reliability) | 10 min | None |
| **P5** | Firecrawl full articles | 🔥🔥 | 30 min | Firecrawl credits |
| **P6** | Source effectiveness routing | 🔥🔥 + ⚡⚡ | 15 min | None |
| **P7** | Better prompt engineering | 🔥🔥 | 10 min | None |
| **P8** | Selective Sonnet upgrade | 🔥🔥 | 10 min | None |
| **P9** | Pick quality scoring | 🔥 | 20 min | None |
| **P10** | State cleanup + weekends | ⚡ (maintenance) | 15 min | None |
| **P11** | Historical pick tracking | 📊 (long-term) | 2+ hours | None |

**Recommended implementation order:** P0 → P1 → P2+P3+P4 (parallel) → P6 → P7 → P5 → P8 → P9 → P10 → P11

**Expected outcome after P0-P4:**
- Pick count: 1-3/run → 8-15/run (YouTube transcripts are rich)
- High conviction: 0/run → 2-5/run (same stocks mentioned across YouTube + articles)
- Runtime: 11-14 min → 3-5 min
- Timeouts: 5/run → 0-1/run

---

## What NOT to Do

- **Don't enable HotCopper.** Tested with Firecrawl — even JS rendering only gets navigation, not discussion content. HotCopper ToS also prohibits scraping.
- **Don't use Firecrawl for RSS feeds.** urllib works fine. Firecrawl costs credits and adds latency.
- **Don't use Firecrawl for YouTube.** `youtube-transcript-api` is free and purpose-built.
- **Don't switch entirely to Sonnet.** 21 Sonnet calls per run is expensive. Selective upgrade (P8) is smarter.
- **Don't remove any existing working sources.** Demote low-value ones from pick extraction to intel-only.

# ASX Digest v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Evolve ASX Digest from a stock-pick extractor into a morning/evening market intelligence briefing with sector signals, commodity prices in AUD, and sentiment context.

**Architecture:** Two-pass Claude analysis per run — Pass 1 produces a market intelligence snapshot (narrative, mining pulse, sectors, commodities, buzz topics); Pass 2 is the existing pick extraction enriched with sector context. Time-aware run mode (morning/evening) controls framing. Email sends even with zero picks if sector signals are notable.

**Tech Stack:** Python 3, Yahoo Finance v8 chart API (no auth), existing Claude CLI subprocess, AgentMail. No new dependencies.

---

## File Map

| File | Changes |
|------|---------|
| `config.json` | Add 3 RSS sources + `market_snapshot` block |
| `asx_digest.py` | Add `fetch_market_snapshot()`, `run_intelligence_pass()`, `assign_sector_alignment()`, modify `analyze_batch()`, rewrite `format_email()`, update `main()` |

---

## Task 1: Verify and add new RSS sources to config

**Files:**
- Modify: `config.json`

- [ ] **Step 1: Verify the three RSS URLs resolve and return RSS/XML**

```bash
cd /Users/ianf/.claude/PAI/Tools/ASXDigest
python3 -c "
import urllib.request
urls = [
    ('abc_business', 'https://www.abc.net.au/news/feed/51120/rss.xml'),
    ('reuters_au', 'https://feeds.reuters.com/reuters/AUBusinessNews'),
    ('mining_weekly', 'https://www.miningweekly.com/rss/rss'),
]
headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36'}
for key, url in urls:
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as r:
            body = r.read(200).decode('utf-8', errors='replace')
            print(f'OK {key}: ct={r.headers.get(\"Content-Type\",\"?\")[:40]} preview={repr(body[:60])}')
    except Exception as e:
        print(f'FAIL {key}: {e}')
"
```

Expected: three OK lines with XML content-type or `<?xml` preview. If any FAIL, find a working alternative URL for that source before continuing (e.g. Reuters AU may have moved — try `https://feeds.reuters.com/reuters/businessNews`).

- [ ] **Step 2: Add working sources to config.json**

In `config.json`, add the three verified sources inside `"sources"` (before `"hotcopper"`):

```json
"abc_business": {
  "enabled": true,
  "url": "https://www.abc.net.au/news/feed/51120/rss.xml",
  "name": "ABC Business"
},
"reuters_au": {
  "enabled": true,
  "url": "https://feeds.reuters.com/reuters/AUBusinessNews",
  "name": "Reuters Australia"
},
"mining_weekly": {
  "enabled": true,
  "url": "https://www.miningweekly.com/rss/rss",
  "name": "Mining Weekly"
},
```

- [ ] **Step 3: Add market_snapshot block to config.json**

Add after the `"sources"` block (before `"thresholds"`):

```json
"market_snapshot": {
  "enabled": true,
  "symbols": ["^AXJO", "^AXMJ", "^AXEJ", "^AXFJ", "GC=F", "HG=F", "BZ=F", "ZW=F", "LITH.AX", "AUDUSD=X"]
},
```

- [ ] **Step 4: Verify config loads cleanly**

```bash
python3 -c "import json; c=json.load(open('config.json')); print('sources:', list(c['sources'].keys())); print('snapshot symbols:', c['market_snapshot']['symbols'])"
```

Expected output:
```
sources: ['wealth_within_youtube', 'livewire_markets_youtube', ..., 'abc_business', 'reuters_au', 'mining_weekly', 'hotcopper', ...]
snapshot symbols: ['^AXJO', '^AXMJ', '^AXEJ', '^AXFJ', 'GC=F', 'HG=F', 'BZ=F', 'ZW=F', 'LITH.AX', 'AUDUSD=X']
```

- [ ] **Step 5: Dry-run to confirm new sources fetch without errors**

```bash
python3 asx_digest.py --dry-run 2>&1 | grep -E "(Fetching|new items|WARNING|ERROR)"
```

Expected: `Fetching ABC Business...`, `Fetching Reuters Australia...`, `Fetching Mining Weekly...` each with an item count. No new WARNING lines for these sources.

---

## Task 2: Add `fetch_market_snapshot()`

**Files:**
- Modify: `asx_digest.py` — add after `fetch_price_signals()`, before `# ── AI Analysis`

- [ ] **Step 1: Add the function**

Insert the following function in `asx_digest.py` after `fetch_price_signals()` (around line 390, before the `# ── AI Analysis` section):

```python
# ── Market Snapshot ───────────────────────────────────────────────────────────
SNAPSHOT_SYMBOLS = {
    "^AXJO":    ("ASX 200",           "aud",  None),
    "^AXMJ":    ("Materials Sector",  "aud",  None),
    "^AXEJ":    ("Energy Sector",     "aud",  None),
    "^AXFJ":    ("Financials Sector", "aud",  None),
    "GC=F":     ("Gold",              "usd",  "oz"),
    "HG=F":     ("Copper",            "usd",  "lb"),
    "BZ=F":     ("Brent Crude Oil",   "usd",  "bbl"),
    "ZW=F":     ("Wheat",             "usd",  "bu"),
    "LITH.AX":  ("Lithium ETF",       "aud",  None),
    "AUDUSD=X": ("AUD/USD",           "rate", None),
}

COMMODITY_UNITS = {"oz": "A$/oz", "lb": "A$/lb", "bbl": "A$/bbl", "bu": "A$/bu"}


def fetch_market_snapshot(symbols):
    """Fetch price data for a list of Yahoo Finance symbols concurrently.
    Returns a dict keyed by symbol with price, change_pct, and AUD-converted values.
    """
    def fetch_one(symbol):
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?interval=1d&range=5d"
        raw = fetch_url(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "application/json",
        })
        if not raw:
            return symbol, None
        try:
            data = json.loads(raw)
            result = data.get("chart", {}).get("result", [None])[0]
            if not result:
                return symbol, None
            meta = result.get("meta", {})
            quotes = result.get("indicators", {}).get("quote", [{}])[0]
            closes = [c for c in (quotes.get("close") or []) if c is not None]
            current = meta.get("regularMarketPrice") or (closes[-1] if closes else None)
            prev = closes[-2] if len(closes) >= 2 else None
            change_pct = ((current - prev) / prev * 100) if (current and prev) else None
            return symbol, {"price": current, "change_pct": change_pct}
        except Exception as e:
            log.debug(f"Snapshot fetch error for {symbol}: {e}")
            return symbol, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        raw_results = dict(executor.map(fetch_one, symbols))

    # Get AUD/USD rate for conversion
    audusd = None
    if "AUDUSD=X" in raw_results and raw_results["AUDUSD=X"]:
        audusd = raw_results["AUDUSD=X"]["price"]

    snapshot = {}
    for symbol, data in raw_results.items():
        if symbol == "AUDUSD=X":
            snapshot[symbol] = data
            continue
        if data is None:
            snapshot[symbol] = None
            continue
        name, currency, unit = SNAPSHOT_SYMBOLS.get(symbol, (symbol, "usd", None))
        price = data["price"]
        change_pct = data["change_pct"]
        if currency == "usd" and audusd and price:
            price_aud = price / audusd
        else:
            price_aud = price
        snapshot[symbol] = {
            "name": name,
            "price_aud": price_aud,
            "change_pct": change_pct,
            "unit": unit,
            "currency": currency,
        }
    return snapshot


def format_snapshot_for_prompt(snapshot):
    """Format market snapshot as a compact text table for Claude."""
    lines = ["MARKET SNAPSHOT (all prices AUD):"]
    for symbol, data in snapshot.items():
        if symbol == "AUDUSD=X" or data is None:
            continue
        name = data.get("name", symbol)
        price = data.get("price_aud")
        chg = data.get("change_pct")
        unit = data.get("unit")
        unit_str = f" {COMMODITY_UNITS.get(unit, '')}" if unit else ""
        price_str = f"A${price:,.2f}{unit_str}" if price else "N/A"
        chg_str = f"{chg:+.2f}%" if chg is not None else "N/A"
        lines.append(f"  {name:<22} {price_str:<16} {chg_str}")
    # Add AUD/USD separately
    if "AUDUSD=X" in snapshot and snapshot["AUDUSD=X"]:
        rate = snapshot["AUDUSD=X"].get("price")
        lines.append(f"  {'AUD/USD':<22} {rate:.4f}" if rate else "  AUD/USD: N/A")
    return "\n".join(lines)
```

- [ ] **Step 2: Verify the function runs and returns data**

```bash
python3 -c "
import sys; sys.path.insert(0, '.')
# Quick import trick — run just the snapshot functions
exec(open('asx_digest.py').read().split('# ── AI Analysis')[0])
symbols = ['^AXJO', '^AXMJ', 'GC=F', 'AUDUSD=X', 'LITH.AX']
snap = fetch_market_snapshot(symbols)
print(format_snapshot_for_prompt(snap))
"
```

Expected: a table with prices like:
```
MARKET SNAPSHOT (all prices AUD):
  ASX 200                A$7,842.30       -0.31%
  Materials Sector       A$16,234.10      +0.12%
  Gold                   A$4,821.50/oz    +0.54%
  Lithium ETF            A$9.23           -0.80%
  AUD/USD                0.6280
```
(Exact values will vary. On weekends, change_pct may show 0.00% — that's correct.)

---

## Task 3: Add `INTELLIGENCE_PROMPT` and `run_intelligence_pass()`

**Files:**
- Modify: `asx_digest.py` — add after the `EXTRACT_PROMPT` constant and before `ASX_PICK_PATTERNS`

- [ ] **Step 1: Add the intelligence prompt constant**

Insert after `EXTRACT_PROMPT` (after line ~399, before `# Pre-filter`):

```python
INTELLIGENCE_PROMPT = """You are a senior Australian equity market analyst. Analyse the MARKET SNAPSHOT and NEWS ITEMS below and produce a structured intelligence briefing.

Run mode: {run_mode}
- If "morning": frame insights as forward-looking (what to watch, what may move today)
- If "evening": frame insights as a recap (what happened, what moved today)

{snapshot}

NEWS ITEMS (title + excerpt):
{items}

Output exactly ONE JSON object on a single line with this schema:
{{"narrative":"2-3 sentences. Morning: outlook/watchpoints. Evening: what happened today.","mining_pulse":{{"signal":"bullish|bearish|mixed|quiet","reason":"1-2 sentences on mining/resources sector with specific data points"}},"sectors":[{{"name":"sector name","signal":"bullish|bearish|mixed","reason":"specific reason with numbers"}}],"commodities":[{{"name":"commodity name","price_aud":0.00,"change_pct":0.00,"note":"optional 1-line significance"}}],"buzz_topics":["topic1","topic2"],"sentiment":"bullish|cautiously bullish|mixed|cautiously bearish|bearish|neutral"}}

Rules:
- mining_pulse MUST always be present even if quiet (signal: "quiet", reason: "No notable moves in mining or resources today")
- sectors: only include sectors with meaningful movement — omit flat sectors entirely
- commodities: include ALL commodities from the snapshot with their AUD prices and day change
- buzz_topics: 3-6 recurring themes or names appearing across multiple items
- Output ONLY the JSON line, nothing else"""
```

- [ ] **Step 2: Add the `run_intelligence_pass()` function**

Insert immediately after `INTELLIGENCE_PROMPT` (before `# Pre-filter`):

```python
def run_intelligence_pass(all_items, snapshot, run_mode, claude_path):
    """Pass 1: single Claude call producing market intelligence context.
    Returns a dict with narrative, mining_pulse, sectors, commodities, buzz_topics, sentiment.
    Returns a safe fallback dict on failure.
    """
    fallback = {
        "narrative": "",
        "mining_pulse": {"signal": "quiet", "reason": "Market intelligence unavailable for this run."},
        "sectors": [],
        "commodities": [],
        "buzz_topics": [],
        "sentiment": "neutral",
    }

    snapshot_text = format_snapshot_for_prompt(snapshot) if snapshot else "Snapshot unavailable."

    # Build condensed item list (title + first 150 chars of description)
    item_lines = []
    for i, item in enumerate(all_items[:60]):  # cap at 60 items for prompt length
        title = item.get("title", "")
        desc = (item.get("description", "") or "")[:150].replace("\n", " ")
        item_lines.append(f"[{i}] {title} — {desc}")
    items_text = "\n".join(item_lines)

    prompt = INTELLIGENCE_PROMPT.format(
        run_mode=run_mode,
        snapshot=snapshot_text,
        items=items_text,
    )

    try:
        result = subprocess.run(
            [claude_path, "--print", "--model", "claude-haiku-4-5-20251001"],
            input=prompt, capture_output=True, text=True, timeout=90
        )
        output = result.stdout.strip()
        # Find the JSON line
        for line in output.split("\n"):
            line = line.strip()
            if line.startswith("{") and '"narrative"' in line:
                try:
                    intel = json.loads(line)
                    # Ensure required fields present
                    intel.setdefault("narrative", "")
                    intel.setdefault("mining_pulse", fallback["mining_pulse"])
                    intel.setdefault("sectors", [])
                    intel.setdefault("commodities", [])
                    intel.setdefault("buzz_topics", [])
                    intel.setdefault("sentiment", "neutral")
                    return intel
                except json.JSONDecodeError:
                    pass
        log.warning("Intelligence pass: could not parse Claude output as JSON")
        return fallback
    except subprocess.TimeoutExpired:
        log.warning("Intelligence pass: Claude timeout")
        return fallback
    except Exception as e:
        log.warning(f"Intelligence pass error: {e}")
        return fallback
```

- [ ] **Step 3: Smoke test — call the function with fake data**

```bash
python3 -c "
import json, subprocess, sys
# patch subprocess to return fake JSON so we don't burn tokens
import unittest.mock as mock
fake_intel = json.dumps({
    'narrative': 'Test narrative.',
    'mining_pulse': {'signal': 'bullish', 'reason': 'Iron ore up.'},
    'sectors': [{'name': 'Materials', 'signal': 'bullish', 'reason': 'Strong China demand.'}],
    'commodities': [{'name': 'Gold', 'price_aud': 4800.0, 'change_pct': 0.5, 'note': ''}],
    'buzz_topics': ['iron ore', 'china'],
    'sentiment': 'cautiously bullish',
})
with mock.patch('subprocess.run') as m:
    m.return_value = mock.Mock(stdout=fake_intel, stderr='', returncode=0)
    exec(open('asx_digest.py').read())
    result = run_intelligence_pass(
        [{'title': 'Test', 'description': 'desc'}],
        {},
        'morning',
        '/Users/ianf/.local/bin/claude'
    )
    print('narrative:', result['narrative'])
    print('mining signal:', result['mining_pulse']['signal'])
    print('sectors:', result['sectors'])
    print('sentiment:', result['sentiment'])
"
```

Expected:
```
narrative: Test narrative.
mining signal: bullish
sectors: [{'name': 'Materials', 'signal': 'bullish', 'reason': 'Strong China demand.'}]
sentiment: cautiously bullish
```

---

## Task 4: Modify `analyze_batch()` to accept market context and handle `sector_play`

**Files:**
- Modify: `asx_digest.py` — `analyze_batch()` function signature and prompt construction

- [ ] **Step 1: Update `EXTRACT_PROMPT` to include a context placeholder**

Find this line in `EXTRACT_PROMPT`:
```python
EXTRACT_PROMPT = """You are an ASX stock analyst. Analyze the ITEMS below and extract any ASX stock picks, buy signals, or investment recommendations.
```

Replace the opening of the prompt to add context support:

```python
EXTRACT_PROMPT = """You are an ASX stock analyst. Analyze the ITEMS below and extract any ASX stock picks, buy signals, or investment recommendations.

{market_context}
```

Also add `sector_play` to the output schema description in the prompt. Find the existing `Fields:` block and add after `source_quotes`:

```
- sector_play: true if this is a broad sector call rather than a specific stock (optional, omit if false)
```

- [ ] **Step 2: Update `analyze_batch()` signature and prompt construction**

Find the existing `def analyze_batch(items, claude_path, batch_size=5):` function. Change its signature and prompt construction:

```python
def analyze_batch(items, claude_path, batch_size=5, market_context=""):
    """Analyze a batch of items in a single Claude call. Returns list of picks."""
    if not items:
        return []

    # Build numbered content block
    content_parts = []
    for i, item in enumerate(items):
        title = item.get("title", "")
        desc = item.get("description", "")[:800]
        content_parts.append(f"[{i}] {title}\n{desc}")
    content = "\n\n".join(content_parts)

    context_block = f"MARKET CONTEXT:\n{market_context}\n" if market_context else ""
    prompt = EXTRACT_PROMPT.format(
        market_context=context_block,
        content=content[:6000]
    )
```

Leave the rest of the function unchanged. The only changes are the signature (adding `market_context=""`) and the two lines building `context_block` and updating the `prompt` construction.

- [ ] **Step 3: Handle `sector_play` field in the picks parsing loop**

Inside `analyze_batch()`, in the picks parsing loop where `pick.setdefault(...)` calls are made, add:

```python
pick.setdefault("sector_play", False)
```

after the existing `pick.setdefault("source_quotes", [])` line.

- [ ] **Step 4: Verify analyze_batch still works with no context**

```bash
python3 -c "
import json, unittest.mock as mock
with mock.patch('subprocess.run') as m:
    m.return_value = mock.Mock(stdout='{\"ticker\":\"BHP\",\"company\":\"BHP Group\",\"signal\":\"BUY\",\"confidence\":\"HIGH\",\"item_index\":0,\"summary\":\"test\",\"catalysts\":[],\"risks\":[],\"price_target\":null,\"source_quotes\":[]}', stderr='', returncode=0)
    exec(open('asx_digest.py').read())
    picks = analyze_batch(
        [{'title': 'BHP looks great', 'description': 'strong buy', 'source': 'Test', 'link': ''}],
        '/Users/ianf/.local/bin/claude'
    )
    print('picks:', len(picks), picks[0]['ticker'] if picks else 'none')
    print('sector_play default:', picks[0].get('sector_play') if picks else 'n/a')
"
```

Expected:
```
picks: 1 BHP
sector_play default: False
```

---

## Task 5: Add `assign_sector_alignment()`

**Files:**
- Modify: `asx_digest.py` — add after `aggregate_picks()`

- [ ] **Step 1: Add the function**

Insert after `aggregate_picks()` (after the sort line, before `# ── Email Formatting`):

```python
def assign_sector_alignment(aggregated, intel):
    """Add sector_alignment field to each aggregated pick based on Pass 1 intel.
    Modifies aggregated in place.
    alignment values: 'confirms', 'diverges', 'neutral'
    """
    if not intel:
        for stock in aggregated:
            stock["sector_alignment"] = "neutral"
            stock["sector_alignment_label"] = ""
        return

    # Build a lookup: sector name (lower) -> signal
    sector_signals = {s["name"].lower(): s["signal"] for s in intel.get("sectors", [])}
    mining_signal = intel.get("mining_pulse", {}).get("signal", "quiet")

    # Sector keywords → sector name mapping
    mining_tickers = {"BHP", "RIO", "FMG", "MIN", "NST", "EVN", "NCM", "SFR", "OZL", "IGO", "PLS", "LTR"}
    energy_tickers = {"WDS", "STO", "BPT", "KAR", "VEA"}
    financials_tickers = {"CBA", "WBC", "NAB", "ANZ", "MQG", "QBE", "SUN", "IAG"}

    def get_sector_signal(ticker):
        if ticker in mining_tickers:
            return "Materials", mining_signal
        if ticker in energy_tickers:
            return "Energy", sector_signals.get("energy", "")
        if ticker in financials_tickers:
            return "Financials", sector_signals.get("financials", "")
        # Check active sectors
        for sname, ssig in sector_signals.items():
            return sname.title(), ssig
        return None, None

    for stock in aggregated:
        ticker = stock.get("ticker", "")
        stock_signal = stock.get("signal", "BUY")
        sector_name, sector_sig = get_sector_signal(ticker)

        if not sector_name or not sector_sig or sector_sig == "quiet":
            stock["sector_alignment"] = "neutral"
            stock["sector_alignment_label"] = "—"
        elif (stock_signal in ("BUY", "WATCH") and sector_sig == "bullish") or \
             (stock_signal == "SELL" and sector_sig == "bearish"):
            stock["sector_alignment"] = "confirms"
            stock["sector_alignment_label"] = f"✅ Confirms {sector_name} ({sector_sig})"
        else:
            stock["sector_alignment"] = "diverges"
            stock["sector_alignment_label"] = f"⚠️ Diverges from {sector_name} ({sector_sig})"
```

- [ ] **Step 2: Verify alignment logic**

```bash
python3 -c "
exec(open('asx_digest.py').read())
intel = {
    'mining_pulse': {'signal': 'bullish', 'reason': 'test'},
    'sectors': [{'name': 'Energy', 'signal': 'bearish', 'reason': 'oil down'}],
    'sentiment': 'mixed',
}
picks = [
    {'ticker': 'BHP', 'signal': 'BUY', 'company': 'BHP', 'sources': ['Test'], 'source_count': 1, 'high_conviction': False, 'mentions': [], 'summary': '', 'catalysts': [], 'risks': [], 'price_target': None, 'source_quotes': []},
    {'ticker': 'WDS', 'signal': 'BUY', 'company': 'Woodside', 'sources': ['Test'], 'source_count': 1, 'high_conviction': False, 'mentions': [], 'summary': '', 'catalysts': [], 'risks': [], 'price_target': None, 'source_quotes': []},
]
assign_sector_alignment(picks, intel)
for p in picks:
    print(p['ticker'], p['sector_alignment'], p['sector_alignment_label'])
"
```

Expected:
```
BHP confirms ✅ Confirms Materials (bullish)
WDS diverges ⚠️ Diverges from Energy (bearish)
```

---

## Task 6: Rewrite `format_email()`

**Files:**
- Modify: `asx_digest.py` — `format_email()` function signature and body

- [ ] **Step 1: Replace `format_email()` signature and plain-text block**

Replace the entire `format_email()` function (from `def format_email` through `return plain, html`) with:

```python
# ── Email Formatting ──────────────────────────────────────────────────────────
def format_email(aggregated, run_time, intel=None, run_mode="morning"):
    high = [s for s in aggregated if s["high_conviction"] and not s.get("sector_play")]
    single = [s for s in aggregated if not s["high_conviction"] and not s.get("sector_play")]
    sector_plays = [s for s in aggregated if s.get("sector_play")]
    aest = run_time.astimezone(timezone(timedelta(hours=11)))
    date_str = aest.strftime("%a %d %b %Y")
    time_str = aest.strftime("%I:%M%p AEST")

    if run_mode == "morning":
        title = f"ASX Morning Briefing — {date_str}"
        narrative_header = "📋 TODAY'S MARKET OUTLOOK"
        buzz_label = "What to watch"
    else:
        title = f"ASX Evening Wrap — {date_str}"
        narrative_header = "📋 WHAT HAPPENED TODAY"
        buzz_label = "What dominated today"

    intel = intel or {}
    narrative = intel.get("narrative", "")
    sentiment = intel.get("sentiment", "")
    mining_pulse = intel.get("mining_pulse", {})
    sectors = intel.get("sectors", [])
    buzz_topics = intel.get("buzz_topics", [])

    # ── Plain text ──
    lines = [title, "=" * 52, ""]

    if narrative:
        lines += [narrative_header, narrative]
        if sentiment:
            lines.append(f"Overall sentiment: {sentiment.title()}")
        lines.append("")

    # Mining pulse (always shown)
    mp_signal = mining_pulse.get("signal", "quiet").title()
    mp_reason = mining_pulse.get("reason", "")
    lines += ["⛏️  MINING PULSE", f"Signal: {mp_signal}", mp_reason, ""]

    # Sector signals (only if any)
    if sectors:
        lines.append("📊 SECTOR SIGNALS")
        for s in sectors:
            arrow = "↑" if s["signal"] == "bullish" else ("↓" if s["signal"] == "bearish" else "↔")
            lines.append(f"  {s['name']:<18} {arrow}  {s['reason']}")
        lines.append("")

    # Commodity prices from intel
    commodities = intel.get("commodities", [])
    if commodities:
        lines.append("💰 COMMODITIES (AUD)")
        for c in commodities:
            price = f"A${c['price_aud']:,.2f}" if c.get("price_aud") else "N/A"
            chg = f"{c['change_pct']:+.2f}%" if c.get("change_pct") is not None else ""
            note = f" — {c['note']}" if c.get("note") else ""
            lines.append(f"  {c['name']:<20} {price:<14} {chg}{note}")
        lines.append("")

    # Buzz topics
    if buzz_topics:
        lines += [f"💬 {buzz_label.upper()}", "  " + " · ".join(buzz_topics), ""]

    # Sector plays
    if sector_plays:
        lines.append("📈 SECTOR PLAYS")
        for s in sector_plays:
            lines.append(f"  {s['ticker']} ({s['company']}) — {s['signal']}")
            if s.get("summary"):
                lines.append(f"  {s['summary']}")
            lines.append("")

    lines.append("─" * 52)

    def format_pick_plain(s, source_label):
        out = []
        out.append(f"  {s['ticker']} ({s['company']}) — {s['signal']}")
        out.append(f"  {source_label}: {', '.join(s['sources'])}")
        alignment = s.get("sector_alignment_label", "")
        if alignment and alignment != "—":
            out.append(f"  Sector: {alignment}")
        if s.get("price_target"):
            out.append(f"  Price Target: {s['price_target']}")
        if s.get("summary"):
            out.append(f"  Thesis: {s['summary']}")
        if s.get("catalysts"):
            out.append("  Catalysts:")
            for c in s["catalysts"]:
                out.append(f"    • {c}")
        if s.get("risks"):
            out.append("  Risks:")
            for r in s["risks"]:
                out.append(f"    • {r}")
        if s.get("source_quotes"):
            out.append("  Key Quote:")
            for q in s["source_quotes"][:1]:
                out.append(f"    \"{q}\"")
        for m in s["mentions"]:
            if m.get("source_link"):
                out.append(f"  [{m['source']}] {m['source_title'][:70]}")
        out.append("")
        return out

    if high:
        lines.append("🔥 HIGH CONVICTION (2+ sources)\n")
        for s in high:
            lines.extend(format_pick_plain(s, "Sources"))

    if single:
        lines.append("📌 SINGLE SOURCE PICKS\n")
        for s in single:
            lines.extend(format_pick_plain(s, "Source"))

    if not high and not single and not sector_plays:
        lines.append("  No individual stock picks today.")
        lines.append("")

    lines.append("─" * 52)
    all_sources = sorted({src for stock in aggregated for src in stock["sources"]})
    source_count = len(all_sources)
    stock_count = len([s for s in aggregated if not s.get("sector_play")])
    lines.append(
        f"ASXDigest v2 · {stock_count} stocks · {source_count} sources · {run_mode} run · {time_str}"
    )
    plain = "\n".join(lines)

    # ── HTML ──
    signal_colors = {"BUY": "#27ae60", "SELL": "#e74c3c", "WATCH": "#f39c12"}
    mining_color = {"bullish": "#27ae60", "bearish": "#e74c3c", "mixed": "#f39c12", "quiet": "#aaa"}

    html_parts = [
        "<html><body style='font-family:Arial,sans-serif;max-width:620px;margin:auto;padding:16px;'>",
        f"<h2 style='color:#1a1a2e;margin-bottom:4px;'>{'📈' if run_mode == 'morning' else '📊'} {title}</h2>",
        f"<p style='color:#888;font-size:12px;margin-top:0;'>{time_str}</p>",
        "<hr style='border:1px solid #eee;'>",
    ]

    if narrative:
        html_parts.append(
            f"<div style='background:#f0f4ff;padding:12px 16px;border-radius:6px;margin-bottom:12px;'>"
            f"<b style='font-size:13px;color:#555;'>{narrative_header}</b>"
            f"<p style='margin:6px 0 4px 0;font-size:14px;color:#222;'>{narrative}</p>"
        )
        if sentiment:
            sent_color = {"bullish": "#27ae60", "cautiously bullish": "#2ecc71",
                          "mixed": "#f39c12", "cautiously bearish": "#e67e22",
                          "bearish": "#e74c3c", "neutral": "#888"}.get(sentiment, "#888")
            html_parts.append(
                f"<span style='font-size:12px;color:{sent_color};font-weight:bold;'>Overall: {sentiment.title()}</span>"
            )
        html_parts.append("</div>")

    # Mining pulse
    mp_color = mining_color.get(mining_pulse.get("signal", "quiet"), "#aaa")
    html_parts.append(
        f"<div style='background:#fafafa;border-left:4px solid {mp_color};padding:10px 14px;margin:8px 0;border-radius:4px;'>"
        f"<b style='font-size:13px;'>⛏️ Mining Pulse</b> "
        f"<span style='background:{mp_color};color:white;font-size:11px;padding:2px 7px;border-radius:10px;margin-left:6px;'>"
        f"{mining_pulse.get('signal','quiet').upper()}</span>"
        f"<p style='margin:6px 0 0 0;font-size:13px;color:#444;'>{mining_pulse.get('reason','')}</p>"
        f"</div>"
    )

    # Sector signals
    if sectors:
        html_parts.append("<div style='margin:8px 0;'><b style='font-size:13px;'>📊 Sector Signals</b><table style='width:100%;font-size:12px;margin-top:6px;border-collapse:collapse;'>")
        for s in sectors:
            sig_color = {"bullish": "#27ae60", "bearish": "#e74c3c", "mixed": "#f39c12"}.get(s["signal"], "#888")
            arrow = "▲" if s["signal"] == "bullish" else ("▼" if s["signal"] == "bearish" else "◆")
            html_parts.append(
                f"<tr><td style='padding:4px 8px;font-weight:bold;width:120px;'>{s['name']}</td>"
                f"<td style='color:{sig_color};width:24px;'>{arrow}</td>"
                f"<td style='color:#555;padding:4px;'>{s['reason']}</td></tr>"
            )
        html_parts.append("</table></div>")

    # Commodities
    if commodities:
        html_parts.append("<div style='margin:8px 0;'><b style='font-size:13px;'>💰 Commodities (AUD)</b><table style='width:100%;font-size:12px;margin-top:6px;'>")
        for c in commodities:
            price_str = f"A${c['price_aud']:,.2f}" if c.get("price_aud") else "N/A"
            chg = c.get("change_pct")
            chg_color = "#27ae60" if (chg or 0) > 0 else ("#e74c3c" if (chg or 0) < 0 else "#888")
            chg_str = f"{chg:+.2f}%" if chg is not None else ""
            html_parts.append(
                f"<tr><td style='padding:3px 8px;width:130px;'>{c['name']}</td>"
                f"<td style='font-weight:bold;width:100px;'>{price_str}</td>"
                f"<td style='color:{chg_color};width:60px;'>{chg_str}</td>"
                f"<td style='color:#888;'>{c.get('note','')}</td></tr>"
            )
        html_parts.append("</table></div>")

    # Buzz topics
    if buzz_topics:
        topic_pills = " ".join(
            f"<span style='background:#e8f4fd;color:#2980b9;padding:2px 8px;border-radius:10px;font-size:11px;margin:2px;display:inline-block;'>{t}</span>"
            for t in buzz_topics
        )
        html_parts.append(
            f"<div style='margin:8px 0;'><b style='font-size:13px;'>💬 {buzz_label}</b>"
            f"<div style='margin-top:6px;'>{topic_pills}</div></div>"
        )

    html_parts.append("<hr style='border:1px solid #eee;margin:16px 0;'>")

    def format_pick_html(s, border_width="4px", bg="#f8f8f8"):
        sig_color = signal_colors.get(s["signal"], "#555")
        alignment = s.get("sector_alignment_label", "")
        parts = [
            f"<div style='background:{bg};border-left:{border_width} solid {sig_color};"
            f"padding:12px;margin:10px 0;border-radius:4px;'>",
            f"<b style='font-size:16px;'>{s['ticker']}</b>"
            f" <span style='color:#888;font-size:14px;'>({s.get('company','')})</span>"
            f" <span style='background:{sig_color};color:white;padding:2px 7px;"
            f"border-radius:3px;font-size:12px;margin-left:6px;'>{s['signal']}</span>",
        ]
        if s.get("price_target"):
            parts.append(f" <span style='color:#888;font-size:12px;margin-left:6px;'>🎯 {s['price_target']}</span>")
        parts.append(f"<br><span style='color:#666;font-size:12px;'>{'  ·  '.join(s['sources'])}</span>")
        if alignment and alignment != "—":
            parts.append(f"<br><span style='font-size:11px;color:#666;'>{alignment}</span>")
        if s.get("summary"):
            parts.append(f"<p style='margin:8px 0 4px 0;font-size:14px;color:#222;'><b>Thesis:</b> {s['summary']}</p>")
        if s.get("catalysts"):
            parts.append("<p style='margin:6px 0 2px 0;font-size:12px;font-weight:bold;color:#555;'>Catalysts</p><ul style='margin:0;padding-left:18px;font-size:13px;color:#333;'>")
            for c in s["catalysts"]:
                parts.append(f"<li>{c}</li>")
            parts.append("</ul>")
        if s.get("risks"):
            parts.append("<p style='margin:6px 0 2px 0;font-size:12px;font-weight:bold;color:#c0392b;'>Risks</p><ul style='margin:0;padding-left:18px;font-size:13px;color:#555;'>")
            for r in s["risks"]:
                parts.append(f"<li>{r}</li>")
            parts.append("</ul>")
        if s.get("source_quotes"):
            parts.append("<p style='margin:6px 0 2px 0;font-size:12px;font-weight:bold;color:#555;'>Key Quote</p>")
            parts.append(f"<p style='margin:2px 0;font-size:12px;color:#444;font-style:italic;'>&#8220;{s['source_quotes'][0]}&#8221;</p>")
        parts.append("<p style='margin:6px 0 0 0;'>")
        for m in s["mentions"]:
            if m.get("source_link"):
                parts.append(
                    f"<a href='{m['source_link']}' style='font-size:12px;color:#3498db;"
                    f"display:inline-block;margin-right:10px;'>{m['source']}: {m['source_title'][:60]}</a>"
                )
        parts.append("</p></div>")
        return "".join(parts)

    if high:
        html_parts.append("<h3 style='color:#c0392b;'>🔥 HIGH CONVICTION — 2+ Sources</h3>")
        for s in high:
            html_parts.append(format_pick_html(s, border_width="4px"))

    if sector_plays:
        html_parts.append("<h3 style='color:#8e44ad;'>📈 Sector Plays</h3>")
        for s in sector_plays:
            html_parts.append(format_pick_html(s, border_width="3px", bg="#f5f0ff"))

    if single:
        html_parts.append("<h3 style='color:#2980b9;'>📌 Single Source Picks</h3>")
        for s in single:
            html_parts.append(format_pick_html(s, border_width="3px", bg="#fafafa"))

    if not high and not single and not sector_plays:
        html_parts.append("<p style='color:#888;font-style:italic;'>No individual stock picks today.</p>")

    html_parts.append(
        f"<hr style='border:1px solid #eee;'>"
        f"<p style='color:#bbb;font-size:11px;'>ASXDigest v2 · {stock_count} stocks · "
        f"{source_count} sources · {run_mode} run · {time_str}</p>"
        f"</body></html>"
    )
    html = "\n".join(html_parts)
    return plain, html
```

- [ ] **Step 2: Verify format_email renders without errors**

```bash
python3 -c "
exec(open('asx_digest.py').read())
from datetime import datetime, timezone

intel = {
    'narrative': 'Iron ore demand from China is driving materials sector strength.',
    'mining_pulse': {'signal': 'bullish', 'reason': 'Iron ore futures up 3.1% AUD overnight.'},
    'sectors': [{'name': 'Materials', 'signal': 'bullish', 'reason': 'Strong China PMI data.'}],
    'commodities': [
        {'name': 'Gold', 'price_aud': 4821.50, 'change_pct': 0.54, 'note': ''},
        {'name': 'Iron Ore (proxy)', 'price_aud': 162.30, 'change_pct': 3.10, 'note': 'China demand strong'},
    ],
    'buzz_topics': ['iron ore', 'china stimulus', 'rate cut'],
    'sentiment': 'cautiously bullish',
}
picks = [{
    'ticker': 'BHP', 'company': 'BHP Group', 'signal': 'BUY',
    'summary': 'Strong Q3 iron ore shipments beat estimates.', 'catalysts': ['China stimulus'],
    'risks': [], 'price_target': None, 'source_quotes': ['BHP is the cleanest China play'],
    'sources': ['Stockhead', 'The Market Herald'], 'source_count': 2,
    'high_conviction': True, 'sector_play': False,
    'sector_alignment': 'confirms', 'sector_alignment_label': '✅ Confirms Materials (bullish)',
    'mentions': [{'source': 'Stockhead', 'source_title': 'BHP surges', 'source_link': 'http://test.com'}],
}]
plain, html = format_email(picks, datetime.now(timezone.utc), intel=intel, run_mode='morning')
print(plain[:800])
print('---HTML length:', len(html))
"
```

Expected: clean plain text with all sections visible (MINING PULSE, SECTOR SIGNALS, COMMODITIES, BUZZ TOPICS, HIGH CONVICTION pick with sector alignment). HTML length > 2000.

---

## Task 7: Update `main()` to orchestrate new flow

**Files:**
- Modify: `asx_digest.py` — `main()` function

- [ ] **Step 1: Add run mode detection and market snapshot fetch at the top of `main()`**

Find `def main():` and add after `claude = config["claude_cli_path"]`:

```python
    # Determine run mode (morning briefing vs evening wrap-up)
    run_mode = "morning" if datetime.now().hour < 12 else "evening"
    log.info(f"Run mode: {run_mode}")

    # Fetch market snapshot concurrently (runs alongside RSS fetches below)
    snapshot = {}
    if config.get("market_snapshot", {}).get("enabled", False):
        log.info("Fetching market snapshot (Yahoo Finance)...")
        snapshot = fetch_market_snapshot(config["market_snapshot"]["symbols"])
        active = sum(1 for v in snapshot.values() if v and v != snapshot.get("AUDUSD=X"))
        log.info(f"  Snapshot: {active} symbols fetched")
```

- [ ] **Step 2: Add intelligence pass between item fetching and analysis**

Find this block in `main()`:
```python
    if not all_new_items:
        log.info("No new items found. Skipping digest.")
        save_state(state)
        return

    # ── Mark all items as seen immediately ──
```

Replace it with:

```python
    if not all_new_items and not snapshot:
        log.info("No new items and no market snapshot. Skipping digest.")
        save_state(state)
        return

    # ── Mark all items as seen immediately ──
```

Then find the block after the mark-as-seen loop:
```python
    # ── AI Analysis — batch all items ──
    log.info(f"Analyzing {len(all_new_items)} items with Claude (batches of 5)...")
```

Insert the intelligence pass before it:

```python
    # ── Pass 1: Market Intelligence ──
    intel = {}
    if all_new_items or snapshot:
        log.info("Running market intelligence pass (Pass 1)...")
        intel = run_intelligence_pass(all_new_items, snapshot, run_mode, claude)
        log.info(f"  Sentiment: {intel.get('sentiment','?')} | Mining: {intel.get('mining_pulse',{}).get('signal','?')}")

    # Build market context string for Pass 2
    market_context = ""
    if intel:
        active_sectors = ", ".join(s["name"] for s in intel.get("sectors", []))
        buzz = ", ".join(intel.get("buzz_topics", []))
        mp = intel.get("mining_pulse", {})
        market_context = (
            f"Sentiment: {intel.get('sentiment', 'neutral')}\n"
            f"Mining pulse: {mp.get('signal','quiet')} — {mp.get('reason','')}\n"
            f"Active sectors: {active_sectors or 'none notable'}\n"
            f"Buzz topics: {buzz or 'none'}"
        )

    # ── AI Analysis — batch all items ──
    log.info(f"Analyzing {len(all_new_items)} items with Claude (batches of 5)...")
```

- [ ] **Step 3: Pass `market_context` into `analyze_batch()` calls**

Find:
```python
        picks = analyze_batch(batch, claude)
```

Replace with:
```python
        picks = analyze_batch(batch, claude, market_context=market_context)
```

- [ ] **Step 4: Add `assign_sector_alignment()` call after aggregation**

Find:
```python
    log.info(f"Aggregated: {len(aggregated)} unique stocks "
             f"({sum(1 for s in aggregated if s['high_conviction'])} high conviction)")
```

Add after it:
```python
    assign_sector_alignment(aggregated, intel)
```

- [ ] **Step 5: Update no-picks guard to allow sending on sector signals**

Find:
```python
    if not all_picks:
        log.info("No stock picks found in new content. Skipping email.")
        save_state(state)
        return
```

Replace with:

```python
    # Send if there are picks, OR if the mining pulse / sectors are notable
    mining_signal = intel.get("mining_pulse", {}).get("signal", "quiet")
    has_notable_intel = mining_signal in ("bullish", "bearish", "mixed") or bool(intel.get("sectors"))

    if not all_picks and not has_notable_intel:
        log.info("No picks and no notable market signals. Skipping email.")
        save_state(state)
        return

    if not all_picks:
        log.info("No stock picks but notable market signals — sending intelligence-only digest.")
```

- [ ] **Step 6: Update `format_email()` call and subject line**

Find:
```python
    plain, html = format_email(aggregated, run_time)
    high_count = sum(1 for s in aggregated if s["high_conviction"])
    subject = f"ASX Digest: {len(aggregated)} picks"
    if high_count:
        subject += f" ({high_count} HIGH CONVICTION)"
```

Replace with:
```python
    plain, html = format_email(aggregated, run_time, intel=intel, run_mode=run_mode)
    high_count = sum(1 for s in aggregated if s["high_conviction"] and not s.get("sector_play"))
    stock_count = len([s for s in aggregated if not s.get("sector_play")])
    if run_mode == "morning":
        subject = f"ASX Morning Briefing — {run_time.astimezone(timezone(timedelta(hours=11))).strftime('%a %d %b')}"
    else:
        subject = f"ASX Evening Wrap — {run_time.astimezone(timezone(timedelta(hours=11))).strftime('%a %d %b')}"
    if high_count:
        subject += f" · {high_count} HIGH CONVICTION"
    elif stock_count:
        subject += f" · {stock_count} pick{'s' if stock_count != 1 else ''}"
    else:
        sentiment = intel.get("sentiment", "")
        if sentiment:
            subject += f" · {sentiment.title()}"
```

- [ ] **Step 7: Full dry run end-to-end**

```bash
cd /Users/ianf/.claude/PAI/Tools/ASXDigest
python3 asx_digest.py --dry-run 2>&1 | tail -50
```

Expected log lines (in order):
```
Run mode: morning  (or evening depending on time)
Fetching market snapshot (Yahoo Finance)...
  Snapshot: N symbols fetched
Fetching Wealth Within (YouTube)...
...
Fetching ABC Business...
  N new items from ABC Business
Fetching Reuters Australia...
  N new items from Reuters Australia
Fetching Mining Weekly...
  N new items from Mining Weekly
...
Running market intelligence pass (Pass 1)...
  Sentiment: ... | Mining: ...
Analyzing N items with Claude (batches of 5)...
...
```

No ERROR or unexpected WARNING lines. Email content printed to log (DRY RUN) shows the new sections.

---

## Task 8: Final validation and commit

- [ ] **Step 1: Check log for clean output from the dry run**

```bash
grep -E "(ERROR|WARNING)" /Users/ianf/.claude/PAI/Tools/ASXDigest/logs/asx_digest.log | tail -20
```

Expected: only known benign warnings (Reddit 403 on weekends, Stockhead timeout occasionally). No new errors introduced by v2 changes.

- [ ] **Step 2: Confirm the email format in dry run log**

```bash
grep -A 80 "DRY RUN — printing" /Users/ianf/.claude/PAI/Tools/ASXDigest/logs/asx_digest.log | tail -60
```

Expected: email output contains `ASX Morning Briefing` or `ASX Evening Wrap`, `⛏️  MINING PULSE`, and commodity prices in AUD format (`A$`).

- [ ] **Step 3: Commit**

```bash
cd /Users/ianf/.claude/PAI/Tools/ASXDigest
git add asx_digest.py config.json Plans/2026-03-28-asx-digest-v2-plan.md Plans/2026-03-28-asx-digest-v2-design.md 2>/dev/null || true
git -C /Users/ianf/.claude add PAI/Tools/ASXDigest/asx_digest.py PAI/Tools/ASXDigest/config.json PAI/Tools/ASXDigest/Plans/ 2>/dev/null || echo "not a git repo — skip commit"
```

---

## Self-Review Notes

- **Spec coverage:** All 8 spec requirements covered: run mode ✅, market snapshot ✅, Pass 1 intelligence ✅, Pass 2 context injection ✅, sector alignment ✅, new email template ✅, no-picks behaviour change ✅, mining staple ✅
- **No placeholders:** All code blocks are complete and executable
- **Type consistency:** `intel` dict uses same keys throughout (`narrative`, `mining_pulse`, `sectors`, `commodities`, `buzz_topics`, `sentiment`). `market_context` is a plain string passed through consistently. `sector_alignment_label` set in `assign_sector_alignment()` and read in `format_email()`
- **EXTRACT_PROMPT change:** The `{market_context}` placeholder is added at the top; `{content}` at the bottom — both must be present in the format call in `analyze_batch()`. Verified in Task 4 Step 2

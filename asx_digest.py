#!/usr/bin/env python3
"""
ASX Stock Tip Aggregator
Fetches 5 ASX tip sources, extracts stock picks via Claude, sends email digest.
"""

import json
import os
import re
import sys
import subprocess
import urllib.request
import urllib.error
import urllib.parse
import smtplib
import xml.etree.ElementTree as ET
import hashlib
import logging
import concurrent.futures
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from collections import defaultdict

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE = BASE_DIR / "state.json"
LOG_FILE = BASE_DIR / "logs" / "asx_digest.log"

LOG_FILE.parent.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
    ]
)
log = logging.getLogger(__name__)

DRY_RUN = "--dry-run" in sys.argv


# ── Config & State ────────────────────────────────────────────────────────────
def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)

def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"seen_ids": {}, "last_run": None}

def save_state(state):
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── YouTube Transcript Extraction ─────────────────────────────────────────────
def extract_youtube_video_id(url):
    """Extract video ID from a YouTube watch URL."""
    match = re.search(r'[?&]v=([a-zA-Z0-9_-]{11})', url)
    return match.group(1) if match else None


def fetch_youtube_transcript(video_id, max_chars=2000):
    """Fetch transcript text from a YouTube video. Returns None on failure."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        ytt_api = YouTubeTranscriptApi()
        transcript = ytt_api.fetch(video_id, languages=["en-AU", "en"])
        text = " ".join(s.text for s in transcript.snippets)
        return text[:max_chars]
    except Exception as e:
        log.debug(f"Transcript fetch failed for {video_id}: {e}")
        return None


def enrich_with_transcripts(items):
    """Fetch YouTube transcripts in parallel for any YouTube video items.
    Replaces the description with transcript text and sets has_transcript=True.
    """
    yt_items = [
        (i, item) for i, item in enumerate(items)
        if "youtube.com/watch" in item.get("link", "")
    ]
    if not yt_items:
        return items

    log.info(f"  Fetching transcripts for {len(yt_items)} YouTube videos...")

    def fetch_one(idx_item):
        idx, item = idx_item
        video_id = extract_youtube_video_id(item["link"])
        if video_id:
            return idx, fetch_youtube_transcript(video_id)
        return idx, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(fetch_one, yt_items))

    enriched = 0
    for idx, transcript in results:
        if transcript:
            items[idx]["description"] = transcript
            items[idx]["has_transcript"] = True
            enriched += 1

    log.info(f"  {enriched}/{len(yt_items)} transcripts fetched successfully")
    return items


# ── Fetching ──────────────────────────────────────────────────────────────────
def fetch_url(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {
        "User-Agent": "Mozilla/5.0 (compatible; ASXDigest/1.0)"
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning(f"Failed to fetch {url}: {e}")
        return None


def parse_rss(xml_text, source_name, max_age_hours, seen_ids):
    """Parse RSS/Atom feed, return new items."""
    items = []
    try:
        # Strip BOM and leading whitespace (causes "declaration not at start" errors)
        xml_text = xml_text.lstrip('\ufeff').strip()
        root = ET.fromstring(xml_text)
        ns = {
            "atom": "http://www.w3.org/2005/Atom",
            "content": "http://purl.org/rss/1.0/modules/content/",
            "media": "http://search.yahoo.com/mrss/",
        }
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)

        # Handle both RSS <item> and Atom <entry>
        entries = root.findall(".//item") or root.findall(".//atom:entry", ns)

        for entry in entries:
            def find_first(el, tags):
                for tag in tags:
                    # Try without namespaces first (plain RSS elements)
                    found = el.find(tag)
                    if found is not None:
                        return found
                    # Then try with namespace dict (Atom elements like atom:title)
                    if ":" in tag:
                        found = el.find(tag, ns)
                        if found is not None:
                            return found
                return None

            # Title
            title_el = find_first(entry, ["title", "atom:title"])
            title = (title_el.text or "").strip() if title_el is not None else ""

            # Link
            link_el = find_first(entry, ["link", "atom:link"])
            if link_el is not None:
                link = link_el.get("href") or link_el.text or ""
            else:
                link = ""
            link = link.strip()

            # Description / summary
            desc_el = find_first(entry, ["description", "summary", "atom:summary", "content:encoded"])
            description = ""
            if desc_el is not None and desc_el.text:
                import re
                description = re.sub(r"<[^>]+>", " ", desc_el.text).strip()[:1000]

            # Date
            date_el = find_first(entry, ["pubDate", "published", "atom:published", "updated", "atom:updated"])
            pub_date = None
            if date_el is not None and date_el.text:
                for fmt in [
                    "%a, %d %b %Y %H:%M:%S %z",
                    "%Y-%m-%dT%H:%M:%S%z",
                    "%Y-%m-%dT%H:%M:%SZ",
                ]:
                    try:
                        pub_date = datetime.strptime(date_el.text.strip(), fmt)
                        if pub_date.tzinfo is None:
                            pub_date = pub_date.replace(tzinfo=timezone.utc)
                        break
                    except ValueError:
                        continue

            if pub_date and pub_date < cutoff:
                continue

            item_id = hashlib.md5((title + link).encode()).hexdigest()
            source_seen = seen_ids.get(source_name, set())
            if isinstance(source_seen, list):
                source_seen = set(source_seen)
            if item_id in source_seen:
                continue

            items.append({
                "id": item_id,
                "title": title,
                "link": link,
                "description": description,
                "source": source_name,
                "pub_date": pub_date.isoformat() if pub_date else None,
            })

    except Exception as e:
        log.warning(f"RSS parse error for {source_name}: {e}")
    return items


def fetch_reddit(url, source_name, max_age_hours, seen_ids):
    """Fetch Reddit JSON listing, return new posts."""
    items = []
    raw = fetch_url(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"})
    if not raw:
        return items
    try:
        data = json.loads(raw)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        posts = data["data"]["children"]
        for post in posts:
            p = post["data"]
            created = datetime.fromtimestamp(p.get("created_utc", 0), tz=timezone.utc)
            if created < cutoff:
                continue
            item_id = p.get("id", "")
            source_seen = seen_ids.get(source_name, set())
            if isinstance(source_seen, list):
                source_seen = set(source_seen)
            if item_id in source_seen:
                continue
            title = p.get("title", "")
            selftext = (p.get("selftext") or "")[:800]
            flair = p.get("link_flair_text") or ""
            items.append({
                "id": item_id,
                "title": title,
                "link": f"https://reddit.com{p.get('permalink', '')}",
                "description": f"[{flair}] {selftext}".strip() if flair else selftext,
                "source": source_name,
                "pub_date": created.isoformat(),
                "upvotes": p.get("score", 0),
            })
    except Exception as e:
        log.warning(f"Reddit parse error: {e}")
    return items


def fetch_asx_announcements(seen_ids, max_age_hours):
    """Fetch today's ASX price-sensitive announcements from the official API."""
    items = []
    raw = fetch_url(ASX_ANNS_URL, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.asx.com.au/",
    })
    if not raw or not raw.strip():
        log.warning("ASX Announcements: fetch failed or empty response")
        return items
    try:
        data = json.loads(raw)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        source_name = "ASX Announcements"
        source_seen = set(seen_ids.get(source_name, []))

        for ann in data.get("data", []):
            # Only price-sensitive announcements — others are too noisy
            if not ann.get("priceSensitive"):
                continue
            ticker = ann.get("asxCode", "").upper().strip()
            headline = ann.get("headline", "").strip()
            if not ticker or not headline:
                continue

            # Parse timestamp
            ts_str = ann.get("timeStamp", "")
            pub_date = None
            if ts_str:
                for fmt in ["%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"]:
                    try:
                        pub_date = datetime.strptime(ts_str[:19], "%Y-%m-%dT%H:%M:%S")
                        pub_date = pub_date.replace(tzinfo=timezone.utc)
                        break
                    except ValueError:
                        continue
            if pub_date and pub_date < cutoff:
                continue

            item_id = hashlib.md5((ticker + headline).encode()).hexdigest()
            if item_id in source_seen:
                continue

            category = ann.get("documentReleaseType", "")
            pdf_url = ann.get("pdfUrl", "")
            if pdf_url and not pdf_url.startswith("http"):
                pdf_url = "https://www.asx.com.au" + pdf_url

            items.append({
                "id": item_id,
                "title": f"ASX Price-Sensitive: {ticker} — {headline}",
                "link": pdf_url,
                "description": (
                    f"ASX Code: {ticker}. {headline}. "
                    f"Release type: {category}. "
                    f"This is a price-sensitive ASX announcement."
                ),
                "source": source_name,
                "pub_date": pub_date.isoformat() if pub_date else None,
            })
    except Exception as e:
        log.warning(f"ASX announcements parse error: {e}")
    log.info(f"  {len(items)} new price-sensitive announcements")
    return items


def fetch_price_signals(extra_tickers=None, seen_ids=None):
    """Scan ASX stocks for notable price/volume activity via Yahoo Finance v8 chart API.
    Uses concurrent per-symbol requests — no authentication required.
    """
    if seen_ids is None:
        seen_ids = {}
    source_name = "Price Signals"
    source_seen = set(seen_ids.get(source_name, []))

    tickers = list(set(ASX_TOP_STOCKS + (extra_tickers or [])))
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def fetch_ticker_data(ticker):
        symbol = f"{ticker}.AX"
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=10d"
        raw = fetch_url(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "application/json",
        })
        if not raw:
            return None
        try:
            data = json.loads(raw)
            result = data.get("chart", {}).get("result", [None])[0]
            if not result:
                return None
            meta = result.get("meta", {})
            quotes = result.get("indicators", {}).get("quote", [{}])[0]
            closes = [c for c in (quotes.get("close") or []) if c is not None]
            volumes = [v for v in (quotes.get("volume") or []) if v is not None]
            current_price = meta.get("regularMarketPrice") or (closes[-1] if closes else 0)
            prev_close = closes[-2] if len(closes) >= 2 else 0
            current_volume = meta.get("regularMarketVolume") or (volumes[-1] if volumes else 0)
            avg_volume = sum(volumes[:-1]) / len(volumes[:-1]) if len(volumes) > 1 else 0
            high_52w = meta.get("fiftyTwoWeekHigh") or 0
            pct_change = ((current_price - prev_close) / prev_close * 100) if prev_close else 0
            return {
                "ticker": ticker,
                "pct_change": pct_change,
                "volume": current_volume,
                "avg_volume": avg_volume,
                "close": current_price,
                "high_52w": high_52w,
            }
        except Exception as e:
            log.debug(f"Yahoo v8 parse error for {ticker}: {e}")
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(fetch_ticker_data, tickers))

    items = []
    for q in results:
        if q is None:
            continue
        ticker = q["ticker"]
        pct_change = q["pct_change"]
        volume = q["volume"]
        avg_vol = q["avg_volume"]
        close = q["close"]
        high_52w = q["high_52w"]

        vol_ratio = volume / avg_vol if avg_vol > 0 else 0
        near_52w_high = (close >= high_52w * 0.97) if high_52w > 0 else False

        signals = []
        if vol_ratio > 2.5 and pct_change > 2:
            signals.append(f"Volume spike {vol_ratio:.1f}x avg with +{pct_change:.1f}% gain")
        elif vol_ratio > 1.8 and pct_change > 1:
            signals.append(f"Elevated volume {vol_ratio:.1f}x avg with +{pct_change:.1f}%")
        if near_52w_high and pct_change > 0:
            signals.append(f"Near 52-week high (${high_52w:.3f})")
        if pct_change > 5:
            signals.append(f"Strong daily gain +{pct_change:.1f}%")
        elif pct_change < -5:
            signals.append(f"Sharp decline {pct_change:.1f}%")

        if not signals:
            continue

        item_id = hashlib.md5((ticker + today_str).encode()).hexdigest()
        if item_id in source_seen:
            continue

        title = f"{ticker}: {', '.join(signals)}"
        description = (
            f"ASX: {ticker}. Price: ${close:.3f}, Change: {pct_change:+.1f}% today. "
            f"Volume: {vol_ratio:.1f}x average ({int(volume):,} vs avg {int(avg_vol):,}). "
            f"Near 52-week high: {'Yes' if near_52w_high else 'No'} (52w high: ${high_52w:.3f})."
        )
        items.append({
            "id": item_id,
            "title": title,
            "link": f"https://finance.yahoo.com/quote/{ticker}.AX",
            "description": description,
            "source": source_name,
            "pub_date": datetime.now(timezone.utc).isoformat(),
        })

    log.info(f"  {len(items)} price signals found")
    return items


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
    "PLS.AX":   ("Lithium (PLS)",      "aud",  None),
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


# ── AI Analysis ───────────────────────────────────────────────────────────────
EXTRACT_PROMPT = """You are an ASX stock analyst. Analyze the ITEMS below and extract any ASX stock picks, buy signals, or investment recommendations.

{market_context}

For each stock clearly being recommended, output a JSON object on its own line:
{{"ticker":"LYC","company":"Lynas Rare Earths","signal":"BUY","confidence":"HIGH","item_index":0,"summary":"2-3 sentence investment thesis using specific facts from the source","catalysts":["specific catalyst 1","specific catalyst 2"],"risks":["key risk if mentioned"],"price_target":"$X or null","source_quotes":["verbatim key quote or data point from the article"]}}

Fields:
- ticker: 2-5 char ASX code
- company: full company name
- signal: BUY/SELL/WATCH
- confidence: HIGH/MEDIUM/LOW
- item_index: which item it came from (0-based)
- summary: 2-3 sentences covering the core thesis with specific facts (numbers, deals, catalysts)
- catalysts: list of 2-4 specific upcoming catalysts or reasons to watch
- risks: list of 1-2 key risks if mentioned, else empty list
- price_target: analyst price target if stated, else null
- source_quotes: list of 1-2 verbatim key quotes or data points from the source
- sector_play: true if this is a broad sector call rather than a specific stock (optional, omit if false)

Rules:
- Only include stocks explicitly recommended as picks/buys — not passing mentions
- Output ONLY JSON lines, nothing else
- If no clear picks in all items, output nothing
- Be specific — use numbers, names, and facts from the source, not vague generalities

ITEMS:
{content}"""

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
        if result.returncode != 0:
            log.warning(f"Intelligence pass: Claude exited {result.returncode}; stderr: {result.stderr[:300]}")
            # Hook failures set exit code 1 but stdout may still be valid — only bail if empty.
            if not result.stdout.strip():
                return fallback
        output = result.stdout.strip()

        def _apply_intel_defaults(intel):
            intel.setdefault("narrative", "")
            intel.setdefault("mining_pulse", fallback["mining_pulse"])
            intel.setdefault("sectors", [])
            intel.setdefault("commodities", [])
            intel.setdefault("buzz_topics", [])
            intel.setdefault("sentiment", "neutral")
            return intel

        # Try single-line JSON scan first
        for line in output.split("\n"):
            line = line.strip()
            if line.startswith("{") and '"narrative"' in line:
                try:
                    return _apply_intel_defaults(json.loads(line))
                except json.JSONDecodeError:
                    pass
        # Fallback: try parsing full output as JSON (handles multi-line formatted responses)
        try:
            intel = json.loads(output)
            if "narrative" in intel or "sentiment" in intel:
                return _apply_intel_defaults(intel)
        except (json.JSONDecodeError, AttributeError):
            pass
        log.warning(f"Intelligence pass: could not parse Claude output as JSON; stderr: {result.stderr[:200]!r}")
        return fallback
    except subprocess.TimeoutExpired:
        log.warning("Intelligence pass: Claude timeout")
        return fallback
    except Exception as e:
        log.warning(f"Intelligence pass error: {e}")
        return fallback


# Pre-filter: only analyze items that look like they contain stock picks
# Tier 1 — sources that are inherently signals (always include)
AUTO_INCLUDE_SOURCES = {"ASX Announcements", "Price Signals", "r/ASX_Bets"}

# Sources that require strict ASX ticker pattern (high-noise, low-pick-rate)
STRICT_FILTER_SOURCES = {"ABC Business", "SMH Business"}

# Strong patterns: ASX ticker-style reference
_ASX_STRONG_RE = re.compile(
    r'\b[A-Z]{2,5}\b|asx[:\s]|\(asx|\.ax\b',
    re.IGNORECASE
)
# Weak patterns: general pick/recommendation language
_ASX_WEAK_PATTERNS = [
    "buy", "pick", "tip", "recommend", "target", "bull", "breakout",
    "entry", "accumulate", "purchase", "upside", "price target",
]

# ASX large caps to scan for price signals (Yahoo Finance, no API key needed)
ASX_TOP_STOCKS = [
    "CBA", "BHP", "CSL", "WBC", "NAB", "ANZ", "WES", "MQG",
    "WDS", "GMG", "TLS", "RIO", "COL", "WOW", "ALL", "QBE",
    "SUN", "IAG", "RMD", "XRO", "FMG", "TWE", "NST", "EVN",
    "MIN", "JHX", "STO", "NXT", "REA", "CPU", "FPH", "COH",
]

# ASX official announcements API
ASX_ANNS_URL = "https://www.asx.com.au/asx/v2/statistics/todayAnns.do"

def looks_like_stock_pick(item):
    """Two-tier heuristic filter before sending to Claude.
    Tier 1: auto-include high-signal sources and transcript-enriched YouTube items.
    Tier 2: strict sources require ASX ticker pattern; others need strong+weak signal.
    """
    source = item.get("source", "")
    if source in AUTO_INCLUDE_SOURCES:
        return True
    if item.get("has_transcript"):
        return True

    text = (item.get("title", "") + " " + item.get("description", ""))
    text_lower = text.lower()

    has_strong = bool(_ASX_STRONG_RE.search(text))
    has_weak = any(p in text_lower for p in _ASX_WEAK_PATTERNS)

    if source in STRICT_FILTER_SOURCES:
        # High-noise sources: require explicit ASX ticker pattern
        return has_strong
    else:
        # Other sources: strong pattern alone, or strong+weak combination
        return has_strong or has_weak


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
    try:
        result = subprocess.run(
            [claude_path, "--print", "--model", "claude-haiku-4-5-20251001"],
            input=prompt, capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            log.warning(f"Claude batch exit {result.returncode}: {result.stderr[:200]}")
            # Hook failures (e.g. missing node) set exit code 1 but stdout is still valid.
            # Only bail out if stdout is empty — otherwise fall through and parse it.
            if not result.stdout.strip():
                return []
        output = result.stdout.strip()
        picks = []
        for line in output.split("\n"):
            line = line.strip()
            if line.startswith("{") and '"ticker"' in line:
                try:
                    pick = json.loads(line)
                    idx = pick.get("item_index", 0)
                    if 0 <= idx < len(items):
                        src_item = items[idx]
                    else:
                        src_item = items[0]
                    pick.setdefault("signal", "BUY")
                    pick.setdefault("confidence", "MEDIUM")
                    pick.setdefault("summary", "")
                    pick.setdefault("catalysts", [])
                    pick.setdefault("risks", [])
                    pick.setdefault("price_target", None)
                    pick.setdefault("source_quotes", [])
                    pick.setdefault("sector_play", False)
                    pick["source"] = src_item["source"]
                    pick["source_title"] = src_item["title"]
                    pick["source_link"] = src_item.get("link", "")
                    picks.append(pick)
                except json.JSONDecodeError:
                    pass
        return picks
    except subprocess.TimeoutExpired:
        log.warning(f"Claude timeout on batch of {len(items)} items — retrying with split batch")
        if len(items) <= 1:
            return []
        mid = len(items) // 2
        return (
            analyze_batch(items[:mid], claude_path, batch_size, market_context) +
            analyze_batch(items[mid:], claude_path, batch_size, market_context)
        )
    except Exception as e:
        log.warning(f"Claude batch error: {e}")
        return []


# ── Aggregation ───────────────────────────────────────────────────────────────
def aggregate_picks(all_picks, min_sources_for_high_conviction):
    """Group picks by ticker, count sources, flag high conviction."""
    by_ticker = defaultdict(list)
    for pick in all_picks:
        ticker = pick.get("ticker", "").upper().strip()
        if ticker and 2 <= len(ticker) <= 5:
            by_ticker[ticker].append(pick)

    aggregated = []
    for ticker, picks in by_ticker.items():
        sources = list({p["source"] for p in picks})
        best = max(picks, key=lambda p: {"HIGH": 3, "MEDIUM": 2, "LOW": 1}.get(p.get("confidence", "LOW"), 1))
        aggregated.append({
            "ticker": ticker,
            "company": best.get("company", ""),
            "signal": best.get("signal", "MENTION"),
            "summary": best.get("summary", ""),
            "catalysts": best.get("catalysts", []),
            "risks": best.get("risks", []),
            "price_target": best.get("price_target"),
            "source_quotes": best.get("source_quotes", []),
            "sources": sources,
            "source_count": len(sources),
            "high_conviction": len(sources) >= min_sources_for_high_conviction,
            "mentions": picks,
        })

    # Sort: high conviction first, then by source count
    aggregated.sort(key=lambda x: (-x["high_conviction"], -x["source_count"]))
    return aggregated


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


# ── Email Sending ─────────────────────────────────────────────────────────────
def send_gmail(plain, html, subject, config):
    ec = config["email"]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = ec["from_address"]
    msg["To"] = ", ".join(ec["recipients"])
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP(ec["smtp_host"], ec["smtp_port"]) as server:
        server.ehlo()
        server.starttls()
        server.login(ec["from_address"], ec["app_password"])
        server.sendmail(ec["from_address"], ec["recipients"], msg.as_string())
    log.info(f"Email sent to {ec['recipients']}")


def send_agentmail(plain, html, subject, config):
    """Send via AgentMail REST API."""
    import urllib.parse
    ac = config["agentmail"]
    payload = json.dumps({
        "to": config["email"]["recipients"],
        "subject": subject,
        "text": plain,
        "html": html,
    }).encode()
    inbox_id_encoded = urllib.parse.quote(ac['inbox_id'], safe='@')
    req = urllib.request.Request(
        f"https://api.agentmail.to/v0/inboxes/{inbox_id_encoded}/messages/send",
        data=payload,
        headers={
            "Authorization": f"Bearer {ac['api_key']}",
            "Content-Type": "application/json",
        },
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        log.info(f"AgentMail sent: {r.status} to {config['email']['recipients']}")


def send_email(plain, html, subject, config):
    method = config["email"].get("method", "gmail")
    if method == "agentmail":
        send_agentmail(plain, html, subject, config)
    else:
        send_gmail(plain, html, subject, config)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    log.info(f"=== ASXDigest run started {'(DRY RUN)' if DRY_RUN else ''} ===")
    config = load_config()
    state = load_state()
    seen_ids = state.get("seen_ids", {})
    max_age = config["thresholds"]["max_age_hours"]
    max_items = config["thresholds"]["max_items_per_source"]
    claude = config["claude_cli_path"]

    # Determine run mode (morning briefing vs evening wrap-up)
    run_mode = "morning" if datetime.now().hour < 12 else "evening"
    log.info(f"Run mode: {run_mode}")

    # Fetch market snapshot concurrently (runs alongside RSS fetches below)
    snapshot = {}
    if config.get("market_snapshot", {}).get("enabled", False):
        log.info("Fetching market snapshot (Yahoo Finance)...")
        snapshot = fetch_market_snapshot(config["market_snapshot"]["symbols"])
        active = sum(1 for v in snapshot.values() if v)
        log.info(f"  Snapshot: {active} symbols fetched")

    all_new_items = []

    # ── YouTube RSS sources ──
    for key, src in config["sources"].items():
        if not src.get("enabled"):
            continue
        name = src["name"]
        if key in ("asx_bets_reddit", "asx_announcements", "price_signals"):
            continue  # handled separately

        log.info(f"Fetching {name}...")
        raw = fetch_url(src["url"], headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"})
        if not raw:
            log.warning(f"Skipped {name} — fetch failed")
            continue
        items = parse_rss(raw, name, max_age, seen_ids)
        items = items[:max_items]
        log.info(f"  {len(items)} new items from {name}")
        all_new_items.extend(items)

    # ── Reddit ──
    reddit_cfg = config["sources"].get("asx_bets_reddit", {})
    if reddit_cfg.get("enabled"):
        log.info("Fetching r/ASX_Bets...")
        items = []
        for url in [reddit_cfg["url"], reddit_cfg.get("url_new", "")]:
            if url:
                items.extend(fetch_reddit(url, reddit_cfg["name"], max_age, seen_ids))
        # Dedupe within reddit items by id
        seen_r = set()
        deduped = []
        for it in items:
            if it["id"] not in seen_r:
                seen_r.add(it["id"])
                deduped.append(it)
        # Filter low-engagement posts (upvotes < 5 for hot, < 2 for new)
        deduped = [i for i in deduped if i.get("upvotes", 0) >= 2]
        deduped = deduped[:max_items]
        log.info(f"  {len(deduped)} new Reddit items")
        all_new_items.extend(deduped)

    # ── ASX Official Announcements ──
    if config["sources"].get("asx_announcements", {}).get("enabled", True):
        log.info("Fetching ASX price-sensitive announcements...")
        ann_items = fetch_asx_announcements(seen_ids, max_age)
        all_new_items.extend(ann_items)
        # Collect tickers from today's announcements to cross-check with price signals
        ann_tickers = []
        for item in ann_items:
            # title format: "ASX Price-Sensitive: {TICKER} — {headline}"
            try:
                ann_tickers.append(item["title"].split("—")[0].replace("ASX Price-Sensitive:", "").strip())
            except Exception:
                pass
    else:
        ann_tickers = []

    # ── Price Signals ──
    if config["sources"].get("price_signals", {}).get("enabled", True):
        log.info("Scanning for price signals (Yahoo Finance)...")
        price_items = fetch_price_signals(extra_tickers=ann_tickers, seen_ids=seen_ids)
        all_new_items.extend(price_items)

    # ── P0: Enrich YouTube items with transcripts ──
    if all_new_items:
        all_new_items = enrich_with_transcripts(all_new_items)

    if not all_new_items and not snapshot:
        log.info("No new items and no market snapshot. Skipping digest.")
        save_state(state)
        return

    # ── Mark all items as seen immediately ──
    for item in all_new_items:
        src_name = item["source"]
        if src_name not in seen_ids:
            seen_ids[src_name] = []
        if isinstance(seen_ids[src_name], set):
            seen_ids[src_name] = list(seen_ids[src_name])
        if item["id"] not in seen_ids[src_name]:
            seen_ids[src_name].append(item["id"])

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

    # ── P2: Pre-filter to items likely containing stock picks ──
    filtered_items = [item for item in all_new_items if looks_like_stock_pick(item)]
    log.info(f"Pre-filter: {len(filtered_items)} items for analysis (from {len(all_new_items)} total)")

    # ── P3: AI Analysis — sequential batches ──
    log.info(f"Analyzing {len(filtered_items)} items with Claude (batches of 5)...")

    all_picks = []
    batch_size = 5
    for i in range(0, len(filtered_items), batch_size):
        batch = filtered_items[i:i + batch_size]
        n_batches = (len(filtered_items) - 1) // batch_size + 1
        log.info(f"  Batch {i // batch_size + 1}/{n_batches} ({len(batch)} items)...")
        picks = analyze_batch(batch, claude, batch_size, market_context)
        if picks:
            log.info(f"    → {len(picks)} pick(s): {[p['ticker'] for p in picks]}")
        all_picks.extend(picks)

    state["seen_ids"] = seen_ids

    # Send if there are picks, OR if the mining pulse / sectors are notable
    mining_signal = intel.get("mining_pulse", {}).get("signal", "quiet")
    has_notable_intel = mining_signal in ("bullish", "bearish", "mixed") or bool(intel.get("sectors"))

    if not all_picks and not has_notable_intel:
        log.info("No picks and no notable market signals. Skipping email.")
        save_state(state)
        return

    if not all_picks:
        log.info("No stock picks but notable market signals — sending intelligence-only digest.")

    # ── Aggregation ──
    min_src = config["thresholds"]["high_conviction_min_sources"]
    aggregated = aggregate_picks(all_picks, min_src)
    log.info(f"Aggregated: {len(aggregated)} unique stocks "
             f"({sum(1 for s in aggregated if s['high_conviction'])} high conviction)")
    assign_sector_alignment(aggregated, intel)

    # ── Format & Send ──
    run_time = datetime.now(timezone.utc)
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

    if DRY_RUN:
        log.info("DRY RUN — printing digest, not sending email:")
        print("\n" + plain)
    else:
        try:
            send_email(plain, html, subject, config)
        except Exception as e:
            log.error(f"Email send failed: {e}")
            log.info("Digest (not sent):\n" + plain)

    save_state(state)
    log.info(f"=== ASXDigest run complete ===")


if __name__ == "__main__":
    main()

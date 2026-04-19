from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from typing import Iterable

from asx_digest import LOG_FILE, fetch_url, load_config, parse_rss  # type: ignore
from youtube_cache import (
    DEFAULT_FRESH_MINUTES,
    load_cache,
    prune_cache,
    save_cache,
    update_entry,
    is_fresh,
)

LOG = logging.getLogger("youtube_collector")


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch YouTube feeds with throttling and update cache")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore freshness and refetch all configured YouTube sources",
    )
    parser.add_argument(
        "--sources",
        type=str,
        default="",
        help="Comma-separated source keys to fetch (defaults to all enabled YouTube sources)",
    )
    parser.add_argument(
        "--fresh-minutes",
        type=int,
        default=DEFAULT_FRESH_MINUTES,
        help=f"Cache freshness window in minutes (default {DEFAULT_FRESH_MINUTES})",
    )
    parser.add_argument(
        "--sleep-min",
        type=float,
        default=5.0,
        help="Minimum seconds to sleep between requests",
    )
    parser.add_argument(
        "--sleep-max",
        type=float,
        default=10.0,
        help="Maximum seconds to sleep between requests",
    )
    return parser.parse_args(argv)


def select_sources(config: dict, requested: set[str] | None = None) -> list[tuple[str, dict]]:
    sources = []
    for key, src in config.get("sources", {}).items():
        if requested and key not in requested:
            continue
        if not src.get("enabled"):
            continue
        url = src.get("url", "")
        if "youtube.com/feeds/videos.xml" not in url:
            continue
        sources.append((key, src))
    return sources


def sleep_between(min_s: float, max_s: float, label: str) -> None:
    if max_s <= 0:
        return
    high = max(min_s, max_s)
    low = min(min_s, max_s)
    if high <= 0:
        return
    duration = random.uniform(max(0.0, low), high)
    LOG.info("[YTCOL] Sleeping %.1fs before %s", duration, label)
    time.sleep(duration)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config()
    thresholds = config.get("thresholds", {})
    max_age_hours = thresholds.get("max_age_hours", 168)
    max_items = thresholds.get("max_items_per_source", 20)

    requested = {s.strip() for s in args.sources.split(",") if s.strip()} if args.sources else None
    sources = select_sources(config, requested)
    if not sources:
        LOG.info("[YTCOL] No matching YouTube sources found. Nothing to do.")
        return 0

    cache = load_cache()
    cache_updated = False

    LOG.info("[YTCOL] Starting YouTube collector for %d source(s)", len(sources))

    for idx, (key, src) in enumerate(sources, start=1):
        entry = cache.get(key)
        if entry and not args.force and is_fresh(entry, args.fresh_minutes):
            LOG.info("[YTCOL] %s already fresh (fetched at %s). Skipping.", key, entry.get("fetched_at"))
            continue

        if idx > 1:
            sleep_between(args.sleep_min, args.sleep_max, src.get("name", key))

        url = src.get("url")
        name = src.get("name", key)
        LOG.info("[YTCOL] Fetching %s...", name)
        raw = fetch_url(url)
        if not raw:
            LOG.warning("[YTCOL] Fetch failed for %s", name)
            continue

        items = parse_rss(raw, name, max_age_hours, {})
        items = items[:max_items]

        update_entry(cache, key, items)
        cache_updated = True
        LOG.info("[YTCOL] Cached %d item(s) for %s", len(items), name)

    if cache_updated:
        pruned = prune_cache(cache, max_age_hours=max_age_hours)
        save_cache(pruned)
        LOG.info("[YTCOL] Cache saved (%d sources)", len(pruned))
    else:
        LOG.info("[YTCOL] No updates required. Cache unchanged.")

    return 0


if __name__ == "__main__":
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
            handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
        )
    else:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        root.addHandler(stream)
        root.setLevel(logging.INFO)
    raise SystemExit(main(sys.argv[1:]))

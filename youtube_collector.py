from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import logging
import random
import sys
import time
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import parse_qs, urlparse

from yt_dlp import YoutubeDL

from asx_digest import LOG_FILE, load_config  # type: ignore
from youtube_cache import (
    DEFAULT_FRESH_MINUTES,
    is_fresh,
    load_cache,
    prune_cache,
    save_cache,
    update_entry,
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


def build_channel_urls(feed_url: str) -> list[str]:
    if not feed_url:
        return []

    parsed = urlparse(feed_url)
    qs = parse_qs(parsed.query)

    urls: list[str] = []
    channel_id = qs.get("channel_id", [None])[0]
    if channel_id:
        urls.append(f"https://www.youtube.com/channel/{channel_id}/videos")
        if channel_id.startswith("UC") and len(channel_id) > 2:
            uploads_id = "UU" + channel_id[2:]
            urls.append(f"https://www.youtube.com/playlist?list={uploads_id}")

    playlist_id = qs.get("playlist_id", [None])[0]
    if playlist_id:
        urls.append(f"https://www.youtube.com/playlist?list={playlist_id}")

    urls.append(feed_url)

    unique_urls: list[str] = []
    for candidate in urls:
        if candidate and candidate not in unique_urls:
            unique_urls.append(candidate)
    return unique_urls


def fetch_channel_items(feed_url: str, source_name: str, max_items: int) -> list[dict]:
    channel_urls = build_channel_urls(feed_url)
    if not channel_urls:
        return []

    ydl_opts = {
        "quiet": True,
        "skip_download": True,
        "extract_flat": False,
        "playlistend": max_items,
        "ignoreerrors": True,
        "noplaylist": False,
        "nocheckcertificate": True,
        "retries": 2,
        "socket_timeout": 15,
        "http_timeout": 15,
    }

    entries: list[dict] = []
    info = None
    last_error: Exception | None = None

    with YoutubeDL(ydl_opts) as ydl:
        for channel_url in channel_urls:
            try:
                info = ydl.extract_info(channel_url, download=False)
            except Exception as exc:  # noqa: BLE001
                LOG.warning(
                    "[YTCOL] yt-dlp error for %s (%s): %s",
                    source_name,
                    channel_url,
                    exc,
                )
                last_error = exc
                continue

            if isinstance(info, dict):
                entries = info.get("entries") or []
                if entries:
                    break
            elif isinstance(info, list):
                entries = info
                if entries:
                    break

    if not entries:
        if last_error:
            LOG.warning("[YTCOL] yt-dlp failed to fetch entries for %s after %d attempt(s)", source_name, len(channel_urls))
        return []

    items: list[dict] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue

        video_url = entry.get("webpage_url") or entry.get("url")
        if not video_url:
            video_id = entry.get("id")
            if video_id:
                video_url = f"https://www.youtube.com/watch?v={video_id}"
        if not video_url:
            continue

        title = entry.get("title") or ""
        description = (entry.get("description") or "")[:1000]

        timestamp = entry.get("timestamp") or entry.get("release_timestamp")
        upload_date = entry.get("upload_date") or entry.get("release_date")
        pub_iso = None
        if timestamp:
            try:
                dt = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
                pub_iso = dt.isoformat()
            except (TypeError, ValueError, OSError):
                pub_iso = None
        if not pub_iso and upload_date and len(str(upload_date)) == 8:
            try:
                dt = datetime.strptime(str(upload_date), "%Y%m%d").replace(tzinfo=timezone.utc)
                pub_iso = dt.isoformat()
            except ValueError:
                pub_iso = None

        item_id = hashlib.md5((title + video_url).encode(), usedforsecurity=False).hexdigest()
        items.append(
            {
                "id": item_id,
                "title": title,
                "link": video_url,
                "description": description,
                "source": source_name,
                "pub_date": pub_iso,
            }
        )

        if len(items) >= max_items:
            break

    return items


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
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(fetch_channel_items, url, name, max_items)
                items = future.result(timeout=120)
        except concurrent.futures.TimeoutError:
            LOG.warning("[YTCOL] Timeout fetching %s after 120s — skipping", name)
            continue
        except Exception as exc:
            LOG.warning("[YTCOL] Error fetching %s: %s", name, exc)
            continue
        if not items:
            LOG.warning("[YTCOL] Fetch yielded no items for %s", name)
            continue

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

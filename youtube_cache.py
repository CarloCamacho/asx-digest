from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict

BASE_DIR = Path(__file__).parent
DEFAULT_CACHE_PATH = BASE_DIR / "data" / "youtube_cache.json"
DEFAULT_FRESH_MINUTES = 90


def _ensure_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def load_cache(path: str | os.PathLike[str] | None = None) -> Dict[str, Any]:
    cache_path = Path(path) if path else DEFAULT_CACHE_PATH
    if not cache_path.exists():
        return {}
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_cache(cache: Dict[str, Any], path: str | os.PathLike[str] | None = None) -> None:
    cache_path = Path(path) if path else DEFAULT_CACHE_PATH
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as tmp:
        json.dump(cache, tmp, indent=2, ensure_ascii=False)
        tmp.write("\n")
    os.replace(tmp_path, cache_path)


def is_fresh(entry: Dict[str, Any] | None, max_age_minutes: int = DEFAULT_FRESH_MINUTES) -> bool:
    if not entry or "fetched_at" not in entry:
        return False
    fetched_at = _ensure_datetime(entry.get("fetched_at"))
    if not fetched_at:
        return False
    age = datetime.now(timezone.utc) - fetched_at
    return age <= timedelta(minutes=max_age_minutes)


def update_entry(cache: Dict[str, Any], source_key: str, items: list[dict[str, Any]]) -> None:
    cache[source_key] = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
    }


def prune_cache(cache: Dict[str, Any], max_age_hours: int) -> Dict[str, Any]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    pruned: Dict[str, Any] = {}
    for key, entry in cache.items():
        fetched_at = _ensure_datetime(entry.get("fetched_at"))
        if fetched_at and fetched_at >= cutoff:
            pruned[key] = entry
    return pruned

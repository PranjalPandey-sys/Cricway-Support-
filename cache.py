"""In-memory caching layer — the heart of the performance work.

Three caches live here:
  1. STATIC_SCREENS  — pre-rendered text for every static page (built once)
  2. PHOTO_FILE_IDS  — Telegram file_id by image filename (avoids re-upload)
  3. TTLCache        — generic time-bounded cache (status, cricket API, AI replies)

Plus an in-flight dedupe helper for AI calls so N identical concurrent
requests collapse into 1 upstream call.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Optional


# ---------------------------------------------------------------------------
# 1. Static screen cache  (pre-built ONCE at startup, never recomputed)
# ---------------------------------------------------------------------------

# Filled by bot.py at module import time.
STATIC_SCREENS: dict[str, str] = {}


def register_static(name: str, text: str) -> None:
    STATIC_SCREENS[name] = text


def get_static(name: str) -> Optional[str]:
    return STATIC_SCREENS.get(name)


# ---------------------------------------------------------------------------
# 2. Telegram photo file_id cache
# ---------------------------------------------------------------------------
#
# After the first send_photo() of a local file, Telegram returns a file_id.
# Reusing that file_id on subsequent sends takes ~50–100ms instead of a multi-
# hundred-KB upload + processing time. This is the single biggest UX win.

PHOTO_FILE_IDS: dict[str, str] = {}


def remember_photo(name: str, file_id: str) -> None:
    PHOTO_FILE_IDS[name] = file_id


def get_photo_id(name: str) -> Optional[str]:
    return PHOTO_FILE_IDS.get(name)


# ---------------------------------------------------------------------------
# 3. Generic TTL cache
# ---------------------------------------------------------------------------


class TTLCache:
    """Tiny LRU + TTL cache. No external deps."""

    __slots__ = ("_data", "_ttl", "_max")

    def __init__(self, ttl_seconds: float = 60.0, max_items: int = 512) -> None:
        self._data: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()
        self._ttl = ttl_seconds
        self._max = max_items

    def get(self, key: str) -> Optional[Any]:
        item = self._data.get(key)
        if not item:
            return None
        expires, value = item
        if expires < time.monotonic():
            self._data.pop(key, None)
            return None
        # touch → LRU bump
        self._data.move_to_end(key)
        return value

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        ttl = ttl if ttl is not None else self._ttl
        self._data[key] = (time.monotonic() + ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    def invalidate(self, key: str) -> None:
        self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()


# Pre-instantiated caches the rest of the app shares.
STATUS_CACHE = TTLCache(ttl_seconds=30.0, max_items=4)        # /status payload
CRICKET_CACHE = TTLCache(ttl_seconds=45.0, max_items=64)      # cricket API
AI_REPLY_CACHE = TTLCache(ttl_seconds=300.0, max_items=512)   # AI dedupe
TICKETS_LIST_CACHE = TTLCache(ttl_seconds=10.0, max_items=512)  # per-user tix


def hash_text(text: str) -> str:
    """Stable, normalized cache key for free-form user text."""
    return hashlib.sha1(text.lower().strip().encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 4. In-flight dedupe — collapse concurrent identical requests
# ---------------------------------------------------------------------------


class InflightDedupe:
    """If 5 users hit the same AI prompt at once, run it ONCE."""

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    async def run(
        self,
        key: str,
        producer: Callable[[], Awaitable[Any]],
    ) -> Any:
        async with self._lock:
            existing = self._pending.get(key)
            if existing is not None:
                return await existing
            loop = asyncio.get_running_loop()
            fut: asyncio.Future = loop.create_future()
            self._pending[key] = fut

        try:
            result = await producer()
            if not fut.done():
                fut.set_result(result)
            return result
        except Exception as exc:  # noqa: BLE001
            if not fut.done():
                fut.set_exception(exc)
            raise
        finally:
            self._pending.pop(key, None)


AI_INFLIGHT = InflightDedupe()

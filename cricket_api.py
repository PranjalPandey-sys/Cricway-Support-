"""Cricket API client with TTL caching + in-flight dedupe.

Drop-in pattern: all reads go through `get_cached(...)` so identical bursts
collapse to a single upstream call and repeat hits return instantly.

The current bot does not call a cricket API; this module is here as the
correct shape so when you wire it in, performance is already solved.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx

from cache import AI_INFLIGHT, CRICKET_CACHE

logger = logging.getLogger(__name__)

CRICKET_API_BASE = os.environ.get("CRICKET_API_BASE", "")
CRICKET_API_KEY = os.environ.get("CRICKET_API_KEY", "")
CRICKET_TIMEOUT = float(os.environ.get("CRICKET_API_TIMEOUT", "6.0"))

_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=CRICKET_API_BASE,
            timeout=CRICKET_TIMEOUT,
            headers={"x-api-key": CRICKET_API_KEY} if CRICKET_API_KEY else {},
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
    return _client


async def get_cached(path: str, params: Optional[dict] = None, ttl: float = 45.0) -> Any:
    """GET an endpoint, returning cached JSON if fresh (< `ttl` seconds old)."""
    cache_key = f"{path}?{sorted((params or {}).items())}"
    hit = CRICKET_CACHE.get(cache_key)
    if hit is not None:
        return hit

    async def _fetch() -> Any:
        if not CRICKET_API_BASE:
            return {"error": "cricket_api_not_configured"}
        try:
            r = await _get_client().get(path, params=params)
            r.raise_for_status()
            data = r.json()
            CRICKET_CACHE.set(cache_key, data, ttl=ttl)
            return data
        except Exception as exc:  # noqa: BLE001
            logger.warning("Cricket API call %s failed: %s", path, exc)
            stale = CRICKET_CACHE.get(cache_key)  # last-resort: return stale
            return stale if stale is not None else {"error": str(exc)}

    return await AI_INFLIGHT.run(f"cricket:{cache_key}", _fetch)


async def shutdown() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None

"""LiteLLM response-cache setup and helpers.

The cache is LiteLLM's built-in in-memory cache (`litellm.Cache(type="local")`).
LiteLLM derives the cache key from the completion arguments - the model name and the
complete `messages` list (system prompt + prior history + current user message) plus
generation params. The API key is *not* part of the key, so a response produced by
key_2 during failover can later be served for the same prompt.

Cache hits are detected via `response._hidden_params["cache_hit"]`, which LiteLLM's
caching handler sets on every response it serves from the cache.
"""

from __future__ import annotations

import asyncio
import logging

import litellm
from litellm.caching.caching import Cache

logger = logging.getLogger(__name__)


def configure_cache(enabled: bool, ttl_seconds: int, max_items: int) -> bool:
    if not enabled:
        litellm.cache = None
        return False
    cache = Cache(type="local", default_in_memory_ttl=ttl_seconds)
    inner = getattr(cache, "cache", None)
    if inner is not None and hasattr(inner, "max_size_in_memory"):
        inner.max_size_in_memory = max_items
    litellm.cache = cache
    return True


def cache_is_enabled() -> bool:
    return litellm.cache is not None


def is_cache_hit(response: object) -> bool:
    hidden = getattr(response, "_hidden_params", None) or {}
    return hidden.get("cache_hit") is True


async def flush_pending_cache_writes(timeout: float = 2.0) -> None:
    """Wait for LiteLLM's background cache-write tasks.

    LiteLLM writes successful responses to the cache in a background asyncio task.
    Awaiting it before replying guarantees that an identical request sent right after
    this response is served from cache (no race between write and next read).
    """
    try:
        from litellm.caching import caching_handler

        pending = [t for t in getattr(caching_handler, "_PENDING_CACHE_WRITES", ()) if not t.done()]
    except Exception:  # pragma: no cover - internal API changed
        pending = []
    if pending:
        await asyncio.wait(pending, timeout=timeout)
    else:
        # Give a just-scheduled task one loop iteration to run.
        await asyncio.sleep(0)


async def prewarm_cache() -> None:
    """Exercise LiteLLM's cache write + cache-hit path once at startup.

    LiteLLM lazily imports and initialises a lot on the first cache hit (~100-300 ms).
    A mocked round-trip (no provider call, no API key) on a private sentinel prompt
    moves that cost to startup so the first real cache hit is as fast as later ones.
    """
    if litellm.cache is None:
        return
    messages = [{"role": "user", "content": "__llm_chatbot_cache_prewarm__"}]
    try:
        for _ in range(2):
            await litellm.acompletion(
                model="openai/cache-prewarm", messages=messages, mock_response="ok", caching=True
            )
            await flush_pending_cache_writes()
    except Exception as exc:  # pragma: no cover - warm-up is best effort
        logger.debug("cache prewarm skipped: %s", type(exc).__name__)

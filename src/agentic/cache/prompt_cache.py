"""Exact Match Prompt and Completion Cache with Multi-Tier (L1 In-Memory + L2 Distributed Redis) Architecture."""

import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from typing import Any

from src.agentic.core.config import get_settings
from src.agentic.observability.metrics import PROMPT_CACHE_HITS_TOTAL, PROMPT_CACHE_MISSES_TOTAL

logger = logging.getLogger(__name__)


class PromptCache:
    """Thread-safe Multi-Tier exact match response cache for LLM queries.

    Tier 1 (L1): In-memory LRU for low-latency local hits.
    Tier 2 (L2): Distributed Redis cache shared across multi-replica pods.
    """

    def __init__(
        self,
        max_entries: int = 2048,
        default_ttl: int = 3600,
        redis_url: str | None = None,
        enable_redis: bool = False,
    ) -> None:
        self.settings = get_settings()
        self.max_entries = max_entries
        self.default_ttl = default_ttl
        self._cache: OrderedDict[str, tuple[float, str, dict[str, Any]]] = OrderedDict()
        self._lock = threading.Lock()
        self._inflight_locks: set[str] = set()
        self._hits = 0
        self._misses = 0
        self._redis_client: Any = None

        target_redis_url = redis_url or self.settings.redis_url
        should_use_redis = enable_redis or self.settings.enable_redis_cache or self.settings.enable_redis_rate_limit

        if should_use_redis and target_redis_url:
            try:
                import redis
                client = redis.Redis.from_url(target_redis_url, socket_timeout=0.5, decode_responses=True)
                client.ping()
                self._redis_client = client
                logger.info(f"Initialized Distributed Redis L2 backend for PromptCache at {target_redis_url}")
            except Exception as e:
                logger.debug(f"Redis L2 backend not reachable for PromptCache ({e}). Operating in L1 local memory mode.")

    @staticmethod
    def _serialize_messages(messages: list[Any]) -> str:
        """Serializes messages to deterministic JSON."""
        clean_list: list[dict[str, str]] = []
        for m in messages:
            if hasattr(m, "type") and hasattr(m, "content"):
                clean_list.append({"role": str(m.type), "content": str(m.content)})
            elif isinstance(m, dict):
                clean_list.append({"role": str(m.get("role", "")), "content": str(m.get("content", ""))})
            else:
                clean_list.append({"role": "user", "content": str(m)})
        return json.dumps(clean_list, sort_keys=True, ensure_ascii=False)

    def _hash_key(self, model_alias: str, messages: list[Any], temperature: float) -> str:
        serialized = self._serialize_messages(messages)
        raw_key = f"{model_alias}:{temperature:.2f}:{serialized}"
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    def get_response(self, model_alias: str, messages: list[Any], temperature: float = 0.2) -> str | None:
        """Returns cached completion if hit and not expired (checking L1 then L2)."""
        if not self.settings.enable_prompt_cache:
            return None

        # Non-deterministic requests with high temperature should skip cache
        if temperature > 0.7:
            return None

        key = self._hash_key(model_alias, messages, temperature)
        now = time.time()

        # 1. Check L1 Memory
        with self._lock:
            if key in self._cache:
                expire_at, response, _ = self._cache[key]
                if now < expire_at:
                    self._cache.move_to_end(key)
                    self._hits += 1
                    PROMPT_CACHE_HITS_TOTAL.labels(model_alias=model_alias).inc()
                    logger.debug(f"Prompt Cache L1 HIT for model='{model_alias}' (key={key[:8]})")
                    return response
                else:
                    del self._cache[key]

        # 2. Check L2 Distributed Redis
        if self._redis_client is not None:
            try:
                redis_key = f"prompt_cache:{key}"
                cached_data = self._redis_client.get(redis_key)
                if cached_data is not None:
                    ttl_left = max(60, self._redis_client.ttl(redis_key))
                    with self._lock:
                        if len(self._cache) >= self.max_entries:
                            self._cache.popitem(last=False)
                        self._cache[key] = (now + ttl_left, cached_data, {})
                    self._hits += 1
                    PROMPT_CACHE_HITS_TOTAL.labels(model_alias=model_alias).inc()
                    logger.debug(f"Prompt Cache L2 (Redis) HIT for model='{model_alias}' (key={key[:8]})")
                    return cached_data
            except Exception as ex:
                logger.debug(f"Redis L2 read exception in PromptCache: {ex}")

        with self._lock:
            self._misses += 1
        PROMPT_CACHE_MISSES_TOTAL.labels(model_alias=model_alias).inc()
        return None

    def set_response(
        self,
        model_alias: str,
        messages: list[Any],
        temperature: float,
        response: str,
        ttl: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Stores LLM response into both L1 memory and L2 distributed Redis."""
        if not self.settings.enable_prompt_cache or temperature > 0.7:
            return

        key = self._hash_key(model_alias, messages, temperature)
        effective_ttl = ttl if ttl is not None else self.settings.prompt_cache_ttl_seconds
        expire_at = time.time() + effective_ttl

        # 1. Set in L1 In-Memory Cache
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            elif len(self._cache) >= self.max_entries:
                self._cache.popitem(last=False)
            self._cache[key] = (expire_at, response, metadata or {})

        # 2. Set in L2 Distributed Redis Cache
        if self._redis_client is not None:
            try:
                redis_key = f"prompt_cache:{key}"
                self._redis_client.set(redis_key, response, ex=effective_ttl)
                logger.debug(f"Prompt Cache L2 (Redis) SET for model='{model_alias}' (TTL={effective_ttl}s)")
            except Exception as ex:
                logger.debug(f"Redis L2 write exception in PromptCache: {ex}")

    def acquire_lock(self, key: str, timeout: float = 5.0) -> bool:
        """Acquires a distributed Redis or in-memory lock for a key to prevent cache stampede."""
        if self._redis_client is not None:
            try:
                acquired = bool(self._redis_client.set(f"lock:prompt:{key}", "1", nx=True, ex=max(1, int(timeout))))
                return acquired
            except Exception:
                pass
        with self._lock:
            if key in self._inflight_locks:
                return False
            self._inflight_locks.add(key)
            return True

    def release_lock(self, key: str) -> None:
        """Releases the stampede lock for a key."""
        if self._redis_client is not None:
            try:
                self._redis_client.delete(f"lock:prompt:{key}")
            except Exception:
                pass
        with self._lock:
            self._inflight_locks.discard(key)

    async def get_or_compute_async(
        self,
        model_alias: str,
        messages: list[Any],
        compute_fn: Any,
        temperature: float = 0.2,
        ttl: int | None = None,
        timeout: float = 5.0,
    ) -> str:
        """Async stampede-guarded completion generation."""
        cached = self.get_response(model_alias, messages, temperature)
        if cached is not None:
            return cached

        key = self._hash_key(model_alias, messages, temperature)
        has_lock = self.acquire_lock(key, timeout=timeout)

        if has_lock:
            try:
                import inspect
                if inspect.iscoroutinefunction(compute_fn):
                    computed = await compute_fn()
                else:
                    computed = compute_fn()
                self.set_response(model_alias, messages, temperature, str(computed), ttl=ttl)
                return str(computed)
            finally:
                self.release_lock(key)
        else:
            import asyncio
            start_wait = time.time()
            while time.time() - start_wait < timeout:
                await asyncio.sleep(0.05)
                cached = self.get_response(model_alias, messages, temperature)
                if cached is not None:
                    return cached
            import inspect
            if inspect.iscoroutinefunction(compute_fn):
                return str(await compute_fn())
            return str(compute_fn())

    def clear(self) -> None:
        """Flushes all cached entries from L1 and L2."""
        with self._lock:
            self._cache.clear()
        if self._redis_client is not None:
            try:
                keys = self._redis_client.keys("prompt_cache:*")
                if keys:
                    self._redis_client.delete(*keys)
            except Exception as ex:
                logger.debug(f"Redis L2 clear exception: {ex}")

    @property
    def stats(self) -> dict[str, Any]:
        """Returns cache metrics."""
        with self._lock:
            total = self._hits + self._misses
            hit_rate = (self._hits / total) if total > 0 else 0.0
            return {
                "size": len(self._cache),
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(hit_rate, 4),
                "has_redis_backend": self._redis_client is not None,
            }


_prompt_cache_instance: PromptCache | None = None


def get_prompt_cache() -> PromptCache:
    """Returns singleton PromptCache instance."""
    global _prompt_cache_instance
    if _prompt_cache_instance is None:
        settings = get_settings()
        _prompt_cache_instance = PromptCache(
            max_entries=settings.cache_max_entries,
            default_ttl=settings.prompt_cache_ttl_seconds,
            redis_url=settings.redis_url,
            enable_redis=settings.enable_redis_cache,
        )
    return _prompt_cache_instance

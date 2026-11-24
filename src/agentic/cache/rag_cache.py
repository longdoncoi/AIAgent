"""RAG Query and Semantic Retrieval Cache with Multi-Tier (L1 In-Memory + L2 Distributed Redis) Architecture.

Cross-replica invalidation
--------------------------
``publish_invalidation(doc_id, file_path)`` atomically bumps a shared Redis
generation key and publishes the event on a pub/sub channel. It never raises:
when Redis is unavailable it logs a warning and returns ``None``.

Other replicas can react in either of two ways:

* Poll ``check_invalidation_generation()`` (e.g. from a background task). It
  clears that pod's L1 cache and invokes callbacks registered with
  ``register_invalidation_callback()`` so a process-local retriever can be
  rebuilt.
* Subscribe directly to ``RAG_INVALIDATION_CHANNEL`` with redis pub/sub.

The caller in the same process should use ``invalidate_by_source`` /
``invalidate_or_noop`` (never ``clear``) for its own L1/L2 entries.
"""

import asyncio
import hashlib
import json
import logging
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from typing import Any

from src.agentic.cache.locking import (
    acquire_owned_lock,
    desynchronize_waiters,
    release_owned_lock,
)
from src.agentic.cache.redaction import redact_pii
from src.agentic.core.config import get_settings
from src.agentic.observability.metrics import RAG_CACHE_HITS_TOTAL, RAG_CACHE_MISSES_TOTAL

logger = logging.getLogger(__name__)

RAG_INVALIDATION_CHANNEL = "rag_cache:invalidation"
RAG_INVALIDATION_GENERATION_KEY = "rag_cache:invalidation:generation"
RAG_INVALIDATION_LATEST_KEY = "rag_cache:invalidation:latest"
RAG_CACHE_KEY_PREFIX = "rag_cache:"
RAG_LOCK_PREFIX = "lock:rag:"


class RAGCache:
    """Thread-safe Multi-Tier TTL/LRU Cache for RAG retrieval results.

    Tier 1 (L1): Local in-memory LRU cache for ultra-fast pod-local hit.
    Tier 2 (L2): Distributed Redis cache shared across multi-replica Kubernetes clusters.
    """

    def __init__(
        self,
        max_entries: int = 2048,
        default_ttl: int = 3600,
        redis_url: str | None = None,
        enable_redis: bool = False,
        embed_fn: Any | None = None,
    ) -> None:
        self.settings = get_settings()
        self.max_entries = max_entries
        self.default_ttl = default_ttl
        self._cache: OrderedDict[str, tuple[float, str, dict[str, Any]]] = OrderedDict()
        self._semantic_entries: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._inflight_locks: set[str] = set()
        self._hits = 0
        self._misses = 0
        self._semantic_hits = 0
        self._redis_client: Any = None
        self._embed_fn: Any = embed_fn or self._resolve_default_embed_fn()
        self._invalidation_generation = 0
        self._invalidation_callbacks: list[Callable[[str, str, int], None]] = []
        self._lock_tokens: dict[str, str] = {}

        target_redis_url = redis_url or self.settings.redis_url
        should_use_redis = (
            enable_redis
            or self.settings.enable_redis_cache
            or self.settings.enable_redis_rate_limit
        )

        if should_use_redis and target_redis_url:
            try:
                from src.agentic.core.redis import get_redis_client

                client = get_redis_client(target_redis_url)
                if client is None:
                    import redis

                    client = redis.Redis.from_url(
                        target_redis_url, socket_timeout=0.5, decode_responses=True
                    )
                client.ping()
                self._redis_client = client
                logger.info(
                    f"Initialized Distributed Redis L2 backend for RAGCache at {target_redis_url}"
                )
            except Exception as e:
                logger.warning(
                    f"Redis L2 backend not reachable for RAGCache ({e}). Operating in L1 local memory mode."
                )

        # A restarted replica must inherit the shared generation so values
        # written by an older process are recognized as stale immediately.
        if self._redis_client is not None:
            try:
                raw_generation = self._redis_client.get(RAG_INVALIDATION_GENERATION_KEY)
                if raw_generation is not None:
                    self._invalidation_generation = int(self._as_text(raw_generation).strip())
            except Exception as ex:  # pragma: no cover - best effort
                logger.debug("Could not read initial RAG invalidation generation: %s", ex)

    @staticmethod
    def _normalize_query(query: str) -> str:
        """Normalizes query text for deterministic hashing."""
        return " ".join(query.strip().lower().split())

    @staticmethod
    def _as_text(value: Any) -> str:
        """Decodes Redis values regardless of the client's decode_responses setting."""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        if isinstance(value, (bytearray, memoryview)):
            return bytes(value).decode("utf-8", errors="replace")
        return str(value)

    def _iter_redis_keys(self, pattern: str) -> Iterator[Any]:
        """Non-blocking SCAN_ITER over L2 keys; never issue KEYS on the hot path."""
        client = self._redis_client
        if client is None:
            return
        scan_iter = getattr(client, "scan_iter", None)
        if callable(scan_iter):
            yield from scan_iter(match=pattern, count=500)
        else:  # pragma: no cover - only legacy/minimal Redis clients
            yield from client.keys(pattern)

    @staticmethod
    def _is_value_key(key: Any) -> bool:
        """True for a cached-value key, excluding invalidation metadata keys."""
        text = RAGCache._as_text(key)
        return text.startswith(RAG_CACHE_KEY_PREFIX) and "invalidation:" not in text

    def _hash_key(self, query: str, top_k: int, **kwargs: Any) -> str:
        norm = self._normalize_query(query)
        meaningful = {key: value for key, value in kwargs.items() if value not in (None, "")}
        # ``domain`` is part of the key: a "general"/chat entry must never be
        # served for legal/finance/security queries. The full set of kwargs
        # (notably ``scope``, which carries the authoritative tenant) is
        # folded in as well, so identical text for two tenants never collides.
        extra = f":k={top_k}:" + ":".join(
            f"{key}={value}" for key, value in sorted(meaningful.items())
        )
        return hashlib.sha256((norm + extra).encode("utf-8")).hexdigest()

    def _current_generation(self) -> int:
        """Read the shared generation from L2, falling back to the local value."""
        if self._redis_client is not None:
            try:
                raw = self._redis_client.get(RAG_INVALIDATION_GENERATION_KEY)
                if raw is not None:
                    return int(self._as_text(raw).strip())
            except Exception as ex:  # pragma: no cover - best effort
                logger.debug("Could not read RAG invalidation generation: %s", ex)
        return self._invalidation_generation

    @staticmethod
    def _redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
        """Redact free-form result/document text before any L2 write."""
        redacted = dict(payload)
        if isinstance(redacted.get("result"), str):
            redacted["result"] = redact_pii(redacted["result"])
        if isinstance(redacted.get("query"), str):
            redacted["query"] = redact_pii(redacted["query"])
        return redacted

    def get(self, query: str, top_k: int = 5, **kwargs: Any) -> str | None:
        """Retrieves cached result if still valid (checking L1 memory then L2 Redis)."""
        if not self.settings.enable_rag_cache:
            return None

        kwargs.setdefault("domain", "general")
        key = self._hash_key(query, top_k, **kwargs)
        now = time.time()

        # 1. Check L1 In-Memory Cache
        with self._lock:
            if key in self._cache:
                expire_at, value, meta = self._cache[key]
                if now < expire_at:
                    self._cache.move_to_end(key)
                    self._hits += 1
                    RAG_CACHE_HITS_TOTAL.inc()
                    logger.debug("RAG Cache L1 HIT (key=%s)", key[:8])
                    return value
                else:
                    del self._cache[key]

        # 2. Check L2 Distributed Redis Cache
        if self._redis_client is not None:
            try:
                redis_key = RAG_CACHE_KEY_PREFIX + key
                cached_data = self._redis_client.get(redis_key)
                if cached_data is not None:
                    # Deserialize and populate back into L1
                    text_data = self._as_text(cached_data)
                    payload = (
                        json.loads(text_data)
                        if text_data.startswith("{")
                        else {"result": text_data}
                    )
                    # Generation embedded in the value is the authoritative
                    # freshness check: if a remote invalidation advanced the
                    # generation after this value was written, ignore it even
                    # when its key survived a partial deletion.
                    cached_generation = payload.get("generation")
                    if (
                        isinstance(cached_generation, int)
                        and cached_generation < self._current_generation()
                    ):
                        self._redis_client.delete(redis_key)
                    else:
                        result_val = str(payload.get("result", text_data))
                        meta_raw = payload.get("metadata", {})
                        meta_val = meta_raw if isinstance(meta_raw, dict) else {}
                        ttl_left = max(60, int(self._redis_client.ttl(redis_key)))
                        with self._lock:
                            if len(self._cache) >= self.max_entries:
                                self._cache.popitem(last=False)
                            self._cache[key] = (now + ttl_left, result_val, meta_val)
                        self._hits += 1
                        RAG_CACHE_HITS_TOTAL.inc()
                        logger.debug("RAG Cache L2 (Redis) HIT (key=%s)", key[:8])
                        return str(result_val)
            except Exception as ex:
                logger.debug("Redis L2 read exception in RAGCache: %s", ex)

        with self._lock:
            self._misses += 1
        RAG_CACHE_MISSES_TOTAL.inc()
        return None

    def set(
        self,
        query: str,
        result: str,
        top_k: int = 5,
        ttl: int | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Stores retrieval result in both L1 memory and L2 distributed Redis."""
        if not self.settings.enable_rag_cache:
            return

        kwargs.setdefault("domain", "general")
        key = self._hash_key(query, top_k, **kwargs)
        effective_ttl = ttl if ttl is not None else self.settings.rag_cache_ttl_seconds
        expire_at = time.time() + effective_ttl

        effective_metadata = dict(metadata or {})
        # Store the authoritative scope alongside the value so RTBF can
        # selectively erase every cache record belonging to one tenant.
        effective_metadata["_scope"] = str(kwargs.get("scope", ""))

        # 1. Set in L1 In-Memory Cache
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            elif len(self._cache) >= self.max_entries:
                self._cache.popitem(last=False)
            self._cache[key] = (expire_at, result, effective_metadata)

        # 2. Set in L2 Distributed Redis Cache. Never persist raw retrieved
        #    document text or PII into the shared Redis instance.
        if self._redis_client is not None:
            try:
                redis_key = RAG_CACHE_KEY_PREFIX + key
                payload = self._redact_payload(
                    {
                        "result": result,
                        "metadata": effective_metadata,
                        "generation": self._current_generation(),
                    }
                )
                self._redis_client.set(
                    redis_key, json.dumps(payload, ensure_ascii=False, default=str),
                    ex=effective_ttl,
                )
                logger.debug("RAG Cache L2 (Redis) SET (key=%s, TTL=%ss)", key[:8], effective_ttl)
            except Exception as ex:
                logger.debug("Redis L2 write exception in RAGCache: %s", ex)

    def get_domain_threshold(self, domain: str = "general") -> float:
        """Retrieves domain-adaptive cosine similarity threshold.

        Higher thresholds (e.g. 0.96-0.97) for sensitive/legal/finance domains;
        Lower thresholds (e.g. 0.88-0.90) for chat/general domains.
        """
        domain_clean = domain.lower().strip()
        return self.settings.semantic_cache_domain_thresholds.get(
            domain_clean,
            self.settings.semantic_cache_default_threshold,
        )

    def _is_sensitive_domain(self, domain: str) -> bool:
        """Whether a domain has a dedicated high-assurance similarity threshold."""
        return self.get_domain_threshold(domain) >= 0.93

    @staticmethod
    def _compute_similarity(vec1: list[float], vec2: list[float]) -> float:
        """Computes cosine similarity between two dense vectors."""
        if not vec1 or not vec2 or len(vec1) != len(vec2):
            return 0.0
        dot = sum(a * b for a, b in zip(vec1, vec2))
        norm1 = math.sqrt(sum(a * a for a in vec1))
        norm2 = math.sqrt(sum(b * b for b in vec2))
        if norm1 == 0.0 or norm2 == 0.0:
            return 0.0
        return dot / (norm1 * norm2)

    def _generate_text_vector(self, text: str, dim: int = 128) -> list[float]:
        """Generates a deterministic pseudo-dense vector for query semantic comparison."""
        vec = [0.0] * dim
        words = text.lower().split()
        if not words:
            return vec
        for w in words:
            grams = [w[i : i + 3] for i in range(max(1, len(w) - 2))]
            for g in grams:
                idx = int(hashlib.md5(g.encode("utf-8")).hexdigest()[:8], 16) % dim
                vec[idx] += 1.0
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0.0:
            vec = [x / norm for x in vec]
        return vec

    def _resolve_default_embed_fn(self) -> Any | None:
        """Attempts to discover active neural embedding provider if available."""
        try:
            from llama_index.core import Settings as LlamaSettings

            if hasattr(LlamaSettings, "embed_model") and LlamaSettings.embed_model is not None:
                embed_model = LlamaSettings.embed_model
                class_name = embed_model.__class__.__name__.lower()
                embed_name = str(getattr(embed_model, "model_name", "")).lower()
                if (
                    "mock" not in class_name
                    and "mock" not in embed_name
                    and embed_name not in ("", "unknown")
                    and not type(embed_model).__module__.startswith("unittest.mock")
                ):
                    return lambda t: embed_model.get_text_embedding(t)
        except Exception:
            pass
        return None

    def set_embed_fn(self, embed_fn: Any) -> None:
        """Sets or updates the dense embedding function used for semantic vector extraction."""
        self._embed_fn = embed_fn

    def get_embedding(self, text: str) -> list[float]:
        """Generates embedding vector for query comparison.

        Uses registered neural embed_fn if available, otherwise falls back to deterministic n-gram vector.
        """
        if self._embed_fn is not None:
            try:
                vec = self._embed_fn(text)
                if isinstance(vec, (list, tuple)) and len(vec) > 0:
                    return list(vec)
            except Exception as e:
                logger.debug(
                    f"Custom embed_fn call failed ({e}). Falling back to pseudo-dense vector."
                )
        return self._generate_text_vector(text)

    async def aget_embedding(self, text: str) -> list[float]:
        """Asynchronously generates embedding vector for query comparison."""
        if self._embed_fn is not None:
            try:
                import inspect

                if inspect.iscoroutinefunction(self._embed_fn):
                    vec = await self._embed_fn(text)
                else:
                    vec = await asyncio.to_thread(self._embed_fn, text)
                if isinstance(vec, (list, tuple)) and len(vec) > 0:
                    return list(vec)
            except Exception as e:
                logger.debug(
                    f"Custom async embed_fn call failed ({e}). Falling back to pseudo-dense vector."
                )
        return self._generate_text_vector(text)

    def get_semantic(
        self,
        query: str,
        domain: str = "general",
        top_k: int = 5,
        query_vector: list[float] | None = None,
        **kwargs: Any,
    ) -> str | None:
        """Retrieves cached result matching semantic similarity >= domain threshold."""
        if not self.settings.enable_rag_cache:
            return None

        # 1. Exact match check first (domain is part of the key).
        exact_hit = self.get(query, top_k=top_k, domain=domain, **kwargs)
        if exact_hit is not None:
            return exact_hit

        if not self.settings.enable_semantic_cache:
            return None

        now = time.time()
        threshold = self.get_domain_threshold(domain)
        target_vec = query_vector if query_vector is not None else self.get_embedding(query)

        with self._lock:
            # Purge expired semantic entries
            self._semantic_entries = [e for e in self._semantic_entries if e["expire_at"] > now]
            best_candidate: dict[str, Any] | None = None
            best_score = 0.0

            for entry in self._semantic_entries:
                if entry.get("top_k", top_k) != top_k:
                    continue
                if entry.get("scope", "") != str(kwargs.get("scope", "")):
                    continue
                entry_domain = entry.get("domain", "general")
                # Sensitive domains (legal/finance/security/...) must match
                # EXACTLY: a "general" entry is never reused for them. This
                # closes the cross-domain cache bleed that let a low-threshold
                # general answer serve a high-assurance policy query.
                if entry_domain != domain and (
                    self._is_sensitive_domain(domain)
                    or self._is_sensitive_domain(entry_domain)
                    or domain not in ("general", "chat")
                ):
                    continue
                sim = self._compute_similarity(target_vec, entry["vector"])
                if sim >= threshold and sim > best_score:
                    best_score = sim
                    best_candidate = entry

            if best_candidate is not None:
                self._hits += 1
                self._semantic_hits += 1
                RAG_CACHE_HITS_TOTAL.inc()
                logger.debug(
                    f"RAG Cache Semantic HIT for domain='{domain}' "
                    f"(score={best_score:.4f} >= threshold={threshold:.2f})"
                )
                return str(best_candidate["result"])

        return None

    def set_semantic(
        self,
        query: str,
        result: str,
        domain: str = "general",
        top_k: int = 5,
        query_vector: list[float] | None = None,
        ttl: int | None = None,
        metadata: dict[str, Any] | None = None,
        scope: str = "",
    ) -> None:
        """Stores query result in both exact match cache and semantic entry list."""
        self.set(
            query, result, top_k=top_k, ttl=ttl, metadata=metadata, scope=scope, domain=domain
        )
        if not self.settings.enable_semantic_cache:
            return

        effective_ttl = ttl if ttl is not None else self.settings.rag_cache_ttl_seconds
        expire_at = time.time() + effective_ttl
        target_vec = query_vector if query_vector is not None else self.get_embedding(query)

        entry = {
            "query": query,
            "vector": target_vec,
            "domain": domain.lower().strip(),
            "result": result,
            "top_k": top_k,
            "expire_at": expire_at,
            "metadata": metadata or {},
            "scope": scope,
        }

        with self._lock:
            if len(self._semantic_entries) >= self.max_entries:
                self._semantic_entries.pop(0)
            self._semantic_entries.append(entry)

    def acquire_lock(self, key: str, timeout: float = 5.0) -> bool:
        """Acquires a distributed Redis or in-memory lock for a key to prevent cache stampede.

        The Redis lock carries a unique ownership token so a slow computation
        that outlives its TTL can never delete another request's lock.
        """
        token = acquire_owned_lock(
            self._redis_client, RAG_LOCK_PREFIX, key, timeout, self._inflight_locks, self._lock
        )
        if token is None:
            return False
        with self._lock:
            self._lock_tokens[key] = token
        return True

    def release_lock(self, key: str) -> None:
        """Releases the stampede lock for a key (only if still owned by us)."""
        with self._lock:
            token = self._lock_tokens.pop(key, None)
        release_owned_lock(
            self._redis_client, RAG_LOCK_PREFIX, key, token, self._inflight_locks, self._lock
        )

    async def get_or_compute_async(
        self,
        query: str,
        compute_fn: Any,
        top_k: int = 5,
        ttl: int | None = None,
        timeout: float = 5.0,
        domain: str = "general",
        query_vector: list[float] | None = None,
        scope: str = "",
    ) -> str:
        """Stampede-guarded async retrieval with adaptive semantic caching."""
        target_vec = query_vector
        if target_vec is None:
            target_vec = await self.aget_embedding(query)

        cached = await asyncio.to_thread(
            self.get_semantic,
            query,
            domain,
            top_k,
            target_vec,
            scope=scope,
        )
        if cached is not None:
            return cached

        key = self._hash_key(query, top_k, scope=scope)
        has_lock = await asyncio.to_thread(self.acquire_lock, key, timeout)

        if has_lock:
            try:
                import inspect

                if inspect.iscoroutinefunction(compute_fn):
                    computed = await compute_fn()
                else:
                    computed = await asyncio.to_thread(compute_fn)
                await asyncio.to_thread(
                    self.set_semantic,
                    query,
                    str(computed),
                    domain,
                    top_k,
                    target_vec,
                    ttl,
                    None,
                    scope,
                )
                return str(computed)
            finally:
                await asyncio.to_thread(self.release_lock, key)
        else:
            start_wait = time.time()
            while time.time() - start_wait < timeout:
                await asyncio.sleep(0.05)
                cached = await asyncio.to_thread(
                    self.get_semantic,
                    query,
                    domain,
                    top_k,
                    target_vec,
                    scope=scope,
                )
                if cached is not None:
                    return cached
            # Jitter prevents every timed-out waiter from recomputing in lockstep.
            desynchronize_waiters()
            import inspect

            if inspect.iscoroutinefunction(compute_fn):
                return str(await compute_fn())
            return str(await asyncio.to_thread(compute_fn))

    def get_or_compute(
        self,
        query: str,
        compute_fn: Any,
        top_k: int = 5,
        ttl: int | None = None,
        timeout: float = 5.0,
        domain: str = "general",
        query_vector: list[float] | None = None,
        scope: str = "",
    ) -> str:
        """Synchronous stampede-guarded retrieval with adaptive semantic caching."""
        target_vec = query_vector
        if target_vec is None:
            target_vec = self.get_embedding(query)

        cached = self.get_semantic(
            query, domain=domain, top_k=top_k, query_vector=target_vec, scope=scope
        )
        if cached is not None:
            return cached

        key = self._hash_key(query, top_k, scope=scope)
        has_lock = self.acquire_lock(key, timeout=timeout)

        if has_lock:
            try:
                computed = str(compute_fn())
                self.set_semantic(
                    query,
                    computed,
                    domain=domain,
                    top_k=top_k,
                    query_vector=target_vec,
                    ttl=ttl,
                    scope=scope,
                )
                return computed
            finally:
                self.release_lock(key)
        else:
            start_wait = time.time()
            while time.time() - start_wait < timeout:
                time.sleep(0.05)
                cached = self.get_semantic(
                    query,
                    domain=domain,
                    top_k=top_k,
                    query_vector=target_vec,
                    scope=scope,
                )
                if cached is not None:
                    return cached
            desynchronize_waiters()
            return str(compute_fn())

    @staticmethod
    def _source_matches(
        norm_source: str, metadata: Any, result: Any, query: Any = "", scope: Any = ""
    ) -> bool:
        """True when an L1/semantic entry references the given source."""
        if not norm_source:
            return False
        try:
            meta_text = json.dumps(metadata or {}, ensure_ascii=False, default=str).lower()
        except Exception:
            meta_text = str(metadata).lower()
        return (
            norm_source in meta_text
            or norm_source in str(result).lower()
            or norm_source in str(query).lower()
            or norm_source in str(scope).lower()
        )

    def _collect_invalidation_targets(
        self, source: str
    ) -> tuple[list[str], list[int], list[Any]]:
        """Finds matching L1 keys, semantic indexes and L2 keys without mutating state."""
        norm_source = source.lower().strip()
        exact_keys: list[str] = []
        semantic_indexes: list[int] = []
        with self._lock:
            for key, (_, result, meta) in self._cache.items():
                if self._source_matches(norm_source, meta, result):
                    exact_keys.append(key)
            for idx, entry in enumerate(self._semantic_entries):
                if self._source_matches(
                    norm_source,
                    entry.get("metadata", {}),
                    entry.get("result", ""),
                    entry.get("query", ""),
                    entry.get("scope", ""),
                ):
                    semantic_indexes.append(idx)

        redis_keys: list[Any] = []
        if self._redis_client is not None and norm_source:
            try:
                for key in self._iter_redis_keys(RAG_CACHE_KEY_PREFIX + "*"):
                    if not self._is_value_key(key):
                        continue
                    raw_val = self._redis_client.get(key)
                    if raw_val is not None and norm_source in self._as_text(raw_val).lower():
                        redis_keys.append(key)
            except Exception as ex:
                logger.warning(f"Redis selective invalidation scan failed: {ex}")
        return exact_keys, semantic_indexes, redis_keys

    def _apply_invalidation_targets(
        self, exact_keys: list[str], semantic_indexes: list[int], redis_keys: list[Any]
    ) -> int:
        """Deletes only matched keys and returns the real number of removed keys."""
        with self._lock:
            for key in exact_keys:
                self._cache.pop(key, None)
            for idx in reversed(semantic_indexes):
                if 0 <= idx < len(self._semantic_entries):
                    del self._semantic_entries[idx]

        removed = len(exact_keys) + len(semantic_indexes)
        if redis_keys and self._redis_client is not None:
            try:
                pipe = self._redis_client.pipeline()
                for key in redis_keys:
                    pipe.delete(key)
                pipe.execute()
                removed += len(redis_keys)
            except Exception as ex:
                logger.warning(f"Redis selective invalidation delete failed: {ex}")
        return removed

    def invalidate_by_source(self, source: str) -> int:
        """Selectively invalidates cached RAG entries associated with a document or source."""
        if not source:
            return 0
        targets = self._collect_invalidation_targets(source)
        removed = self._apply_invalidation_targets(*targets)
        logger.info(f"Selectively invalidated {removed} RAG cache entries for source '{source}'.")
        return removed

    async def ainvalidate_by_source(self, source: str) -> int:
        """Async counterpart of :meth:`invalidate_by_source` (offloads blocking Redis I/O)."""
        return await asyncio.to_thread(self.invalidate_by_source, source)

    def invalidate_or_noop(self, source: str) -> int:
        """Invalidates matching entries, or does absolutely nothing when none match.

        Used by callers that previously fell back to ``clear()`` when the count
        was zero — a full flush is never safe in a shared multi-tenant cache.
        """
        if not source:
            return 0
        targets = self._collect_invalidation_targets(source)
        if not any(targets):
            logger.info(
                "RAG invalidation for source '%s' matched no keys; no-op (cache left intact).",
                source,
            )
            return 0
        removed = self._apply_invalidation_targets(*targets)
        logger.info(f"Selectively invalidated {removed} RAG cache entries for source '{source}'.")
        return removed

    async def ainvalidate_or_noop(self, source: str) -> int:
        """Async counterpart of :meth:`invalidate_or_noop`."""
        return await asyncio.to_thread(self.invalidate_or_noop, source)

    def invalidate_document(self, doc_id: str) -> int:
        """Selectively invalidates cache entries for a specific document identifier."""
        return self.invalidate_by_source(doc_id)

    def publish_invalidation(self, doc_id: str, file_path: str) -> int | None:
        """Broadcasts a document invalidation to every replica via Redis.

        Uses an atomic INCR generation key plus a pub/sub notification so other
        pods can invalidate their process-local retriever and L1 cache. Failure
        is explicitly non-fatal: returns ``None`` with a WARNING when Redis is
        unavailable or misbehaving. Safe to call off the event loop with
        ``asyncio.to_thread``.
        """
        if self._redis_client is None:
            logger.warning(
                "Redis L2 unavailable; skipping invalidation broadcast "
                "for doc_id=%s file_path=%s.",
                doc_id,
                file_path,
            )
            return None
        try:
            generation = int(self._redis_client.incr(RAG_INVALIDATION_GENERATION_KEY))
            message = json.dumps(
                {
                    "doc_id": doc_id,
                    "file_path": file_path,
                    "generation": generation,
                    "ts": time.time(),
                },
                ensure_ascii=False,
            )
            self._redis_client.set(RAG_INVALIDATION_LATEST_KEY, message, ex=3600)
            self._redis_client.publish(RAG_INVALIDATION_CHANNEL, message)
            # This pod already invalidates its own entries via the caller;
            # recording the generation prevents a redundant self-trigger.
            self._invalidation_generation = generation
            logger.info(
                "Broadcast RAG invalidation generation=%s doc_id=%s file_path=%s.",
                generation,
                doc_id,
                file_path,
            )
            return generation
        except Exception as ex:
            logger.warning(
                "Redis invalidation broadcast failed for doc_id=%s file_path=%s: %s",
                doc_id,
                file_path,
                ex,
            )
            return None

    def register_invalidation_callback(
        self, callback: Callable[[str, str, int], None]
    ) -> None:
        """Registers ``callback(doc_id, file_path, generation)`` for remote invalidations.

        Use this to drop a process-local retriever/index on another replica.
        """
        with self._lock:
            if callback not in self._invalidation_callbacks:
                self._invalidation_callbacks.append(callback)

    def check_invalidation_generation(self) -> int | None:
        """Polls the shared generation key; applies remote invalidation when it advanced.

        Clears this pod's L1 cache and invokes registered callbacks. Returns
        ``None`` when Redis is unavailable (never raises).
        """
        if self._redis_client is None:
            return None
        try:
            raw_generation = self._redis_client.get(RAG_INVALIDATION_GENERATION_KEY)
            if raw_generation is None:
                return self._invalidation_generation
            generation = int(self._as_text(raw_generation).strip())
            if generation <= self._invalidation_generation:
                return generation

            doc_id = file_path = ""
            latest_raw = self._redis_client.get(RAG_INVALIDATION_LATEST_KEY)
            if latest_raw is not None:
                try:
                    latest = json.loads(self._as_text(latest_raw))
                    doc_id = str(latest.get("doc_id", ""))
                    file_path = str(latest.get("file_path", ""))
                except (TypeError, ValueError):
                    pass

            self._invalidation_generation = generation
            with self._lock:
                self._cache.clear()
                self._semantic_entries.clear()

            # Clear L2 values too. Advancing the generation without deleting
            # Redis keys leaves stale values that are immediately rehydrated
            # into L1. Only keys whose embedded generation is older than the
            # new one are deleted: a value written after this generation by a
            # faster replica is fresh and must survive. This also avoids any
            # indiscriminate cross-tenant flush.
            removed_l2 = 0
            try:
                stale_keys: list[Any] = []
                for key in self._iter_redis_keys(RAG_CACHE_KEY_PREFIX + "*"):
                    if not self._is_value_key(key):
                        continue
                    raw_val = self._redis_client.get(key)
                    if raw_val is None:
                        continue
                    value_gen: int | None = None
                    try:
                        parsed = json.loads(self._as_text(raw_val))
                        if isinstance(parsed, dict) and isinstance(
                            parsed.get("generation"), int
                        ):
                            value_gen = int(parsed["generation"])
                    except (TypeError, ValueError):
                        value_gen = None  # legacy value without generation metadata
                    if value_gen is None or value_gen < generation:
                        stale_keys.append(key)
                for start in range(0, len(stale_keys), 500):
                    batch = stale_keys[start : start + 500]
                    pipe = self._redis_client.pipeline()
                    for key in batch:
                        pipe.delete(key)
                    pipe.execute()
                    removed_l2 += len(batch)
            except Exception as ex:
                logger.warning("Could not purge stale RAG L2 keys after invalidation: %s", ex)

            logger.info(
                "Applied remote RAG invalidation generation=%s doc_id=%s file_path=%s; "
                "cleared local L1 cache and %s stale L2 keys.",
                generation,
                doc_id,
                file_path,
                removed_l2,
            )
            for callback in list(self._invalidation_callbacks):
                try:
                    callback(doc_id, file_path, generation)
                except Exception as ex:
                    logger.warning("RAG invalidation callback failed: %s", ex)
            return generation
        except Exception as ex:
            logger.warning("Could not check RAG invalidation generation: %s", ex)
            return None

    @property
    def invalidation_generation(self) -> int:
        """Last observed cross-replica invalidation generation."""
        return self._invalidation_generation

    def clear(self) -> None:
        """Flushes all cached entries from L1 and L2 (maintenance use only)."""
        with self._lock:
            self._cache.clear()
            self._semantic_entries.clear()
        if self._redis_client is not None:
            try:
                keys = [
                    key
                    for key in self._iter_redis_keys(RAG_CACHE_KEY_PREFIX + "*")
                    if self._is_value_key(key)
                ]
                if keys:
                    pipe = self._redis_client.pipeline()
                    for key in keys:
                        pipe.delete(key)
                    pipe.execute()
            except Exception as ex:
                logger.warning(f"Redis L2 clear exception: {ex}")

    async def aclear(self) -> None:
        """Async counterpart of :meth:`clear`."""
        await asyncio.to_thread(self.clear)

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
                "semantic_entries": len(self._semantic_entries),
                "semantic_hits": self._semantic_hits,
                "invalidation_generation": self._invalidation_generation,
            }


_rag_cache_instance: RAGCache | None = None


def get_rag_cache() -> RAGCache:
    """Returns singleton RAGCache instance."""
    global _rag_cache_instance
    if _rag_cache_instance is None:
        settings = get_settings()
        _rag_cache_instance = RAGCache(
            max_entries=settings.cache_max_entries,
            default_ttl=settings.rag_cache_ttl_seconds,
            redis_url=settings.redis_url,
            enable_redis=settings.enable_redis_cache,
        )
    return _rag_cache_instance

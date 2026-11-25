"""Unit tests for Recommendation hardening:
1. Token Tier Rate Limiting
2. Domain-Adaptive Semantic Cache Distance Threshold
"""

import pytest

from src.agentic.api.middleware import (
    SlidingWindowRateLimiter,
    estimate_tokens_from_request,
)
from src.agentic.cache.rag_cache import RAGCache


def test_token_tier_rate_limiter_in_memory():
    """Verifies dual enforcement: request rate limit and token tier rate limit."""
    limiter = SlidingWindowRateLimiter(
        limit_per_minute=10,
        token_limit_per_minute=500,
        enable_token_limit=True,
    )
    client = "tenant_test_1"

    # 1. Normal request allowed
    allowed, _ = limiter.is_allowed(client)
    assert allowed is True

    # 2. Token limit allowance
    tok_ok, _ = limiter.is_token_allowed(client, tokens=200)
    assert tok_ok is True

    tok_ok2, _ = limiter.is_token_allowed(client, tokens=200)
    assert tok_ok2 is True

    # 3. Exceed token limit (200 + 200 + 150 = 550 > 500)
    tok_rejected, retry_after = limiter.is_token_allowed(client, tokens=150)
    assert tok_rejected is False
    assert retry_after > 0


def test_adaptive_semantic_cache_domain_thresholds():
    """Verifies domain-specific adaptive cosine similarity thresholds."""
    cache = RAGCache(max_entries=50, default_ttl=300, enable_redis=False)

    # Check adaptive thresholds
    assert cache.get_domain_threshold("legal") >= 0.95
    assert cache.get_domain_threshold("finance") >= 0.95
    assert cache.get_domain_threshold("medical") >= 0.95
    assert cache.get_domain_threshold("coding") == 0.94
    assert cache.get_domain_threshold("chat") <= 0.90
    assert cache.get_domain_threshold("unknown_domain") == 0.92

    # Set semantic entry
    cache.set_semantic(
        query="what is company travel policy",
        result="Company travel policy allows business class for flights over 6 hours.",
        domain="general",
        top_k=3,
    )

    # Exact match should hit
    exact_hit = cache.get("what is company travel policy", top_k=3)
    assert exact_hit is not None

    # Paraphrased query should achieve semantic cache hit
    semantic_hit = cache.get_semantic(
        query="what is our company travel policy",
        domain="chat",
        top_k=3,
    )
    assert semantic_hit == "Company travel policy allows business class for flights over 6 hours."
    assert cache.stats["semantic_hits"] >= 1


def test_rag_cache_get_or_compute_with_domain():
    """Verifies get_or_compute seamlessly leverages domain-aware semantic caching."""
    cache = RAGCache(max_entries=20, default_ttl=60, enable_redis=False)
    computed_counter = 0

    def compute_expensive():
        nonlocal computed_counter
        computed_counter += 1
        return "Legal NDA compliance guidelines"

    # First call computes
    res1 = cache.get_or_compute(
        "non-disclosure agreement requirements",
        compute_expensive,
        top_k=3,
        domain="legal",
    )
    assert res1 == "Legal NDA compliance guidelines"
    assert computed_counter == 1

    # Exact repeated call hits cache
    res2 = cache.get_or_compute(
        "non-disclosure agreement requirements",
        compute_expensive,
        top_k=3,
        domain="legal",
    )
    assert res2 == "Legal NDA compliance guidelines"
    assert computed_counter == 1


def test_estimate_tokens_from_request_multilingual():
    """Verifies token estimation accuracy for Vietnamese and multi-lingual requests."""
    # 1. Zero or negative content length
    assert estimate_tokens_from_request(0) == 10
    assert estimate_tokens_from_request(-5) == 10

    # 2. Small payload below envelope threshold
    assert estimate_tokens_from_request(30) == 10

    # 3. Standard JSON payload (e.g. 150 bytes)
    tok = estimate_tokens_from_request(150, "application/json")
    # (150 - 30) / 3.2 = 37.5 -> 37
    assert tok == 37

    # 4. Vietnamese text payload (e.g. 600 bytes)
    tok_vn = estimate_tokens_from_request(600, "application/json; charset=utf-8")
    # (600 - 30) / 3.2 = 178
    assert 170 <= tok_vn <= 190


def test_rag_cache_custom_embed_fn_sync():
    """Verifies RAGCache dense embedding function registration and fallback."""
    # Mock dense embedding function
    def mock_embedder(text: str) -> list[float]:
        if "finance" in text.lower():
            return [1.0, 0.0, 0.0, 0.0]
        return [0.0, 1.0, 0.0, 0.0]

    cache = RAGCache(max_entries=20, default_ttl=60, enable_redis=False, embed_fn=mock_embedder)

    v1 = cache.get_embedding("finance reports 2026")
    assert v1 == [1.0, 0.0, 0.0, 0.0]

    v2 = cache.get_embedding("general overview")
    assert v2 == [0.0, 1.0, 0.0, 0.0]

    # Test error fallback to pseudo-dense vector
    def error_embedder(text: str) -> list[float]:
        raise RuntimeError("Embedding model connection failed")

    cache.set_embed_fn(error_embedder)
    fallback_v = cache.get_embedding("test fallback text")
    assert isinstance(fallback_v, list)
    assert len(fallback_v) == 128


@pytest.mark.asyncio
async def test_rag_cache_custom_embed_fn_async():
    """Verifies asynchronous dense embedding function and get_or_compute_async."""
    async def mock_async_embedder(text: str) -> list[float]:
        if "crypto" in text.lower():
            return [0.5, 0.5, 0.0]
        return [0.0, 0.5, 0.5]

    cache = RAGCache(max_entries=20, default_ttl=60, enable_redis=False)
    cache.set_embed_fn(mock_async_embedder)

    v_async = await cache.aget_embedding("crypto policies")
    assert v_async == [0.5, 0.5, 0.0]

    # Test get_or_compute_async with neural embedder
    async def sample_compute():
        return "Decentralized Finance guidelines"

    res = await cache.get_or_compute_async(
        "crypto policies",
        sample_compute,
        domain="finance",
        top_k=3,
    )
    assert res == "Decentralized Finance guidelines"

    # Async error fallback
    async def failing_async_embedder(text: str):
        raise ValueError("Embedder crashed")

    cache.set_embed_fn(failing_async_embedder)
    v_fallback = await cache.aget_embedding("test fallback")
    assert len(v_fallback) == 128


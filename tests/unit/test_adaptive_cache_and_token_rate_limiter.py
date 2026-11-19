"""Unit tests for Recommendation hardening:
1. Token Tier Rate Limiting
2. Domain-Adaptive Semantic Cache Distance Threshold
"""

from src.agentic.api.middleware import SlidingWindowRateLimiter
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

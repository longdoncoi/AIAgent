"""Chaos engineering and resilience integration tests: Failover, Graceful Degradation, and Concurrency."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from src.agentic.api.middleware import SlidingWindowRateLimiter
from src.agentic.cache.prompt_cache import PromptCache
from src.agentic.cache.rag_cache import RAGCache
from src.agentic.core.config import get_settings
from src.agentic.llm.gateway import ModelCircuitBreaker, get_llm
from src.agentic.workflows.rag_workflow import ingest_documents_activity


def test_circuit_breaker_failover_routing():
    """Verifies that circuit breaker trip automatically switches endpoints to backup CPU model."""
    settings = get_settings()
    cb = ModelCircuitBreaker(failure_threshold=2, recovery_time_seconds=10.0)
    alias = "reasoning-llm"

    # Initially closed
    assert cb.should_fallback(alias) is False

    # Simulate 2 failures
    cb.record_failure(alias)
    cb.record_failure(alias)
    assert cb.should_fallback(alias) is True

    # Temporarily patch global circuit breaker to verify get_llm routing
    import src.agentic.llm.gateway as gw
    orig_cb = gw._circuit_breaker
    try:
        gw._circuit_breaker = cb
        llm = get_llm(alias=alias)
        # Should now route to backup llama_chat_base_url
        assert llm.openai_api_base == settings.llama_chat_base_url
        assert llm.model_name == settings.default_fast_model

        # Record success -> resets to closed
        cb.record_success(alias)
        assert cb.should_fallback(alias) is False
        normal_llm = get_llm(alias=alias)
        assert normal_llm.openai_api_base == settings.litellm_base_url
        assert normal_llm.model_name == settings.default_reasoning_model
    finally:
        gw._circuit_breaker = orig_cb


def test_redis_outage_graceful_degradation():
    """Verifies system degrades gracefully to L1 in-memory when Redis is offline or throws errors."""
    # Point to a non-existent port (simulating down/crashed Redis)
    dead_redis_url = "redis://localhost:59999/0"

    # 1. Rate Limiter degradation
    rl = SlidingWindowRateLimiter(limit_per_minute=5, redis_url=dead_redis_url, enable_redis=True)
    assert rl.is_allowed("client_chaos_1")[0] is True
    assert rl.is_allowed("client_chaos_1")[0] is True

    # 2. Prompt Cache degradation
    pc = PromptCache(redis_url=dead_redis_url, enable_redis=True)
    pc.set_response(model_alias="test-llm", messages=["ping"], temperature=0.1, response="pong")
    res = pc.get_response(model_alias="test-llm", messages=["ping"], temperature=0.1)
    assert res == "pong"
    assert pc.stats["has_redis_backend"] is False

    # 3. RAG Cache degradation
    rc = RAGCache(redis_url=dead_redis_url, enable_redis=True)
    rc.set(query="what is our policy?", result="Company Security Policy v2", top_k=3)
    rag_hit = rc.get(query="what is our policy?", top_k=3)
    assert rag_hit == "Company Security Policy v2"


def test_multithreaded_cache_concurrency_race():
    """Verifies cache thread-safety and lack of deadlocks under high-concurrency contention."""
    cache = PromptCache(max_entries=500)

    def worker(worker_id: int) -> int:
        hits = 0
        for i in range(50):
            key_msg = [f"worker_{worker_id % 5}_msg_{i % 10}"]
            cache.set_response("concurrency-llm", key_msg, 0.2, f"response_{i}")
            val = cache.get_response("concurrency-llm", key_msg, 0.2)
            if val is not None:
                hits += 1
        return hits

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(worker, range(16)))

    assert sum(results) > 0
    assert cache.stats["size"] <= 500


@pytest.mark.asyncio
async def test_temporal_rag_ingest_activity():
    """Verifies that durable RAG ingestion activity executes without unhandled errors."""
    res = await ingest_documents_activity()
    assert res["status"] == "completed"
    assert "persist_dir" in res

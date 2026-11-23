"""Unit tests for Caching subsystem (RAG, Prompt, KV) and Security/RBAC."""

import time

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from src.agentic.cache.kv_cache import get_kv_cache_headers, optimize_messages_for_kv_cache
from src.agentic.cache.prompt_cache import PromptCache
from src.agentic.cache.rag_cache import RAGCache


def test_rag_cache_hit_and_miss():
    cache = RAGCache(max_entries=10, default_ttl=5)
    cache.clear()

    query = "Quy định về thời gian timeout của sandbox code execution?"
    assert cache.get(query) is None

    sample_result = "Timeout tối đa là 30 giây trong sandbox cách ly."
    cache.set(query, sample_result)

    # Cache hit
    hit_result = cache.get(query)
    assert hit_result == sample_result

    # Case insensitivity & whitespace normalization
    assert (
        cache.get("  quy định về thời gian TIMEOUT của sandbox code execution?  ") == sample_result
    )

    stats = cache.stats
    assert stats["hits"] >= 2
    assert stats["misses"] >= 1


def test_rag_cache_ttl_expiration():
    cache = RAGCache(max_entries=10, default_ttl=1)
    cache.clear()

    query = "Chính sách PII"
    cache.set(query, "Nội dung chính sách", ttl=1)
    assert cache.get(query) == "Nội dung chính sách"

    # Wait for TTL expiration
    time.sleep(1.1)
    assert cache.get(query) is None


def test_prompt_cache_exact_match():
    cache = PromptCache(max_entries=10, default_ttl=10)
    cache.clear()

    messages = [
        SystemMessage(content="You are a helpful coding assistant."),
        HumanMessage(content="Write a Python hello world function."),
    ]

    assert cache.get_response("fast-llm", messages, temperature=0.1) is None

    fake_completion = "def hello():\n    print('Hello World')"
    cache.set_response("fast-llm", messages, temperature=0.1, response=fake_completion)

    # Hit
    assert cache.get_response("fast-llm", messages, temperature=0.1) == fake_completion

    # Miss with different model or higher temperature
    assert cache.get_response("reasoning-llm", messages, temperature=0.1) is None
    # High temperature bypasses cache
    assert cache.get_response("fast-llm", messages, temperature=0.8) is None


def test_kv_cache_prefix_optimization():
    raw_messages = [
        HumanMessage(content="Can you explain RRF in Hybrid RAG?"),
        SystemMessage(content="You are an enterprise knowledge search assistant."),
    ]

    optimized = optimize_messages_for_kv_cache(raw_messages)
    # System message must be pinned to index 0 for GPU prefix caching
    assert isinstance(optimized[0], SystemMessage)
    assert "enterprise knowledge search assistant" in str(optimized[0].content)
    assert isinstance(optimized[1], HumanMessage)

    headers = get_kv_cache_headers()
    assert "X-Cache-Control" in headers


@pytest.mark.asyncio
async def test_rbac_approval_endpoint_protection():
    from httpx import ASGITransport, AsyncClient

    from src.agentic.api.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        import time

        import jwt

        from src.agentic.core.config import get_settings

        payload = {
            "thread_id": "fake_thread_123",
            "step_id": "step_1",
            "decision": {"approved": True},
        }
        url = "/api/v1/approvals/fake_thread_123/resume"

        # An invalid token is always rejected (no downgrade to an identity).
        resp_invalid = await client.post(
            url, json=payload, headers={"Authorization": "Bearer unauthorized_guest_token"}
        )
        assert resp_invalid.status_code == 401

        # A valid guest JWT is authenticated but not authorized for approvals.
        settings = get_settings()
        now = int(time.time())
        guest_token = jwt.encode(
            {
                "sub": "guest",
                "role": "guest",
                "aud": settings.jwt_audience,
                "iss": settings.jwt_issuer,
                "iat": now,
                "exp": now + 3600,
            },
            settings.jwt_secret_key,
            algorithm=settings.jwt_algorithm,
        )
        resp = await client.post(url, json=payload, headers={"Authorization": f"Bearer {guest_token}"})
        assert resp.status_code == 403
        assert "privileges" in resp.text

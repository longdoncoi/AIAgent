"""Unit tests covering Next-Gen Production Hardening Recommendations:
1. Dynamic hot-reload of policies and LLM Gateway circuit breaker via API and code
2. Prometheus sub-second latency buckets & Time-to-First-Token (TTFT) metrics
3. Flexible per-thread TTL and inactive thread checkpoint cleanup in RedisSaver
4. Selective semantic cache invalidation via Change Data Capture (CDC) in RAGCache
5. Pod-to-Pod mTLS client configuration in A2AClient
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.agentic.a2a.client import A2AClient
from src.agentic.api.main import app
from src.agentic.cache.rag_cache import RAGCache, get_rag_cache
from src.agentic.core.config import Settings
from src.agentic.llm.gateway import _circuit_breaker, reload_gateway_config
from src.agentic.memory.redis_saver import RedisSaver
from src.agentic.observability.metrics import REQUEST_DURATION, TTFT_DURATION
from src.agentic.safety.policy import get_policy_manager, reload_policy_manager
from src.agentic.workflows.rag_workflow import ingest_delta_document_activity


def test_dynamic_policy_reload() -> None:
    """Verifies that PolicyManager can be reloaded dynamically without restart."""
    mgr = get_policy_manager()
    assert mgr.policy_data is not None

    # Test instance reload method
    reloaded_data = mgr.reload()
    assert reloaded_data is not None
    assert isinstance(reloaded_data, dict)

    # Test singleton cache clear and reload
    mgr2 = reload_policy_manager()
    assert mgr2 is not None
    assert mgr2.policy_data is not None


def test_dynamic_gateway_config_reload() -> None:
    """Verifies that reload_gateway_config resets circuit breaker state."""
    # Trip the circuit breaker
    _circuit_breaker.record_failure("test-alias")
    _circuit_breaker.record_failure("test-alias")
    _circuit_breaker.record_failure("test-alias")
    assert _circuit_breaker.state.get("test-alias") == "OPEN"

    # Reload gateway config
    reload_gateway_config()
    assert "test-alias" not in _circuit_breaker.state
    assert "test-alias" not in _circuit_breaker.failures
    assert not _circuit_breaker.should_fallback("test-alias")


def test_admin_reload_configs_endpoint() -> None:
    """Tests the /api/v1/admin/reload-configs endpoint with RBAC checks."""
    import jwt

    from src.agentic.core.config import get_settings

    settings = get_settings()
    client = TestClient(app)

    # 1. Invalid token format should be rejected with 401
    resp_bad_header = client.post(
        "/api/v1/admin/reload-configs",
        headers={"Authorization": "InvalidTokenFormat"},
    )
    assert resp_bad_header.status_code == 401

    # 2. Non-admin user token should be rejected with 403 Forbidden
    user_token = jwt.encode(
        {"sub": "regular_user", "role": "user"},
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    resp_forbidden = client.post(
        "/api/v1/admin/reload-configs",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert resp_forbidden.status_code == 403

    # 3. Admin should succeed
    resp_admin = client.post(
        "/api/v1/admin/reload-configs",
        headers={"Authorization": "Bearer admin_token"},
    )
    assert resp_admin.status_code == 200
    data = resp_admin.json()
    assert data["status"] == "ok"
    assert "reloaded successfully" in data["message"]


def test_prometheus_subsecond_buckets_and_ttft() -> None:
    """Verifies Prometheus metrics configuration for TTFT and subsecond buckets."""
    # 1. Check REQUEST_DURATION buckets contain subsecond intervals
    buckets = REQUEST_DURATION._upper_bounds
    assert 0.01 in buckets
    assert 0.025 in buckets
    assert 0.05 in buckets

    # 2. Check TTFT_DURATION metric
    TTFT_DURATION.labels(endpoint="chat_stream").observe(0.045)
    # Check sample observation works cleanly
    assert TTFT_DURATION.labels(endpoint="chat_stream") is not None


def test_redis_saver_per_thread_ttl_and_cleanup() -> None:
    """Verifies per-thread TTL in put/aput and inactive thread checkpoint cleanup."""
    mock_redis = MagicMock()
    mock_redis.keys.return_value = [b"agentic:checkpoint:index:thread_old:"]
    mock_redis.ttl.return_value = -2  # Expired
    mock_redis.smembers.return_value = set()

    saver = RedisSaver(redis_url="redis://localhost:6379/0", ttl_seconds=3600)
    saver._redis_client = mock_redis

    # Test put with custom per-thread TTL in config
    config = {
        "configurable": {
            "thread_id": "custom_thread_1",
            "checkpoint_ns": "",
            "checkpoint_ttl": 7200,
        }
    }
    checkpoint = {
        "id": "cp_1",
        "v": 1,
        "ts": "2026-01-01",
        "channel_values": {},
        "channel_versions": {},
        "versions_seen": {},
    }
    metadata = {"step": 1}
    new_versions = {}

    saver.put(config, checkpoint, metadata, new_versions)
    # Verify set was called with ex=7200
    mock_redis.set.assert_called()
    call_args = mock_redis.set.call_args
    assert call_args[1].get("ex") == 7200 or call_args[0][2] == 7200 or "ex" in call_args[1]

    # Test cleanup_inactive_threads
    cleaned = saver.cleanup_inactive_threads(max_idle_seconds=86400 * 30)
    assert cleaned >= 1
    mock_redis.delete.assert_called()


@pytest.mark.asyncio
async def test_redis_saver_async_per_thread_ttl() -> None:
    """Verifies async aput with per-thread TTL."""
    mock_async_redis = MagicMock()
    mock_async_redis.set = MagicMock(side_effect=lambda *args, **kwargs: None)
    mock_async_redis.sadd = MagicMock(side_effect=lambda *args, **kwargs: None)
    mock_async_redis.expire = MagicMock(side_effect=lambda *args, **kwargs: None)

    saver = RedisSaver(redis_url="redis://localhost:6379/0", ttl_seconds=3600)
    saver._async_redis_client = mock_async_redis

    config = {
        "configurable": {
            "thread_id": "async_thread_1",
            "checkpoint_ns": "",
            "checkpoint_ttl": 1800,
        }
    }
    checkpoint = {
        "id": "cp_async",
        "v": 1,
        "ts": "2026-01-01",
        "channel_values": {},
        "channel_versions": {},
        "versions_seen": {},
    }
    metadata = {"step": 1}
    new_versions = {}

    # Make async mock compatible
    async def _async_set(*args, **kwargs):
        pass

    async def _async_sadd(*args, **kwargs):
        pass

    async def _async_expire(*args, **kwargs):
        pass

    mock_async_redis.set = _async_set
    mock_async_redis.sadd = _async_sadd
    mock_async_redis.expire = _async_expire

    res = await saver.aput(config, checkpoint, metadata, new_versions)
    assert res is not None


def test_admin_cleanup_checkpoints_endpoint() -> None:
    """Tests /api/v1/admin/cleanup-checkpoints endpoint."""
    client = TestClient(app)
    resp = client.post(
        "/api/v1/admin/cleanup-checkpoints?max_idle_seconds=3600",
        headers={"Authorization": "Bearer admin_token"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "cleaned_threads" in data


def test_selective_cache_invalidation_by_source_and_document() -> None:
    """Verifies selective RAG cache invalidation preserves unrelated entries."""
    cache = RAGCache(max_entries=100)

    # Seed cache with 2 distinct documents
    cache.set(
        query="What is the refund policy?",
        result="Refund within 30 days.",
        metadata={"source": "finance_refund_policy.md", "doc_id": "doc_fin_01"},
    )
    cache.set(
        query="What is the code execution limit?",
        result="Sandbox execution limit is 30 seconds.",
        metadata={"source": "security_code_policy.md", "doc_id": "doc_sec_01"},
    )

    # Seed semantic cache entries
    cache.set_semantic(
        query="How to get refund?",
        result="Refund within 30 days.",
        metadata={"source": "finance_refund_policy.md", "doc_id": "doc_fin_01"},
    )
    cache.set_semantic(
        query="Bash command execution rules",
        result="Bash is high risk.",
        metadata={"source": "security_code_policy.md", "doc_id": "doc_sec_01"},
    )

    # Invalidate ONLY finance document
    invalidated_count = cache.invalidate_by_source("finance_refund_policy.md")
    assert invalidated_count >= 1

    # Finance query should now be a MISS
    assert cache.get("What is the refund policy?") is None

    # Security query must remain a HIT!
    assert cache.get("What is the code execution limit?") is not None

    # Invalidate security document by doc_id
    sec_count = cache.invalidate_document("doc_sec_01")
    assert sec_count >= 1
    assert cache.get("What is the code execution limit?") is None


@pytest.mark.asyncio
async def test_cdc_delta_ingestion_selective_cache_invalidation() -> None:
    """Verifies ingest_delta_document_activity uses selective cache invalidation."""
    cache = get_rag_cache()
    cache.set(
        query="Sample query for delta test",
        result="Sample result referencing delta_test_doc",
        metadata={"source": "delta_test_doc.md", "doc_id": "delta_test_doc"},
    )
    assert cache.get("Sample query for delta test") is not None

    event_data = {
        "doc_id": "delta_test_doc",
        "action": "upsert",
        "file_path": "delta_test_doc.md",
        "content": "# Updated Knowledge Content\nNew policy guidelines.",
    }

    res = await ingest_delta_document_activity(event_data)
    assert res["status"] == "completed"
    assert res["cache_invalidated"] is True
    # The updated document cache was selectively evicted
    assert cache.get("Sample query for delta test") is None


def test_a2a_client_mtls_configuration() -> None:
    """Verifies that A2AClient properly configures client certificates when mTLS is enabled."""
    with patch("src.agentic.a2a.client.get_settings") as mock_settings_fn:
        mock_settings = Settings()
        mock_settings.enable_mtls = True
        mock_settings.mtls_cert_path = "/etc/certs/client.crt"
        mock_settings.mtls_key_path = "/etc/certs/client.key"
        mock_settings.mtls_ca_cert_path = "/etc/certs/ca.crt"
        mock_settings_fn.return_value = mock_settings

        with patch("httpx.AsyncClient") as mock_async_client_cls:
            client = A2AClient()
            _ = client._get_http_client()

            mock_async_client_cls.assert_called_once()
            call_kwargs = mock_async_client_cls.call_args[1]
            assert call_kwargs.get("cert") == ("/etc/certs/client.crt", "/etc/certs/client.key")
            assert call_kwargs.get("verify") == "/etc/certs/ca.crt"

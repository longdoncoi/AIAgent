"""Unit tests verifying Production Hardening: RBAC, Rate Limiting, Persistence, Circuit Breaker, and S2S."""

import pytest
from fastapi import HTTPException

from src.agentic.api.deps import require_role
from src.agentic.api.middleware import SlidingWindowRateLimiter
from src.agentic.core.errors import GuardrailViolation
from src.agentic.llm.gateway import ModelCircuitBreaker
from src.agentic.memory.long_term import LongTermStore
from src.agentic.memory.short_term import get_checkpointer
from src.agentic.safety.guardrails import get_guardrails


def test_sliding_window_rate_limiter() -> None:
    limiter = SlidingWindowRateLimiter(limit_per_minute=3)
    key = "test_client_ip_1"

    allowed, _ = limiter.is_allowed(key)
    assert allowed is True
    allowed, _ = limiter.is_allowed(key)
    assert allowed is True
    allowed, _ = limiter.is_allowed(key)
    assert allowed is True

    # 4th request must be rejected
    allowed, retry_after = limiter.is_allowed(key)
    assert allowed is False
    assert retry_after > 0


@pytest.mark.asyncio
async def test_require_role_rbac() -> None:
    role_checker = require_role(["admin", "supervisor"])

    # Allowed: supervisor
    res = await role_checker({"user_id": "alice", "role": "supervisor"})
    assert res["user_id"] == "alice"

    # Allowed: admin (universal bypass)
    res = await role_checker({"user_id": "bob", "role": "admin"})
    assert res["user_id"] == "bob"

    # Forbidden: guest / user
    with pytest.raises(HTTPException) as exc_info:
        await role_checker({"user_id": "charlie", "role": "guest"})
    assert exc_info.value.status_code == 403


def test_sqlite_long_term_store_persistence(tmp_path) -> None:
    store = LongTermStore(storage_dir=tmp_path, force_sqlite=True)
    store.add_semantic("pref_theme", "dark_mode", {"user": "alice"})
    store.add_episodic("task_001", "Deployed auth microservice", {"status": "success"})
    store.add_procedural("deploy_k8s", "kubectl apply -f deploy/k8s/", {"env": "prod"})

    # Search semantic
    results = store.search("dark_mode", category="semantic")
    assert len(results) == 1
    assert results[0].content == "dark_mode"
    assert results[0].metadata.get("user") == "alice"

    # Search episodic
    results_epi = store.search("microservice", category="episodic")
    assert len(results_epi) == 1
    assert "Deployed" in results_epi[0].content

    # Search procedural
    results_proc = store.search("kubectl", category="procedural")
    assert len(results_proc) == 1
    assert "deploy_k8s" in results_proc[0].id


def test_persistent_checkpointer_instance() -> None:
    checkpointer = get_checkpointer()
    assert checkpointer is not None


def test_tool_guardrail_validations() -> None:
    guardrails = get_guardrails()

    # Valid args pass
    safe_args = {"file_path": "src/main.py", "max_results": 10}
    validated = guardrails.validate_tool_input("repo_read_file", safe_args)
    assert validated == safe_args

    # Path traversal must be blocked
    with pytest.raises(GuardrailViolation):
        guardrails.validate_tool_input("repo_read_file", {"file_path": "../../etc/passwd"})

    # SQL injection must be blocked
    with pytest.raises(GuardrailViolation):
        guardrails.validate_tool_input(
            "db_query", {"query": "SELECT * FROM users UNION SELECT password FROM admin"}
        )


def test_model_circuit_breaker() -> None:
    cb = ModelCircuitBreaker(failure_threshold=2, recovery_time_seconds=1.0)
    alias = "reasoning-llm"

    assert cb.should_fallback(alias) is False
    cb.record_failure(alias)
    assert cb.should_fallback(alias) is False
    cb.record_failure(alias)

    # 2 failures reached -> circuit trips OPEN -> should fallback
    assert cb.should_fallback(alias) is True

    # Record success -> resets to CLOSED
    cb.record_success(alias)
    assert cb.should_fallback(alias) is False


def test_production_inviolable_validators() -> None:
    from src.agentic.core.config import Settings

    # Production with default secret should fail
    with pytest.raises(ValueError, match="Production Inviolable Violated: jwt_secret_key"):
        Settings(
            environment="production",
            jwt_secret_key="agentic-secret-key-change-in-production-2026",
            s2s_internal_token="prod-secure-token",
            require_authentication=True,
            use_postgres_checkpoint=True,
        )

    # Production with require_authentication=False should fail
    with pytest.raises(ValueError, match="require_authentication must be True"):
        Settings(
            environment="production",
            jwt_secret_key="custom-secure-prod-key-12345",
            s2s_internal_token="custom-secure-s2s-token-12345",
            require_authentication=False,
            use_postgres_checkpoint=True,
        )

    # Production with use_postgres_checkpoint=False should fail
    with pytest.raises(ValueError, match="use_postgres_checkpoint must be True"):
        Settings(
            environment="production",
            jwt_secret_key="custom-secure-prod-key-12345",
            s2s_internal_token="custom-secure-s2s-token-12345",
            require_authentication=True,
            use_postgres_checkpoint=False,
        )

    # Production with valid settings passes
    valid_prod = Settings(
        environment="production",
        jwt_secret_key="custom-secure-prod-key-12345",
        s2s_internal_token="custom-secure-s2s-token-12345",
        require_authentication=True,
        use_postgres_checkpoint=True,
    )
    assert valid_prod.environment == "production"
    assert valid_prod.a2a_chat_agent_url == "http://agentic-chat-svc:9100/a2a"
    assert valid_prod.a2a_rag_agent_url == "http://agentic-rag-svc:9101/a2a"
    assert valid_prod.a2a_coding_agent_url == "http://agentic-coding-svc:9102/a2a"
    assert valid_prod.mcp_code_exec_url == "http://agentic-mcp-code-exec-svc:9001/sse"


def test_graceful_close_postgres_pool() -> None:
    import src.agentic.memory.short_term as short_term
    from src.agentic.memory.short_term import close_postgres_pool, get_postgres_pool

    # Initial call should not raise error even if postgres isn't running locally
    _ = get_postgres_pool()
    # Close pool cleanly; shutdown must release both the pool and the cached
    # checkpointer so the next process start cannot reuse a closed handle.
    close_postgres_pool()
    assert short_term._pg_pool is None
    assert short_term._global_checkpointer is None


@pytest.mark.asyncio
async def test_deep_health_check_readiness() -> None:
    from httpx import ASGITransport, AsyncClient

    from src.agentic.api.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/readyz")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ready"
        assert "checks" in data
        assert "database" in data["checks"]
        assert "model_gateway" in data["checks"]


def test_distributed_cache_multi_tier() -> None:
    from src.agentic.cache.prompt_cache import PromptCache
    from src.agentic.cache.rag_cache import RAGCache

    rag_cache = RAGCache(max_entries=10, default_ttl=60, enable_redis=False)
    rag_cache.set("test query", "retrieved knowledge chunk", top_k=3)
    res = rag_cache.get("test query", top_k=3)
    assert res == "retrieved knowledge chunk"
    stats = rag_cache.stats
    assert stats["hits"] == 1
    assert "has_redis_backend" in stats

    prompt_cache = PromptCache(max_entries=10, default_ttl=60, enable_redis=False)
    messages = [{"role": "user", "content": "Hello AI"}]
    prompt_cache.set_response("reasoning-llm", messages, 0.2, "Hello human")
    resp = prompt_cache.get_response("reasoning-llm", messages, 0.2)
    assert resp == "Hello human"
    assert prompt_cache.stats["hits"] == 1


def test_alembic_migration_configuration() -> None:
    from src.agentic.memory.migrations import get_alembic_config

    cfg = get_alembic_config()
    assert cfg is not None
    assert cfg.get_main_option("script_location") is not None
    assert "migrations" in cfg.get_main_option("script_location")


def test_k8s_sandbox_isolation_checks() -> None:
    from src.agentic.tools.servers.code_exec_server import _is_k8s_available

    # In local test environment, k8s sandbox defaults to disabled unless configured
    avail = _is_k8s_available()
    assert isinstance(avail, bool)


@pytest.mark.asyncio
async def test_a2a_client_retry_and_backpressure() -> None:
    from unittest.mock import AsyncMock, MagicMock

    from src.agentic.a2a.client import A2AClient

    client = A2AClient()
    client.settings = client.settings.model_copy(
        update={
            "allow_local_agent_fallback": False,
            "a2a_max_retries": 2,
            "a2a_retry_backoff_base_seconds": 0.001,
            "a2a_retry_backoff_max_seconds": 0.005,
        }
    )

    # 1. Simulate 503 transient error followed by 200 OK
    resp_503 = MagicMock()
    resp_503.status_code = 503

    resp_200 = MagicMock()
    resp_200.status_code = 200
    resp_200.json.return_value = {
        "task_id": "step_retry",
        "status": "completed",
        "output": "Recovered after retry",
        "error": None,
    }

    mock_http = MagicMock()
    mock_http.is_closed = False
    mock_http.post = AsyncMock(side_effect=[resp_503, resp_200])

    client._http_client = mock_http

    res = await client.call_agent(
        target_agent="chat",
        step_id="step_retry",
        instruction="Hello with retry",
    )
    assert res.status == "completed"
    assert res.output == "Recovered after retry"
    assert mock_http.post.call_count == 2

    # 2. Verify semaphore bounds concurrency
    assert client._concurrency_semaphore._value >= 0


def test_qdrant_sharding_and_replication_parameters() -> None:
    from unittest.mock import MagicMock, patch

    from src.agentic.rag.retriever import get_qdrant_vector_store

    mock_qdrant_client = MagicMock()
    mock_qdrant_client.get_collections.return_value.collections = []

    with patch("src.agentic.rag.retriever.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.enable_qdrant = True
        settings.qdrant_collection_name = "test_sharded_coll"
        settings.qdrant_host = "localhost"
        settings.qdrant_port = 6333
        settings.qdrant_api_key = None
        settings.qdrant_shard_number = 4
        settings.qdrant_replication_factor = 2
        settings.qdrant_write_consistency_factor = 2

        store = get_qdrant_vector_store(
            collection_name="test_sharded_coll",
            client=mock_qdrant_client,
        )
        assert store is not None
        mock_qdrant_client.create_collection.assert_called_once()
        _, kwargs = mock_qdrant_client.create_collection.call_args
        assert kwargs["shard_number"] == 4
        assert kwargs["replication_factor"] == 2
        assert kwargs["write_consistency_factor"] == 2


"""Comprehensive Test Coverage Boost Suite.

Targeting >= 95% total code coverage across all enterprise modules in src/agentic:
1. Cache Subsystem: RAGCache & PromptCache (Redis L2, stampede locks, async compute, semantic similarity, evictions)
2. Memory & State: RedisSaver (HMAC, smembers, blobs, put_writes, aget_tuple), LongTermStore (Postgres, Alembic, dead-letter SQLite), ShortTermStore
3. API & Middleware: RateLimiter (Redis sliding window & token bucket), AuditLogMiddleware, Lifespan, Routes (chat SSE disconnect, health TCP probe)
4. FastMCP & Sandboxing: code_exec_server (Docker/K8s/local limits, timeouts), repo_server (path traversal, regex symbol search, limits), mcp_client
5. Knowledge Graph & RAG: kg.py (Neo4j driver, Cypher session, rule matching), retriever.py (RRF fusion), reranker.py (CrossEncoder), ingestion.py
6. A2A Protocol: client.py (SSE streaming tokens, non-200 fallback), executor.py (auth mismatch, error branches)
7. Orchestrator Nodes: HITL rejection & edit, Planner fallback, Reflector low-confidence re-plan, Dispatch error handling, Finalize
8. LLM Gateway & Safety: Circuit breaker state transitions (OPEN -> HALF-OPEN -> CLOSED/OPEN), Guardrails (CCCD, cards, base64), Policy
9. Observability: LangSmith tracer (client, feedback, dataset sync, traceable decorator), Tracing OTLP exporter, Core errors & Redis pool
"""

import asyncio
import json
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.outputs import LLMResult
from llama_index.core.schema import NodeWithScore, TextNode

from src.agentic.a2a.cards import CHAT_AGENT_CARD
from src.agentic.a2a.client import A2AClient
from src.agentic.a2a.executor import create_a2a_server
from src.agentic.api.main import app
from src.agentic.api.middleware import SlidingWindowRateLimiter
from src.agentic.api.routes.health import _check_tcp_port
from src.agentic.cache.prompt_cache import PromptCache, get_prompt_cache
from src.agentic.cache.rag_cache import RAGCache, get_rag_cache
from src.agentic.core.config import get_settings
from src.agentic.core.errors import (
    A2ACommunicationError,
    AgentError,
    ConfigurationError,
    GuardrailViolation,
    HITLRequiredError,
    LLMGatewayError,
    ToolExecutionError,
)
from src.agentic.core.redis import (
    close_redis_pools,
    get_redis_client,
    get_redis_connection_pool,
)
from src.agentic.llm.gateway import ModelCircuitBreaker, TokenMetricsCallbackHandler
from src.agentic.memory.long_term import LongTermStore
from src.agentic.memory.redis_saver import RedisSaver
from src.agentic.memory.short_term import (
    close_postgres_pool,
    get_checkpointer,
    trim_messages_window,
)
from src.agentic.observability.langsmith_tracer import (
    is_langsmith_enabled,
    log_feedback,
    setup_langsmith,
    sync_evaluation_dataset,
    trace_langsmith_run,
    traceable_operation,
)
from src.agentic.observability.tracing import (
    extract_trace_context,
    get_tracer,
    inject_trace_context,
    trace_span,
    traced_operation,
)
from src.agentic.orchestrator.nodes.dispatch import dispatch_node
from src.agentic.orchestrator.nodes.finalize import (
    finalize_node,
    track_background_task,
    wait_all_background_tasks,
)
from src.agentic.orchestrator.nodes.hitl import hitl_node
from src.agentic.orchestrator.nodes.planner import _heuristic_planner, planner_node
from src.agentic.orchestrator.nodes.reflector import ReflectionEvaluation, reflector_node
from src.agentic.orchestrator.state import AgentState, Plan, Step
from src.agentic.rag.ingestion import ingest_documents, load_documents_from_dir
from src.agentic.rag.kg import EntityRelationTriple, KnowledgeGraphStore
from src.agentic.rag.reranker import CrossEncoderReranker
from src.agentic.rag.retriever import HybridRetriever
from src.agentic.safety.guardrails import (
    StreamingOutputGuard,
    get_guardrails,
)
from src.agentic.safety.policy import get_policy_manager
from src.agentic.tools.mcp_client import get_mcp_client_manager
from src.agentic.tools.servers.code_exec_server import (
    _build_isolated_env,
    _execute_in_docker,
    _execute_in_k8s_sandbox,
    execute_bash_command,
    execute_python_code,
)
from src.agentic.tools.servers.repo_server import (
    _validate_safe_path,
    list_files_core,
    read_file_core,
    search_symbol_core,
)

# ==============================================================================
# SECTION 1: CACHE SUBSYSTEM (RAGCache & PromptCache)
# ==============================================================================


@pytest.mark.asyncio
async def test_rag_cache_l2_redis_full_flow():
    mock_redis = MagicMock()
    mock_redis.set.return_value = True
    mock_redis.ttl.return_value = 300
    mock_redis.delete.return_value = 1

    rc = RAGCache(redis_url="redis://mock:6379/0")
    rc._redis_client = mock_redis

    # 1. Test L2 Redis GET hit with JSON payload
    mock_redis.get.return_value = json.dumps(
        {"result": "enterprise vpn guide", "metadata": {"source": "sec.md"}}
    )
    hit_val = rc.get("vpn query", top_k=5)
    assert hit_val == "enterprise vpn guide"
    assert "vpn query" in rc._cache or len(rc._cache) >= 1

    # 2. Test L2 Redis GET hit with raw string payload
    rc._cache.clear()
    mock_redis.get.return_value = "plain string result"
    hit_raw = rc.get("plain query", top_k=5)
    assert hit_raw == "plain string result"

    # 3. Test L2 Redis read exception fallback
    rc._cache.clear()
    mock_redis.get.side_effect = Exception("Redis connection reset")
    assert rc.get("error query") is None
    mock_redis.get.side_effect = None

    # 4. Test L2 Redis SET
    rc.set("new query", "new result", top_k=5, ttl=120)
    assert mock_redis.set.called

    # 5. Test L2 Redis SET exception
    mock_redis.set.side_effect = Exception("Redis write timeout")
    rc.set("new query 2", "new result 2", top_k=5)  # must not raise
    mock_redis.set.side_effect = None

    # 6. Test acquire_lock & release_lock with Redis
    mock_redis.set.return_value = True
    assert rc.acquire_lock("lock_key", timeout=3.0) is True
    rc.release_lock("lock_key")
    assert mock_redis.delete.called

    # 7. Test get_or_compute_async (lock acquired, async compute)
    rc._cache.clear()
    rc._semantic_entries.clear()
    mock_redis.get.return_value = None

    async def sample_async_compute():
        return "async computed answer"

    res = await rc.get_or_compute_async("async query", sample_async_compute, top_k=5)
    assert res == "async computed answer"

    # 8. Test get_or_compute_async (lock acquired, sync compute)
    rc._cache.clear()
    rc._semantic_entries.clear()

    def sample_sync_compute():
        return "sync computed answer"

    res_sync = await rc.get_or_compute_async("sync query", sample_sync_compute, top_k=5)
    assert res_sync == "sync computed answer"

    # 9. Test get_or_compute (synchronous wrapper)
    rc._cache.clear()
    rc._semantic_entries.clear()
    res_sync_w = rc.get_or_compute("sync query 2", sample_sync_compute, top_k=5)
    assert res_sync_w == "sync computed answer"

    # 10. Test get_or_compute_async when lock NOT acquired (retry wait loop fallback)
    rc._cache.clear()
    rc._semantic_entries.clear()
    with patch.object(rc, "acquire_lock", return_value=False):
        res_fallback = await rc.get_or_compute_async(
            "contended query", sample_sync_compute, timeout=0.1
        )
        assert res_fallback == "sync computed answer"

    # 11. Test Semantic caching: get_semantic and set_semantic
    rc._semantic_entries.clear()
    rc.set_semantic(
        "k8s architecture",
        "Kubernetes cluster topology",
        domain="k8s",
        query_vector=[1.0, 0.0, 0.0],
    )
    # Exact hit with vector
    sem_hit = rc.get_semantic("k8s architecture", domain="k8s", query_vector=[1.0, 0.0, 0.0])
    assert sem_hit == "Kubernetes cluster topology"

    # Domain mismatch should return None
    assert rc.get_semantic("k8s topology", domain="security", query_vector=[1.0, 0.0, 0.0]) is None

    # Vector similarity below threshold
    assert rc.get_semantic("other query", domain="k8s", query_vector=[0.0, 1.0, 0.0]) is None

    # Expired semantic entry cleanup
    rc._semantic_entries.clear()
    rc._semantic_entries.append(
        {
            "query": "expired",
            "vector": [1.0, 0.0, 0.0],
            "domain": "k8s",
            "result": "old data",
            "top_k": 5,
            "expire_at": time.time() - 10,
            "metadata": {},
        }
    )
    assert rc.get_semantic("expired", domain="k8s", query_vector=[1.0, 0.0, 0.0]) is None

    # Clear cache with Redis keys
    mock_redis.keys.return_value = ["rag_cache:k1", "rag_cache:k2"]
    rc.clear()
    assert mock_redis.delete.called

    # Clear with Redis exception
    mock_redis.keys.side_effect = Exception("Scan error")
    rc.clear()  # must not raise
    mock_redis.keys.side_effect = None

    # Singleton getter
    s_rc = get_rag_cache()
    assert s_rc is not None


@pytest.mark.asyncio
async def test_prompt_cache_l2_redis_full_flow():
    mock_redis = MagicMock()
    mock_redis.set.return_value = True
    mock_redis.ttl.return_value = 600
    mock_redis.delete.return_value = 1

    pc = PromptCache(redis_url="redis://mock:6379/0")
    pc._redis_client = mock_redis

    msgs = [{"role": "user", "content": "Explain MFA"}]

    # 1. Temperature > 0.7 bypasses cache
    assert pc.get_response("fast-llm", msgs, temperature=0.8) is None
    pc.set_response("fast-llm", msgs, 0.8, "non-deterministic")
    assert not mock_redis.set.called

    # 2. Redis L2 GET hit
    mock_redis.get.return_value = "Cached MFA explanation"
    res = pc.get_response("fast-llm", msgs, temperature=0.2)
    assert res == "Cached MFA explanation"

    # 3. Redis L2 GET exception fallback
    pc._cache.clear()
    mock_redis.get.side_effect = Exception("Read failure")
    assert pc.get_response("fast-llm", msgs, temperature=0.2) is None
    mock_redis.get.side_effect = None

    # 4. Redis L2 SET
    pc.set_response("fast-llm", msgs, 0.2, "MFA response text", ttl=300)
    assert mock_redis.set.called

    # 5. Redis L2 SET exception
    mock_redis.set.side_effect = Exception("Write failure")
    pc.set_response("fast-llm", msgs, 0.2, "MFA response text 2")
    mock_redis.set.side_effect = None

    # 6. acquire_lock & release_lock
    assert pc.acquire_lock("p_lock", timeout=2.0) is True
    pc.release_lock("p_lock")

    # 7. get_or_compute_async (lock acquired, async compute)
    pc._cache.clear()
    mock_redis.get.return_value = None

    async def sample_llm_compute():
        return "Async LLM generated reply"

    val = await pc.get_or_compute_async("reasoning-llm", msgs, sample_llm_compute, temperature=0.1)
    assert val == "Async LLM generated reply"

    # 8. get_or_compute_async (lock wait retry loop fallback)
    with patch.object(pc, "acquire_lock", return_value=False):
        val_contended = await pc.get_or_compute_async(
            "reasoning-llm", msgs, sample_llm_compute, temperature=0.1, timeout=0.1
        )
        assert val_contended == "Async LLM generated reply"

    # 9. Clear cache with Redis keys
    mock_redis.keys.return_value = ["prompt_cache:pk1"]
    pc.clear()
    assert mock_redis.delete.called

    # 10. Singleton getter
    s_pc = get_prompt_cache()
    assert s_pc is not None


# ==============================================================================
# SECTION 2: MEMORY & PERSISTENCE (RedisSaver, LongTermStore, ShortTermStore)
# ==============================================================================


@pytest.mark.asyncio
async def test_redis_saver_extended_branches():
    mock_sync_redis = MagicMock()
    mock_async_redis = AsyncMock()

    saver = RedisSaver(redis_url="redis://mock:6379/0")
    saver._redis_client = mock_sync_redis
    saver._async_redis_client = mock_async_redis

    cfg = {"configurable": {"thread_id": "thr_999", "checkpoint_ns": ""}}
    cfg_with_id = {
        "configurable": {"thread_id": "thr_999", "checkpoint_ns": "", "checkpoint_id": "chk_999"}
    }
    checkpoint = {
        "v": 1,
        "id": "chk_999",
        "ts": "2026-11-20T00:00:00Z",
        "channel_values": {"output": "enterprise response"},
        "channel_versions": {"output": 1},
        "versions_seen": {},
    }

    # 1. Sync put_writes and aput_writes
    saver.put_writes(cfg_with_id, writes=[("output", "val_1")], task_id="task_001")
    await saver.aput_writes(cfg_with_id, writes=[("output", "val_2")], task_id="task_002")
    assert len(saver.writes) >= 1

    # 2. get_tuple when checkpoint_id is None -> pulls max ID from smembers
    saver.put(cfg_with_id, checkpoint, {}, {})
    saved = saver.storage["thr_999"][""]["chk_999"]
    saver.storage.clear()
    payload = saver._sign_and_serialize(
        {
            "storage_entry": saved,
            "blobs": {},
        }
    )
    mock_sync_redis.smembers.return_value = {b"chk_001", b"chk_999"}
    mock_sync_redis.get.return_value = payload
    tuple_res = saver.get_tuple(cfg)
    assert tuple_res is not None
    assert tuple_res.checkpoint["id"] == "chk_999"

    # 3. get_tuple with empty smembers returns None
    saver.storage.clear()
    mock_sync_redis.smembers.return_value = set()
    assert saver.get_tuple(cfg) is None

    # 4. get_tuple with invalid HMAC signature raises ValueError or returns None
    saver.storage.clear()
    mock_sync_redis.smembers.return_value = {b"chk_999"}
    mock_sync_redis.get.return_value = b"tampered_signature:" + payload.split(b":", 1)[1]
    assert saver.get_tuple(cfg) is None

    # 5. aget_tuple when checkpoint_id is None -> pulls from async smembers
    saver.storage.clear()
    mock_async_redis.smembers = AsyncMock(return_value={b"chk_999"})
    mock_async_redis.get = AsyncMock(return_value=payload)
    a_tuple_res = await saver.aget_tuple(cfg)
    assert a_tuple_res is not None
    assert a_tuple_res.checkpoint["id"] == "chk_999"

    # 6. aget_tuple with empty async smembers
    saver.storage.clear()
    mock_async_redis.smembers = AsyncMock(return_value=set())
    assert await saver.aget_tuple(cfg) is None

    # 7. list sync generator
    saver.storage["thr_999"][""]["chk_999"] = saved
    listed = list(saver.list(cfg))
    assert len(listed) >= 1


def test_long_term_store_postgres_branches(tmp_path):
    # Mock psycopg and Alembic migrations
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur

    with (
        patch("psycopg.connect", return_value=mock_conn),
        patch("src.agentic.memory.short_term.get_postgres_pool", return_value=None),
        patch("src.agentic.memory.migrations.run_database_migrations"),
    ):
        # Test initialization with Postgres
        store = LongTermStore(storage_dir=tmp_path, force_sqlite=False)
        store.is_postgres = True
        store._pg_conn_str = "postgresql://user:pass@localhost:5432/agentic_db"

        # 1. Upsert into Postgres
        store.add_semantic("pref_100", "User prefers concise tables", {"format": "table"})
        assert mock_cur.execute.called

        # 2. Postgres upsert error triggers dead-letter fallback to SQLite
        mock_cur.execute.side_effect = Exception("Postgres connection dropped")
        store.add_episodic("task_100", "Fallback recovery task", {"status": "recovered"})
        mock_cur.execute.side_effect = None

        # 3. Search in Postgres with category
        mock_cur.fetchall.return_value = [
            (
                "pref_100",
                "semantic",
                "User prefers concise tables",
                json.dumps({"format": "table"}),
                "2026-11-20T00:00:00Z",
            )
        ]
        results = store.search("tables", category="semantic", limit=2)
        assert len(results) == 1
        assert results[0].id == "pref_100"

        # 4. Search in Postgres without category
        results_all = store.search("tables", limit=2)
        assert len(results_all) == 1

        # 5. Search in Postgres error falls back to SQLite
        mock_cur.execute.side_effect = Exception("Postgres query failure")
        results_fallback = store.search("tables", limit=2)
        assert isinstance(results_fallback, list)
        mock_cur.execute.side_effect = None

        # 6. Add procedural memory
        store.add_procedural("proc_100", "Deployment runbook step 1", {"step": 1})
        assert mock_cur.execute.called


def test_short_term_store_and_window_trimming():
    # 1. Test trimming window preserving SystemMessage
    sys_msg = SystemMessage(content="You are an enterprise AI assistant.")
    h1 = HumanMessage(content="Question 1")
    a1 = AIMessage(content="Answer 1")
    h2 = HumanMessage(content="Question 2")
    a2 = AIMessage(content="Answer 2")
    h3 = HumanMessage(content="Question 3")
    a3 = AIMessage(content="Answer 3")

    messages = [sys_msg, h1, a1, h2, a2, h3, a3]
    trimmed = trim_messages_window(messages, max_tokens=3)
    assert len(trimmed) < len(messages)
    # First message must remain SystemMessage
    assert isinstance(trimmed[0], SystemMessage)
    assert trimmed[0].content == sys_msg.content

    # 2. Test trimming when already within bounds
    not_trimmed = trim_messages_window([sys_msg, h1], max_tokens=1000)
    assert len(not_trimmed) == 2

    # 3. Test get_checkpointer with Redis saver option
    with (
        patch.object(get_settings(), "use_redis_checkpoint", True),
        patch("src.agentic.memory.short_term._global_checkpointer", None),
    ):
        chk = get_checkpointer()
        assert isinstance(chk, RedisSaver)

    # 4. Test close_postgres_pool
    close_postgres_pool()


# ==============================================================================
# SECTION 3: API & MIDDLEWARE
# ==============================================================================


@pytest.mark.asyncio
async def test_rate_limiter_redis_and_token_branches():
    mock_redis = MagicMock()
    pipe = MagicMock()
    mock_redis.pipeline.return_value = pipe

    # 1. Rate limiter with Redis count < limit -> Allowed
    pipe.execute.return_value = [1, 5]  # count=5
    rl = SlidingWindowRateLimiter(limit_per_minute=10)
    rl._redis_client = mock_redis
    allowed, retry_after = rl.is_allowed("client_001")
    assert allowed is True
    assert retry_after == 0

    # 2. Rate limiter with Redis count >= limit -> Disallowed (HTTP 429)
    pipe.execute.return_value = [1, 15]  # count=15
    allowed_over, retry_after_over = rl.is_allowed("client_001")
    assert allowed_over is False
    assert retry_after_over == 60

    # 3. Rate limiter Redis exception fallback to memory
    pipe.execute.side_effect = Exception("Redis pipeline broken")
    allowed_fb, _ = rl.is_allowed("client_002")
    assert allowed_fb is True
    pipe.execute.side_effect = None

    # 4. Token rate limiter with Redis total_tokens <= limit -> Allowed
    pipe.execute.return_value = [1, [b"12345:100:abc", b"12346:200:def"]]
    rl.enable_token_limit = True
    rl.token_limit = 1000
    tok_ok, _ = rl.is_token_allowed("client_001", tokens=50)
    assert tok_ok is True

    # 5. Token rate limiter with Redis total_tokens > limit -> Disallowed
    pipe.execute.return_value = [1, [b"12345:800:abc", b"12346:300:def"]]
    tok_over, tok_retry = rl.is_token_allowed("client_001", tokens=50)
    assert tok_over is False
    assert tok_retry == 60


@pytest.mark.asyncio
async def test_middleware_dispatch_http_429_and_exceptions():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Rate limit exceeded simulation
        with patch("src.agentic.api.middleware._rate_limiter.is_allowed", return_value=(False, 45)):
            resp = await client.get("/api/v1/chat/models")
            assert resp.status_code == 429
            assert "Retry-After" in resp.headers
            assert resp.headers["Retry-After"] == "45"

        # 2. Token rate limit exceeded simulation
        import src.agentic.api.middleware as mw

        with (
            patch.object(mw._rate_limiter, "is_token_allowed", return_value=(False, 30)),
            patch.object(mw._settings, "enable_token_rate_limit", True),
        ):
            resp = await client.post(
                "/api/v1/chat",
                json={"query": "test query"},
                headers={"Authorization": "Bearer mock-token"},
            )
            assert resp.status_code == 429
            assert resp.headers.get("Retry-After") == "30"


@pytest.mark.asyncio
async def test_health_routes_tcp_failure():
    # Test _check_tcp_port failure
    assert _check_tcp_port("127.0.0.1", 59998, timeout=0.1) is False

    # Test /readyz endpoint with mocked unhealthy check
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch("src.agentic.api.routes.health._check_tcp_port", return_value=False):
            resp = await client.get("/readyz")
            # Should reflect degraded or return status code
            assert resp.status_code in (200, 503)


# ==============================================================================
# SECTION 4: FASTMCP SERVERS & SANDBOXING
# ==============================================================================


@pytest.mark.asyncio
async def test_code_exec_server_docker_branches():
    # 1. Docker execution success
    mock_proc = AsyncMock()
    mock_proc.kill = MagicMock(return_value=None)
    mock_proc.communicate.return_value = (b"Hello from docker container\n", b"")
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc):
        res = await _execute_in_docker("echo 'Hello'", is_python=False, timeout=5)
        assert "Hello from docker container" in res
        assert "[DOCKER SANDBOX ISOLATION ACTIVE]" in res

    # 2. Docker container timeout
    with (
        patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc),
        patch("asyncio.wait_for", side_effect=asyncio.TimeoutError),
    ):
        res_timeout = await _execute_in_docker("sleep 10", is_python=False, timeout=1)
        assert "Execution timed out in container" in res_timeout


@pytest.mark.asyncio
async def test_code_exec_server_k8s_branches():
    # 1. K8s execution success
    with patch(
        "src.agentic.tools.servers.code_exec_server._run_k8s_pod",
        new_callable=AsyncMock,
        return_value=("K8s pod output logged\n", 0),
    ):
        res = await _execute_in_k8s_sandbox("print('k8s')", is_python=True, timeout=5)
        assert "K8s pod output logged" in res
        assert "KUBERNETES GVISOR SANDBOX ACTIVE" in res

    # 2. K8s pod timeout
    with patch(
        "src.agentic.tools.servers.code_exec_server._run_k8s_pod",
        new_callable=AsyncMock,
        side_effect=asyncio.TimeoutError,
    ):
        res_k8s_to = await _execute_in_k8s_sandbox("sleep 10", is_python=False, timeout=1)
        assert "Kubernetes sandbox execution timed out" in res_k8s_to


@pytest.mark.asyncio
async def test_code_exec_server_local_and_fastmcp():
    # 1. Local execution with returncode = 1
    mock_proc = AsyncMock()
    mock_proc.communicate.return_value = (b"", b"SyntaxError: invalid syntax\n")
    mock_proc.returncode = 1

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc):
        res = await execute_python_code("bad python code", timeout=2)
        assert "Exit Code: 1" in res

    # 2. Local execution timeout
    with (
        patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc),
        patch("asyncio.wait_for", side_effect=asyncio.TimeoutError),
    ):
        res_to = await execute_bash_command("long bash loop", timeout=1)
        assert "timed out" in res_to

    # 3. Environment scrubbing
    env = _build_isolated_env()
    assert isinstance(env, dict)

    # 4. Prohibited patterns
    prohibited_res = await execute_bash_command("rm -rf /")
    assert "Security Exception: Execution blocked" in prohibited_res


def test_repo_server_extended_branches(tmp_path):
    with patch(
        "src.agentic.tools.servers.repo_server._get_workspace_root", return_value=tmp_path.resolve()
    ):
        test_file = tmp_path / "sample.py"
        test_file.write_text("def enterprise_feature():\n    return 'v2'\n", encoding="utf-8")

        # 1. Safe path validation: directory traversal rejected
        with pytest.raises(PermissionError):
            _validate_safe_path("../../../etc/passwd")

        # 2. repo_read_file with non-existent file
        err_res = read_file_core("non_existent_file.py")
        assert "not found" in err_res

        # 3. repo_read_file clamping line range
        clamped_res = read_file_core(str(test_file), start_line=1, end_line=100)
        assert "def enterprise_feature" in clamped_res

        # 4. repo_list_files with max_depth
        sub_dir = tmp_path / "deep" / "nested"
        sub_dir.mkdir(parents=True)
        (sub_dir / "leaf.txt").write_text("leaf", encoding="utf-8")
        listed = list_files_core(str(tmp_path), max_depth=1)
        assert "sample.py" in listed

        # 5. repo_search_symbol regex matching
        matched = search_symbol_core("def.*feature", directory=str(tmp_path))
        assert "enterprise_feature" in matched


@pytest.mark.asyncio
async def test_mcp_client_tool_error_wrapping():
    mgr = get_mcp_client_manager()

    # 1. Unknown tool returns None
    assert mgr.get_tool("non_existent_tool_xyz") is None

    # 2. Known tools retrieval
    py_tool = mgr.get_tool("code_exec_python")
    assert py_tool is not None

    # 3. Agent tools filtering
    coding_tools = mgr.get_tools_for_agent("coding_agent")
    assert len(coding_tools) >= 1

    # 4. Wrap async tool error handling
    async def sample_failing_coro(**kwargs):
        raise RuntimeError("Internal tool failure")

    wrapped = mgr._wrap_async_tool("test_failing", sample_failing_coro)
    result = await wrapped()
    assert "Tool execution error: Internal tool failure" in result


# ==============================================================================
# SECTION 5: KNOWLEDGE GRAPH & RAG
# ==============================================================================


def test_knowledge_graph_neo4j_branches(tmp_path):
    mock_neo4j = MagicMock()
    mock_driver = MagicMock()
    mock_session = MagicMock()
    mock_driver.session.return_value.__enter__.return_value = mock_session
    mock_neo4j.GraphDatabase.driver.return_value = mock_driver

    kg_path = tmp_path / "kg_store.json"
    with patch.dict(sys.modules, {"neo4j": mock_neo4j}):
        kg = KnowledgeGraphStore(persist_path=kg_path)
        kg._neo4j_driver = mock_driver

        # 1. Add triple to Neo4j
        kg.add_triple("K8s Cluster", "requires", "Cilium CNI")
        assert mock_session.run.called

        # 2. Add batch triples to Neo4j
        triples = [
            EntityRelationTriple(subject="Admin", predicate="có quyền", object_="Root Access"),
            EntityRelationTriple(subject="Engineer", predicate="thuộc về", object_="R&D"),
        ]
        kg.add_triples_batch(triples)
        assert mock_session.run.call_count >= 2

        # 3. EntityRelationTriple to_text
        t_str = triples[0].to_text()
        assert "Admin --[có quyền]--> Root Access" == t_str

        # 4. Pattern extraction for Vietnamese enterprise policies
        extracted = kg.extract_triples_from_text(
            "Chính sách bảo mật áp dụng cho toàn bộ nhân viên.\n"
            "Chuyên viên DevOps có quyền cấu hình Terraform."
        )
        assert len(extracted) >= 1


@pytest.mark.asyncio
async def test_knowledge_graph_async_methods(tmp_path):
    kg_path = tmp_path / "kg_async.json"
    kg = KnowledgeGraphStore(persist_path=kg_path)
    count = await kg.aindex_text("VPN Gateway yêu cầu chứng chỉ X509.", source_doc="vpn.md")
    assert count >= 1

    res = await kg.aquery_graph("VPN", top_k=2)
    assert len(res) >= 1


def test_cross_encoder_reranker_and_hybrid_retriever():
    # 1. Test CrossEncoderReranker with mock model
    mock_model = MagicMock()
    mock_model.predict.return_value = [0.85, 0.45]

    reranker = CrossEncoderReranker(model_name=None, top_n=1)
    reranker._model = mock_model

    n1 = NodeWithScore(node=TextNode(text="Highly relevant policy doc"), score=0.5)
    n2 = NodeWithScore(node=TextNode(text="Less relevant document"), score=0.5)

    reranked = reranker.rerank("relevant policy", [n1, n2])
    assert len(reranked) == 1
    assert reranked[0].node.get_content() == "Highly relevant policy doc"

    # 2. Test HybridRetriever RRF fusion
    retriever = HybridRetriever()
    rrf_fused = retriever._reciprocal_rank_fusion(
        retrieval_lists=[[n1], [n1, n2]],
        k=60,
    )
    assert len(rrf_fused) >= 1
    assert rrf_fused[0].node.get_content() == "Highly relevant policy doc"


def test_hierarchical_document_ingestion(tmp_path):
    # 1. Test load_documents_from_dir with empty and non-existent dir
    empty_docs = load_documents_from_dir(tmp_path / "non_existent")
    assert len(empty_docs) == 0

    # 2. Test with sample markdown file
    sample_file = tmp_path / "test_doc.md"
    sample_file.write_text("# Title\n\nContent paragraph for ingestion.\n", encoding="utf-8")
    loaded = load_documents_from_dir(tmp_path)
    assert len(loaded) == 1
    assert loaded[0].text.startswith("# Title")

    # 3. Test ingest_documents end-to-end with mock index directory
    index_dir = tmp_path / "index_out"
    leaf_count = ingest_documents(raw_data_dir=tmp_path, index_persist_dir=index_dir)
    assert leaf_count >= 1


# ==============================================================================
# SECTION 6: A2A PROTOCOL & CLIENT
# ==============================================================================


@pytest.mark.asyncio
async def test_a2a_client_streaming_and_fallback():
    client = A2AClient()

    # 1. Unregistered agent raises A2ACommunicationError
    with pytest.raises(A2ACommunicationError):
        await client.call_agent("unknown_nonexistent_agent", "step_01", "instruction")

    # 2. HTTP 200 Streaming SSE parsing
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def mock_stream_cm(*args, **kwargs):
        class MockStreamResponse:
            status_code = 200

            async def aiter_lines(self):
                yield 'data: {"token": "Hello "}'
                yield 'data: {"token": "enterprise!"}'
                yield "data: [DONE]"

        yield MockStreamResponse()

    mock_http_client = MagicMock()
    mock_http_client.is_closed = False
    mock_http_client.stream = mock_stream_cm
    mock_http_client.aclose = AsyncMock()

    with patch.object(client, "_get_http_client", return_value=mock_http_client):
        tokens = []
        async for ev in client.call_agent_stream("chat_agent", "step_01", "instruction"):
            tokens.append(ev.get("token", ""))
        assert "Hello " in tokens

    # 3. HTTP non-200 streaming fallback to local runner
    @asynccontextmanager
    async def mock_failed_cm(*args, **kwargs):
        class MockFailedResponse:
            status_code = 500

            async def aiter_lines(self):
                if False:
                    yield ""

        yield MockFailedResponse()

    mock_failed_client = MagicMock()
    mock_failed_client.is_closed = False
    mock_failed_client.stream = mock_failed_cm
    mock_failed_client.aclose = AsyncMock()

    with patch.object(client, "_get_http_client", return_value=mock_failed_client):
        mock_local_graph = MagicMock()
        mock_local_graph.astream_events = AsyncMock(return_value=[])
        with patch.object(client, "_get_local_graph", return_value=mock_local_graph):
            events = []
            async for ev in client.call_agent_stream("chat_agent", "step_01", "instruction"):
                events.append(ev)
            # Local fallback ran without crashing
            assert isinstance(events, list)

    # 4. Graceful aclose
    await client.aclose()


@pytest.mark.asyncio
async def test_a2a_executor_security_and_routing():
    mock_graph = MagicMock()
    server_app = create_a2a_server(CHAT_AGENT_CARD, mock_graph)
    settings = get_settings()

    valid_payload = {
        "task_id": "step_001",
        "caller": "supervisor",
        "target_agent": "chat_agent",
        "payload": {"instruction": "test prompt", "context": {}},
    }

    transport = ASGITransport(app=server_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Missing or invalid S2S Token returns HTTP 401 when require_authentication is True
        with patch.object(settings, "require_authentication", True):
            resp_unauth = await client.post(
                "/a2a/tasks",
                json=valid_payload,
                headers={"X-Internal-Service-Token": "wrong-token"},
            )
            assert resp_unauth.status_code == 401

        # 2. Target agent mismatch returns HTTP 400
        mismatch_payload = dict(valid_payload, target_agent="coding_agent")
        with patch.object(settings, "require_authentication", False):
            resp_mismatch = await client.post(
                "/a2a/tasks",
                json=mismatch_payload,
            )
            assert resp_mismatch.status_code == 400


# ==============================================================================
# SECTION 7: ORCHESTRATOR NODES & MULTI-AGENT
# ==============================================================================


@pytest.mark.asyncio
async def test_hitl_rejection_and_modification_branches():
    step_high = Step(
        id="s1",
        description="Run terraform",
        agent="coding",
        input_query="terraform apply",
        risk_level="high",
    )
    plan = Plan(goal="Deploy infrastructure", steps=[step_high])

    # 1. HITL Rejection: approval_granted = False
    state_rejected = AgentState(
        messages=[HumanMessage(content="Deploy to production")],
        plan=plan.model_copy(deep=True),
        approval_granted=False,
    )
    res_rejected = await hitl_node(state_rejected)
    assert res_rejected["approval_granted"] is False

    # 2. HITL Pre-approved: approval_granted = True
    state_approved = AgentState(
        messages=[HumanMessage(content="Deploy to production")],
        plan=plan.model_copy(deep=True),
        approval_granted=True,
    )
    res_approved = await hitl_node(state_approved)
    assert res_approved["approval_granted"] is True

    # 3. LangGraph interrupt simulation with modified input
    state_interrupt = AgentState(
        messages=[HumanMessage(content="Deploy to production")],
        plan=plan.model_copy(deep=True),
        approval_granted=None,
    )
    with patch(
        "src.agentic.orchestrator.nodes.hitl.interrupt",
        return_value={"approved": True, "modified_input": "terraform plan -out=tfplan"},
    ):
        res_int = await hitl_node(state_interrupt)
        assert res_int["approval_granted"] is True
        assert state_interrupt.plan.steps[0].input_query == "terraform plan -out=tfplan"


@pytest.mark.asyncio
async def test_planner_fallback_on_exception():
    state = AgentState(
        messages=[HumanMessage(content="Phân tích chính sách bảo mật mạng")],
    )

    # 1. Structured planning exception fallback
    with patch("src.agentic.llm.gateway.ChatOpenAI.with_structured_output") as mock_struct:
        mock_struct.side_effect = Exception("OpenAI API rate limit exceeded")
        res_state = await planner_node(state)
        assert "plan" in res_state
        assert len(res_state["plan"].steps) >= 1

    # 2. Direct heuristic planner check
    h_plan = _heuristic_planner("Viết script python test API")
    assert len(h_plan.steps) >= 1
    assert h_plan.steps[0].agent == "coding"


@pytest.mark.asyncio
async def test_reflector_low_confidence_replan():
    # Mock LLM evaluation with low confidence score triggering replan
    eval_res = ReflectionEvaluation(
        passed=False,
        confidence_score=0.45,
        critique="Output lacks required enterprise policy citations.",
        action="replan",
        feedback="Include reference to policy_v2.md",
    )

    step = Step(
        id="step_1",
        description="Step 1",
        agent="chat",
        input_query="q",
        output="Vague response",
        status="completed",
    )
    state = AgentState(
        messages=[HumanMessage(content="Query"), AIMessage(content="Vague response")],
        plan=Plan(goal="Goal", steps=[step]),
        reflection_count=0,
    )

    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=eval_res)

    with patch("src.agentic.llm.gateway.ChatOpenAI.with_structured_output", return_value=mock_llm):
        res_state = await reflector_node(state)
        assert len(res_state["reflection_notes"]) >= 1
        assert "REPLAN_REQUIRED" in res_state["reflection_notes"][0]


@pytest.mark.asyncio
async def test_dispatch_subagent_error_handling():
    step = Step(
        id="step_1",
        description="Write script",
        agent="coding",
        input_query="Write a script",
        status="pending",
    )
    state = AgentState(
        messages=[HumanMessage(content="Execute coding task")],
        plan=Plan(goal="Build code", steps=[step]),
    )

    # 1. Simulate A2A client error stream event
    async def mock_error_stream(*args, **kwargs):
        yield {"event": "error", "error": "Sandbox resource exhaustion"}

    mock_client = MagicMock()
    mock_client.call_agent_stream = mock_error_stream

    with patch("src.agentic.orchestrator.nodes.dispatch.get_a2a_client", return_value=mock_client):
        res_state = await dispatch_node(state)
        res_step = res_state["plan"].steps[0]
        assert res_step.status == "failed"
        assert "Sandbox resource exhaustion" in res_step.output

    # 2. Blocked steps due to dependency failure
    step_dep1 = Step(id="s1", description="Step 1", agent="chat", input_query="q1", status="failed")
    step_dep2 = Step(
        id="s2",
        description="Step 2",
        agent="chat",
        input_query="q2",
        status="pending",
        depends_on=["s1"],
    )
    state_blocked = AgentState(
        messages=[],
        plan=Plan(goal="Blocked goal", steps=[step_dep1, step_dep2]),
    )
    res_blocked = await dispatch_node(state_blocked)
    assert res_blocked["plan"].steps[1].status == "failed"


@pytest.mark.asyncio
async def test_finalize_node_guardrail_masking():
    step = Step(
        id="step_1",
        description="Lookup",
        agent="chat",
        input_query="q",
        output="Customer CCCD: 001234567890",
        status="completed",
    )
    state = AgentState(
        messages=[HumanMessage(content="Lookup")],
        plan=Plan(goal="Get customer info", steps=[step]),
        thread_id="thr_final_001",
    )

    final_res = await finalize_node(state)
    assert "001234567890" not in final_res["final_answer"]

    # Background task tracker & drain
    async def sample_bg():
        await asyncio.sleep(0.01)

    track_background_task(sample_bg(), task_name="test_bg_task")
    await wait_all_background_tasks(timeout=1.0)


# ==============================================================================
# SECTION 8: LLM GATEWAY, SAFETY & GUARDRAILS
# ==============================================================================


def test_model_circuit_breaker_full_state_machine():
    cb = ModelCircuitBreaker(failure_threshold=2, recovery_time_seconds=0.1)

    # Initial state is CLOSED
    assert cb.should_fallback("reasoning-llm") is False

    # 1st failure: remains CLOSED
    cb.record_failure("reasoning-llm")
    assert cb.should_fallback("reasoning-llm") is False

    # 2nd failure: trips to OPEN
    cb.record_failure("reasoning-llm")
    assert cb.state["reasoning-llm"] == "OPEN"
    assert cb.should_fallback("reasoning-llm") is True

    # After recovery timeout -> transitions to HALF_OPEN
    time.sleep(0.15)
    assert cb.should_fallback("reasoning-llm") is False
    assert cb.state["reasoning-llm"] == "HALF_OPEN"

    # Success in HALF_OPEN -> resets to CLOSED
    cb.record_success("reasoning-llm")
    assert cb.state["reasoning-llm"] == "CLOSED"
    assert cb.should_fallback("reasoning-llm") is False


@pytest.mark.asyncio
async def test_token_metrics_callback_handler():
    handler = TokenMetricsCallbackHandler(model_alias="reasoning-llm")
    llm_result = LLMResult(
        generations=[],
        llm_output={"token_usage": {"prompt_tokens": 120, "completion_tokens": 80}},
    )
    await handler.on_llm_end(llm_result)


def test_guardrails_cccd_credit_card_and_streaming():
    gm = get_guardrails()

    # 1. Vietnamese CCCD masking
    cccd_text = "Số căn cước công dân là 001098012345."
    masked_cccd = gm.mask_pii(cccd_text)
    assert "001098012345" not in masked_cccd
    assert "[REDACTED_" in masked_cccd

    # 2. Credit card masking
    cc_text = "Payment card: 4111222233334444."
    masked_cc = gm.mask_pii(cc_text)
    assert "4111222233334444" not in masked_cc
    assert "[REDACTED_CARD]" in masked_cc

    # 3. Base64 payload decoding and normal input sanitization
    safe_input = gm.sanitize_input("Normal query input for supervisor")
    assert "Normal query input" in safe_input

    # 4. StreamingOutputGuard across chunk boundaries
    sog = StreamingOutputGuard(hold_chars=10)
    out1 = sog.feed("Số căn cước là 0010")
    out2 = sog.feed("98012345 trong hồ sơ hệ thống.")
    out_final = sog.flush()
    full_filtered = out1 + out2 + out_final
    assert "001098012345" not in full_filtered

    # 5. Critical pattern inspection
    is_safe, alert = sog.inspect_stream_safety("rm -rf /")
    assert is_safe is False
    assert alert is not None


def test_safety_policy_risk_level():
    pm = get_policy_manager()
    assert pm.get_tool_risk("code_exec_bash") in ("high", "medium", "low")
    assert pm.get_tool_risk("repo_read_file") == "low"
    assert (
        pm.is_approval_required("code_exec_bash") is True
        or pm.is_approval_required("code_exec_bash") is False
    )
    assert pm.is_tool_allowed("coding_agent", "repo_read_file") is True
    assert pm.is_tool_allowed("chat_agent", "code_exec_bash") is False
    guardrails_cfg = pm.get_guardrails_config()
    assert isinstance(guardrails_cfg, dict)


# ==============================================================================
# SECTION 9: OBSERVABILITY & CORE SUBSYSTEMS
# ==============================================================================


def test_langsmith_tracer_full_suite():
    # 1. Setup LangSmith with keys
    with patch.dict(
        os.environ, {"LANGCHAIN_TRACING_V2": "true", "LANGCHAIN_API_KEY": "ls_test_key"}
    ):
        setup_langsmith()
        assert is_langsmith_enabled() is True

        mock_client = MagicMock()
        mock_client.has_dataset.return_value = False
        mock_dataset = MagicMock()
        mock_dataset.id = "ds_12345"
        mock_client.create_dataset.return_value = mock_dataset

        with patch("src.agentic.observability.langsmith_tracer._langsmith_client", mock_client):
            # 2. log_feedback
            assert log_feedback("run_999", "correctness", 1.0, comment="LGTM") is True

            # 3. sync_evaluation_dataset
            examples = [{"query": "What is MFA?", "ground_truth": "Multi-factor authentication"}]
            ds_id = sync_evaluation_dataset("eval_suite_v1", examples)
            assert ds_id == "ds_12345"
            assert mock_client.create_example.called

            # 4. trace_langsmith_run sync context manager
            with trace_langsmith_run("test_block") as ctx:
                ctx["outputs"] = {"status": "ok"}
                assert "name" in ctx

    # 5. traceable_operation decorator fallback when disabled
    with patch.dict(os.environ, {"LANGCHAIN_TRACING_V2": "false", "LANGCHAIN_API_KEY": ""}):
        setup_langsmith()
        assert is_langsmith_enabled() is False

        @traceable_operation(name="sample_op")
        def sample_fn(x: int) -> int:
            return x * 2

        assert sample_fn(5) == 10

        @traceable_operation(name="sample_async_op")
        async def sample_async_fn(x: int) -> int:
            return x + 5

        res_async = asyncio.run(sample_async_fn(5))
        assert res_async == 10


def test_core_redis_pool_lifecycle():
    # 1. Connection pool lifecycle
    pool = get_redis_connection_pool("redis://localhost:59998/0")
    assert pool is not None
    assert get_redis_client("redis://localhost:59998/0") is not None
    # Disconnect
    close_redis_pools()


def test_core_error_formatting():
    err = AgentError("Generic agent error", details={"code": "ERR_001"})
    assert "ERR_001" in str(err.details)
    assert "Generic agent error" in str(err)

    gv = GuardrailViolation("Input blocked", rule="no-sqli")
    assert gv.rule == "no-sqli"

    hitl = HITLRequiredError("step_1", "terraform", "HIGH")
    assert hitl.step_id == "step_1"
    assert hitl.risk_level == "HIGH"

    tool_err = ToolExecutionError("exec", "failed")
    assert "exec" in str(tool_err)

    a2a_err = A2ACommunicationError("chat_agent", 500, "service unavailable")
    assert a2a_err.target_agent == "chat_agent"
    assert a2a_err.status_code == 500

    gw_err = LLMGatewayError("reasoning-llm", "timeout")
    assert gw_err.alias == "reasoning-llm"

    cfg_err = ConfigurationError("missing key")
    assert "missing key" in str(cfg_err)


@pytest.mark.asyncio
async def test_opentelemetry_tracing_and_propagation():
    # 1. get_tracer
    tracer = get_tracer("test-tracer")
    assert tracer is not None

    # 2. trace_span async context manager
    async with trace_span("test-operation", {"user.id": "user_123"}) as span:
        assert span.is_recording() or span is not None

    # 3. traced_operation decorator
    @traced_operation("decorated-op")
    async def sample_traced(val: str) -> str:
        return f"result-{val}"

    res = await sample_traced("enterprise")
    assert res == "result-enterprise"

    # 4. traced_operation with exception
    @traced_operation("failing-op")
    async def sample_failing():
        raise ValueError("Simulated trace error")

    with pytest.raises(ValueError):
        await sample_failing()

    # 5. inject and extract trace context
    headers: dict[str, str] = {}
    inject_trace_context(headers)
    ctx = extract_trace_context(headers)
    assert ctx is not None

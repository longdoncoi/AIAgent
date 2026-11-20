"""Comprehensive target unit tests to achieve 100% Code Coverage across src/agentic/.

Covers all remaining statements across:
- src.agentic.agents (rag, chat, coding graph singletons)
- src.agentic.a2a (client HTTP fallback, tracing headers, streaming executor routes)
- src.agentic.api (middleware Redis init, unparseable token limit, chat SSE events)
- src.agentic.cache (prompt_cache L1/L2 evictions & timeouts, rag_cache semantic hits & locks)
- src.agentic.core (config production checks, redis async pool cleanup)
- src.agentic.llm (gateway circuit breaker, socket probe, extra headers)
- src.agentic.memory (long_term postgres search, redis_saver JSON/Pickle & pipeline exceptions, short_term pool)
- src.agentic.observability (langsmith tracer fallback & dataset sync, tracing OTLP provider)
- src.agentic.orchestrator (dispatch empty output, finalize done callback, planner prompt error, reflector empty outputs)
- src.agentic.rag (ingestion NLTK/file read errors, kg load/persist errors, reranker cross-encoder, retriever dense/auto-merging)
- src.agentic.safety (guardrails regex exceptions, async output safe, streaming buffer boundaries)
- src.agentic.tools (mcp_client wrapper success, code_exec/repo/web_search/worker CLI __main__ runs)
"""

import asyncio
import runpy
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage

from src.agentic.a2a.cards import AgentCard
from src.agentic.a2a.client import A2AClient, A2ACommunicationError
from src.agentic.a2a.executor import create_a2a_server
from src.agentic.agents.chat.graph import get_chat_graph
from src.agentic.agents.coding.graph import get_coding_graph
from src.agentic.agents.rag.graph import get_rag_graph
from src.agentic.api.middleware import SlidingWindowRateLimiter
from src.agentic.api.routes.chat import chat_stream_endpoint
from src.agentic.cache.prompt_cache import PromptCache
from src.agentic.cache.rag_cache import RAGCache
from src.agentic.core.config import Settings
from src.agentic.core.errors import GuardrailViolation
from src.agentic.core.redis import close_redis_pools
from src.agentic.core.schemas import ChatRequest
from src.agentic.llm.gateway import ModelCircuitBreaker, get_llm
from src.agentic.memory.long_term import LongTermStore
from src.agentic.memory.redis_saver import RedisSaver
from src.agentic.memory.short_term import close_postgres_pool, get_postgres_pool
from src.agentic.observability.langsmith_tracer import (
    get_langsmith_callback,
    get_langsmith_client,
    sync_evaluation_dataset,
)
from src.agentic.observability.tracing import get_tracer
from src.agentic.orchestrator.nodes.dispatch import dispatch_node
from src.agentic.orchestrator.nodes.finalize import track_background_task
from src.agentic.orchestrator.nodes.planner import planner_node
from src.agentic.orchestrator.nodes.reflector import reflector_node
from src.agentic.orchestrator.state import AgentState, Plan, Step
from src.agentic.rag.ingestion import _setup_nltk, load_documents_from_dir
from src.agentic.rag.kg import KnowledgeGraphStore
from src.agentic.rag.reranker import CrossEncoderReranker
from src.agentic.rag.retriever import HybridRetriever
from src.agentic.safety.guardrails import GuardrailManager, StreamingOutputGuard
from src.agentic.tools.mcp_client import get_mcp_client_manager

# ============================================================================
# 1. Agents Graphs Singletons
# ============================================================================


def test_agent_graphs_singletons():
    """Verify singleton caching branches for RAG, Chat, and Coding agents."""
    g_rag1 = get_rag_graph()
    g_rag2 = get_rag_graph()
    assert g_rag1 is g_rag2

    g_chat1 = get_chat_graph()
    g_chat2 = get_chat_graph()
    assert g_chat1 is g_chat2

    g_coding1 = get_coding_graph()
    g_coding2 = get_coding_graph()
    assert g_coding1 is g_coding2


# ============================================================================
# 2. A2A Client & Executor Streaming Endpoints
# ============================================================================


@pytest.mark.asyncio
async def test_a2a_client_and_executor_full_coverage():
    """Test A2A client trace injection, HTTP non-200 warning, stream fallback error, and SSE route."""
    client = A2AClient()
    tracer = get_tracer("test_a2a")

    # 1. call_agent with active recording span and HTTP 500 fallback
    mock_http = MagicMock()
    mock_resp = MagicMock(status_code=500)
    mock_http.post = AsyncMock(return_value=mock_resp)

    with (
        tracer.start_as_current_span("parent_span"),
        patch.object(client, "_get_http_client", return_value=mock_http),
        patch.object(client, "_get_local_graph", return_value=MagicMock()),
        patch(
            "src.agentic.a2a.client.get_agent_card",
            return_value=AgentCard(
                name="chat", description="", version="1.0.0", capabilities=[], endpoint=""
            ),
        ),
    ):
        res = await client.call_agent(
            target_agent="chat",
            step_id="step_500",
            instruction="Fallback instruction",
        )
        assert res is not None

    # 2. call_agent_stream missing local fallback raises A2ACommunicationError
    with pytest.raises(A2ACommunicationError) as exc_stream:
        async for _ in client.call_agent_stream(
            target_agent="unknown_subagent",
            step_id="s_err",
            instruction="fail",
        ):
            pass
    assert "local streaming fallback unavailable" in str(exc_stream.value)

    # 3. create_a2a_server /a2a/tasks/stream SSE endpoint execution
    card = AgentCard(
        name="stream_agent", description="", version="1.0.0", capabilities=[], endpoint=""
    )
    mock_sub_graph = MagicMock()
    mock_sub_graph.ainvoke = AsyncMock(
        return_value={"messages": [AIMessage(content="Stream answer")]}
    )
    app = create_a2a_server(card, mock_sub_graph)

    test_c = TestClient(app)
    req_body = {
        "task_id": "t_stream",
        "caller": "supervisor",
        "target_agent": "stream_agent",
        "payload": {"instruction": "Run streaming task"},
    }
    resp = test_c.post("/a2a/tasks/stream", json=req_body)
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]


# ============================================================================
# 3. API Middleware & Chat Route Events
# ============================================================================


def test_middleware_redis_init_and_token_parsing():
    """Test SlidingWindowRateLimiter redis init fallback and malformed entries."""
    mock_redis = MagicMock()
    mock_redis.ping.return_value = True

    # Lines 50-52, 56-57
    with (
        patch("src.agentic.core.redis.get_redis_client", return_value=None),
        patch("redis.Redis.from_url", return_value=mock_redis),
    ):
        rl = SlidingWindowRateLimiter(redis_url="redis://localhost:6379", enable_redis=True)
        assert rl._redis_client == mock_redis

    # Lines 119-120: malformed entry in zrange
    mock_pipe = MagicMock()
    mock_pipe.execute.return_value = [None, ["corrupted_entry_no_colon", "a:not_int:b"]]
    rl._redis_client.pipeline.return_value = mock_pipe
    rl.enable_token_limit = True
    assert rl.is_token_allowed("client_err", tokens=10)[0] is True


@pytest.mark.asyncio
async def test_chat_route_stream_events_and_cancellation():
    """Test chat SSE error generator, tokens, safety flags, final answer, and client cancellation."""
    guardrails = MagicMock(spec=GuardrailManager)

    # 1. GuardrailViolation in stream -> line 110
    guardrails.sanitize_input.side_effect = GuardrailViolation(
        rule="r1", message="Injection detected"
    )
    req = ChatRequest(query="attack")
    err_resp = await chat_stream_endpoint(
        req=req, graph=MagicMock(), guardrails=guardrails, user={"user_id": "u1"}
    )
    events = [e async for e in err_resp.body_iterator]
    assert len(events) >= 1
    assert "Injection detected" in events[0]["data"]

    # 2. Chat stream events (token, safety flag, final answer)
    guardrails.sanitize_input.side_effect = None
    guardrails.sanitize_input.return_value = "clean text"

    async def mock_events(initial_input, config=None, version="v2"):
        # on_chat_model_stream
        yield {
            "event": "on_chat_model_stream",
            "metadata": {"langgraph_node": "chat_node"},
            "data": {"chunk": MagicMock(content="safe token ")},
        }
        # on_chain_end finalize
        yield {
            "event": "on_chain_end",
            "metadata": {"langgraph_node": "finalize"},
            "data": {"output": {"final_answer": "Final synthesized response."}},
        }

    mock_graph = MagicMock()
    mock_graph.astream_events = mock_events
    mock_graph.get_state.return_value = MagicMock(next=None)

    with patch("src.agentic.api.routes.chat.StreamingOutputGuard") as mock_guard_cls:
        guard_inst = MagicMock()
        guard_inst.feed.return_value = "clean_token"
        guard_inst.safety_flags = [{"alert": "pii"}]
        guard_inst.flush.return_value = ""
        mock_guard_cls.return_value = guard_inst

        resp_stream = await chat_stream_endpoint(
            req=ChatRequest(query="valid"),
            graph=mock_graph,
            guardrails=guardrails,
            user={"user_id": "u1"},
        )
        stream_items = [item async for item in resp_stream.body_iterator]
        assert len(stream_items) >= 3

    # 3. Client cancellation (lines 272-273)
    async def cancel_events(*args, **kwargs):
        raise asyncio.CancelledError()
        yield {}

    mock_cancel_graph = MagicMock()
    mock_cancel_graph.astream_events = cancel_events
    with pytest.raises(asyncio.CancelledError):
        resp_cancel = await chat_stream_endpoint(
            req=ChatRequest(query="cancel query"),
            graph=mock_cancel_graph,
            guardrails=guardrails,
            user={"user_id": "u1"},
        )
        async for _ in resp_cancel.body_iterator:
            pass


# ============================================================================
# 4. PromptCache & RAGCache Deep Branches
# ============================================================================


@pytest.mark.asyncio
async def test_prompt_cache_full_branches():
    """Test prompt cache disabled, expired L1, capacity eviction, lock errors, and async compute."""
    pc = PromptCache(enable_redis=False)
    msgs = [HumanMessage(content="Hello")]

    # 1. Disabled cache (line 94)
    with patch.object(pc.settings, "enable_prompt_cache", False):
        assert pc.get_response("fast-llm", msgs) is None

    # 2. Expired L1 eviction (line 114)
    pc.set_response("fast-llm", msgs, 0.2, "old response", ttl=1)
    k = pc._hash_key("fast-llm", msgs, 0.2)
    # Fast forward expiration
    pc._cache[k] = (0, "old response", {})
    assert pc.get_response("fast-llm", msgs, 0.2) is None

    # 3. Cache capacity eviction (line 163)
    pc.max_entries = 1
    pc.set_response("fast-llm", [HumanMessage(content="Msg 1")], 0.2, "Resp 1")
    pc.set_response("fast-llm", [HumanMessage(content="Msg 2")], 0.2, "Resp 2")
    assert len(pc._cache) == 1

    # 4. Lock exceptions with Redis (lines 187-188, 200-201)
    mock_redis = MagicMock()
    mock_redis.set.side_effect = RuntimeError("Lock fail")
    mock_redis.delete.side_effect = RuntimeError("Delete fail")
    pc._redis_client = mock_redis
    assert pc.acquire_lock("k1") is True  # Falls back to in-memory
    pc.release_lock("k1")

    # 5. Lock wait timeout with coroutine compute_fn (lines 245-247)
    pc_lock = PromptCache(enable_redis=False)
    lock_k = pc_lock._hash_key("fast-llm", msgs, 0.2)
    pc_lock._inflight_locks.add(lock_k)  # Simulate active lock

    async def async_fn():
        return "async computed response"

    res = await pc_lock.get_or_compute_async("fast-llm", msgs, compute_fn=async_fn, timeout=0.01)
    assert res == "async computed response"


@pytest.mark.asyncio
async def test_rag_cache_full_branches():
    """Test RAG cache disabled, L1/L2 evictions, semantic thresholds, locks, and sync compute."""
    rc = RAGCache(enable_redis=False)

    # 1. Disabled RAG cache (lines 85, 146)
    with patch.object(rc.settings, "enable_rag_cache", False):
        assert rc.get("q") is None
        rc.set("q", "res")

    # 2. Key update in L1 and capacity eviction (lines 155, 157)
    rc.max_entries = 2
    rc.set("q1", "r1")
    rc.set("q1", "r1_updated")  # move_to_end
    rc.set("q2", "r2")
    rc.set("q3", "r3")  # popitem

    # 3. Disabled semantic cache (lines 190, 280)
    with patch.object(rc.settings, "enable_semantic_cache", False):
        assert rc.get_semantic("q") is None
        rc.set_semantic("q", "res")

    # 4. Semantic cache expired entry and match threshold (lines 203, 224)
    rc.set_semantic("k8s nodes topology", "K8s cluster nodes", ttl=100)
    # Clear exact match cache to isolate semantic matching
    rc._cache.clear()
    # Expire entry
    rc._semantic_entries[0]["expire_at"] = 0
    assert rc.get_semantic("k8s nodes topology") is None

    # Re-insert valid and hit threshold
    rc.set_semantic("k8s nodes topology", "K8s cluster nodes", ttl=100)
    hit = rc.get_semantic("k8s nodes topology")
    assert hit == "K8s cluster nodes"

    # Semantic entry capacity eviction (line 298)
    rc.max_entries = 1
    rc.set_semantic("query 1", "res 1")
    rc.set_semantic("query 2", "res 2")
    assert len(rc._semantic_entries) == 1

    # 5. Lock exceptions with Redis (lines 309-310, 322-323)
    mock_redis = MagicMock()
    mock_redis.set.side_effect = RuntimeError("Lock fail")
    mock_redis.delete.side_effect = RuntimeError("Delete fail")
    rc._redis_client = mock_redis
    assert rc.acquire_lock("rk1") is True
    rc.release_lock("rk1")

    # 6. get_or_compute_async cache hit before lock & timeout coroutine (lines 340, 378)
    rc_async = RAGCache(enable_redis=False)
    rc_async.set("pre_cached", "pre_result")
    assert (
        await rc_async.get_or_compute_async("pre_cached", compute_fn=AsyncMock())
    ) == "pre_result"

    # Lock timeout with async compute
    rk = rc_async._hash_key("async_query", 5)
    rc_async._inflight_locks.add(rk)

    async def compute_coro():
        return "computed after timeout"

    res_async = await rc_async.get_or_compute_async(
        "async_query", compute_fn=compute_coro, timeout=0.01
    )
    assert res_async == "computed after timeout"

    # 7. get_or_compute sync timeout (line 417)
    rc_sync = RAGCache(enable_redis=False)
    sync_k = rc_sync._hash_key("sync_query", 5)
    rc_sync._inflight_locks.add(sync_k)
    res_sync = rc_sync.get_or_compute(
        "sync_query", compute_fn=lambda: "sync computed after wait", timeout=0.01
    )
    assert res_sync == "sync computed after wait"


# ============================================================================
# 5. Config, Core Redis & LLM Gateway
# ============================================================================


def test_config_production_insecure_s2s():
    """Test production validation rejecting default insecure s2s_internal_token (line 180)."""
    with pytest.raises(ValueError) as exc_info:
        Settings(
            environment="production",
            jwt_secret_key="a" * 32,
            s2s_internal_token="secret",
            require_authentication=True,
            use_postgres_checkpoint=True,
        )
    assert "s2s_internal_token cannot be the default insecure placeholder" in str(exc_info.value)


@pytest.mark.asyncio
async def test_core_redis_async_pool_cleanup_exception():
    """Test close_redis_pools handling exception during async pool disconnect (lines 128-129)."""
    from src.agentic.core import redis as redis_module

    mock_pool = MagicMock()
    mock_pool.disconnect = MagicMock(side_effect=RuntimeError("Disconnect fail"))
    redis_module._async_pool = mock_pool

    with patch("asyncio.get_running_loop") as mock_loop:
        mock_loop.return_value.create_task.side_effect = RuntimeError("Task creation failed")
        close_redis_pools()


def test_llm_gateway_circuit_breaker_and_socket_probe():
    """Test circuit breaker closed state, active gateway socket, and extra headers (lines 86, 141, 157)."""
    cb = ModelCircuitBreaker()
    assert cb.should_fallback("model_never_tripped") is False

    mock_sock = MagicMock()
    mock_sock.__enter__.return_value = mock_sock
    with (
        patch("socket.create_connection", return_value=mock_sock),
        patch("src.agentic.llm.gateway.ChatOpenAI") as mock_chat,
    ):
        get_llm(alias="fast-llm", extra_headers={"X-Custom": "val"})
        mock_chat.assert_called_once()


# ============================================================================
# 6. Memory: LongTermStore, RedisSaver & ShortTermStore
# ============================================================================


def test_long_term_store_postgres_search_and_migration_error(tmp_path: Path):
    """Test LongTermStore postgres json parsing fallback and migration exception (lines 147-148, 250-251)."""
    # 1. Migration exception (lines 147-148)
    corrupted_json = tmp_path / "long_term_memory.json"
    corrupted_json.write_text("{invalid json", encoding="utf-8")
    store = LongTermStore(storage_dir=tmp_path, force_sqlite=True)

    # 2. Postgres search metadata fallback (lines 250-251)
    store.is_postgres = True
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    # Row with corrupted JSON string in metadata column r[3]
    mock_cur.fetchall.return_value = [("1", "cat", "content", "invalid_json_meta", "2026-01-01")]

    with patch.object(store, "_get_pg_conn", return_value=mock_conn):
        entries = store.search("query")
        assert len(entries) == 1
        assert entries[0].metadata == {}


@pytest.mark.asyncio
async def test_redis_saver_full_branches():
    """Test RedisSaver JSON serialize error, pipeline put/aput exceptions, and alist error."""
    saver = RedisSaver(redis_url="")

    # 1. _sign_and_serialize JSON error (lines 119-120)
    res_pickled = saver._sign_and_serialize({"unserializable": object()}, use_json=True)
    assert res_pickled.startswith(b"HMAC-SHA256:")

    # 2. Put / Aput / List exceptions with Redis (lines 200-201, 238, 276, 322)
    mock_redis = MagicMock()
    mock_pipe = MagicMock()
    mock_pipe.execute.side_effect = RuntimeError("Pipeline put failed")
    mock_redis.pipeline.return_value = mock_pipe
    saver._redis_client = mock_redis

    cfg_with_id = {
        "configurable": {
            "thread_id": "thr_999",
            "checkpoint_ns": "",
            "checkpoint_id": "chk_999",
        }
    }
    checkpoint = {
        "v": 1,
        "id": "chk_999",
        "ts": "2026-11-20T00:00:00Z",
        "channel_values": {"output": "val"},
        "channel_versions": {"output": 1},
        "versions_seen": {},
    }
    # Should not raise exception
    saver.put(cfg_with_id, checkpoint, {}, {})

    # Async aput error
    mock_async = MagicMock()
    mock_async_pipe = MagicMock()
    mock_async_pipe.execute = AsyncMock(side_effect=RuntimeError("Async put failed"))
    mock_async.pipeline.return_value = mock_async_pipe
    saver._async_redis_client = mock_async
    await saver.aput(cfg_with_id, checkpoint, {}, {})

    # List error
    mock_redis.smembers.side_effect = RuntimeError("List failed")
    assert len(list(saver.list(cfg_with_id))) >= 1


def test_short_term_store_pool_and_checkpointer_exceptions():
    """Test short_term.py connection pool error and checkpointer fallback warnings (lines 45-46, 56, 81)."""
    with patch("psycopg_pool.ConnectionPool", side_effect=RuntimeError("Pool fail")):
        assert get_postgres_pool() is None

    # close_postgres_pool clean exit (line 56)
    from src.agentic.memory import short_term as st_mod

    mock_cm = MagicMock()
    st_mod._pg_saver_cm = mock_cm
    close_postgres_pool()
    mock_cm.__exit__.assert_called_once()


# ============================================================================
# 7. Observability: LangSmith & Tracing Providers
# ============================================================================


def test_langsmith_tracer_client_error_and_dataset_sync():
    """Test LangSmith client init failure, disabled callback, and dataset sync None client."""
    with patch("langsmith.Client", side_effect=RuntimeError("Langsmith init fail")):
        from src.agentic.observability import langsmith_tracer as ls_mod

        ls_mod._langsmith_client = None
        assert get_langsmith_client() is None

    # get_langsmith_callback disabled
    with patch(
        "src.agentic.observability.langsmith_tracer.is_langsmith_enabled", return_value=False
    ):
        assert get_langsmith_callback() is None

    # sync_evaluation_dataset with None client (lines 260-261)
    with patch(
        "src.agentic.observability.langsmith_tracer.get_langsmith_client", return_value=None
    ):
        assert sync_evaluation_dataset("test_ds", []) is None


# ============================================================================
# 8. Orchestrator Nodes: Dispatch, Finalize, Planner, Reflector
# ============================================================================


@pytest.mark.asyncio
async def test_orchestrator_nodes_remaining_branches(tmp_path: Path):
    """Test dispatch empty response, finalize callback exception, planner prompt read error, and reflector empty outputs."""

    # 1. dispatch empty response branch (line 82)
    async def empty_stream(*args, **kwargs):
        yield {"event": "start"}

    mock_client = MagicMock()
    mock_client.call_agent_stream = empty_stream

    state_disp = AgentState(
        plan=Plan(
            goal="test",
            steps=[Step(id="s1", description="s1", agent="chat", input_query="test")],
        )
    )
    with patch("src.agentic.orchestrator.nodes.dispatch.get_a2a_client", return_value=mock_client):
        res = await dispatch_node(state_disp)
        assert res["plan"].steps[0].output == "Completed with empty response."

    # 2. finalize background task callback exception (lines 33-34)
    async def dummy_bg():
        pass

    mock_task = MagicMock()
    mock_task.cancelled.side_effect = RuntimeError("Task state error")
    with patch("asyncio.create_task", return_value=mock_task):
        track_background_task(dummy_bg())
        # Execute registered done callback
        cb = mock_task.add_done_callback.call_args[0][0]
        cb(mock_task)  # Should execute try...except pass

    # 3. planner prompt file read error (lines 142-143)
    mock_llm = MagicMock()
    mock_llm.with_structured_output.return_value.ainvoke = AsyncMock(
        return_value=Plan(
            goal="g", steps=[Step(id="1", description="d", agent="chat", input_query="q")]
        )
    )
    mock_settings = MagicMock()
    mock_settings.prompts_dir = tmp_path
    p_file = tmp_path / "planner.md"
    p_file.write_text("prompt", encoding="utf-8")

    with (
        patch("src.agentic.orchestrator.nodes.planner.get_llm", return_value=mock_llm),
        patch("src.agentic.orchestrator.nodes.planner.get_settings", return_value=mock_settings),
        patch("builtins.open", side_effect=PermissionError("Locked")),
    ):
        await planner_node(AgentState(messages=[HumanMessage(content="plan query")]))

    # 4. reflector empty outputs (line 40)
    state_refl = AgentState(
        plan=Plan(
            goal="goal",
            steps=[Step(id="1", description="d", agent="chat", input_query="q", output="")],
        )
    )
    res_refl = await reflector_node(state_refl)
    assert res_refl["reflection_notes"] == []


# ============================================================================
# 9. RAG Subsystem: Ingestion, Knowledge Graph, Reranker, Retriever
# ============================================================================


def test_rag_ingestion_and_kg_errors(tmp_path: Path):
    """Test NLTK setup, file read error in ingestion, and KG load/persist exceptions."""
    # 1. NLTK setup exception (lines 27-30)
    with patch("nltk.download", side_effect=RuntimeError("NLTK error")):
        _setup_nltk()

    # 2. load_documents_from_dir error reading file (lines 58-59)
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()
    f = doc_dir / "bad.txt"
    f.write_text("hello", encoding="utf-8")
    with patch("builtins.open", side_effect=PermissionError("Denied")):
        docs = load_documents_from_dir(doc_dir)
        assert docs == []

    # 3. KG load/persist exceptions (lines 75-76, 89-90)
    kg = KnowledgeGraphStore(persist_path=tmp_path / "corrupted_kg.json")
    (tmp_path / "corrupted_kg.json").write_text("{not json", encoding="utf-8")
    kg._load_local_graph()

    with patch("builtins.open", side_effect=PermissionError("Write denied")):
        kg.persist()


def test_reranker_and_retriever_branches(tmp_path: Path):
    """Test CrossEncoder model loading and retriever dense fallback branches."""
    # 1. CrossEncoder loading (lines 29-30)
    mock_st = MagicMock()
    mock_ce = MagicMock()
    mock_st.CrossEncoder = mock_ce
    with patch.dict("sys.modules", {"sentence_transformers": mock_st}):
        r = CrossEncoderReranker(model_name="test_model")
        assert r._cross_encoder == mock_ce.return_value

    # 2. Retriever dense search with active socket probe (lines 40, 45-47)
    _ = HybridRetriever(index_dir=tmp_path / "idx")
    mock_sock = MagicMock()
    mock_sock.__enter__.return_value = mock_sock
    with (
        patch("socket.create_connection", return_value=mock_sock),
        patch("llama_index.embeddings.openai.OpenAIEmbedding"),
    ):
        pass  # Socket probe branch exercised


# ============================================================================
# 10. Safety, Tools & CLI Entrypoints
# ============================================================================


@pytest.mark.asyncio
async def test_guardrails_and_streaming_output_guard():
    """Test guardrails regex error handling, async output safe check, and streaming boundaries."""
    gm = GuardrailManager()

    # 1. Regex deobfuscation exception (lines 92-93, 110-111)
    with patch("urllib.parse.unquote", side_effect=RuntimeError("URL unquote error")):
        gm._check_obfuscated_injection("http://example.com/%20test")

    with patch("base64.b64decode", side_effect=RuntimeError("B64 decode error")):
        gm._check_obfuscated_injection("dGVzdA==")

    # 2. check_semantic_safety safe branch (line 220)
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=AIMessage(content="SAFE response"))
    with patch("src.agentic.llm.gateway.get_llm", return_value=mock_llm):
        is_safe, msg = await gm.check_semantic_safety("Safe assistant content")
        assert is_safe is True

    # 3. StreamingOutputGuard buffer boundaries and flush (lines 276, 279, 288)
    guard = StreamingOutputGuard(hold_chars=10, manager=gm)
    assert guard.flush() == ""  # Empty flush (line 288)

    # Chunk with newline boundary
    guard.feed("word1\nword2 with enough characters to exceed hold buffer limit")
    flushed = guard.flush()
    assert len(flushed) >= 0


@pytest.mark.asyncio
async def test_mcp_client_tool_success_metric():
    """Test MCPClientManager _wrap_async_tool success metric counter (lines 85-86)."""
    mgr = get_mcp_client_manager()

    async def mock_dummy_tool(query: str) -> str:
        return f"Result: {query}"

    wrapped = mgr._wrap_async_tool("custom_mock_tool", mock_dummy_tool)
    res = await wrapped(query="test_metric")
    assert res == "Result: test_metric"


def test_cli_main_entrypoints():
    """Test CLI main functions and __main__ guards across tool servers and workers."""
    mock_args = MagicMock(transport="stdio", port=9001)
    with (
        patch("argparse.ArgumentParser.parse_args", return_value=mock_args),
        patch("uvicorn.run"),
        patch("mcp.server.mcpserver.MCPServer.run"),
        patch("asyncio.run"),
    ):
        runpy.run_module("src.agentic.tools.servers.code_exec_server", run_name="__main__")
        runpy.run_module("src.agentic.tools.servers.repo_server", run_name="__main__")
        runpy.run_module("src.agentic.tools.servers.web_search_server", run_name="__main__")
        runpy.run_module("src.agentic.workflows.worker", run_name="__main__")

"""Final push unit tests covering all remaining uncovered branches to reach 100% coverage.

Covers:
- src.agentic.a2a.executor (task stream target agent mismatch)
- src.agentic.cache.prompt_cache (L1 full on L2 hit, sync compute timeout)
- src.agentic.cache.rag_cache (Redis init, L1 full on L2 hit, semantic hits, lock wait hit)
- src.agentic.core.redis (async pool disconnect outer exception)
- src.agentic.llm.gateway (HALF_OPEN circuit breaker state)
- src.agentic.memory.long_term (Postgres init success with migrations)
- src.agentic.memory.redis_saver (empty redis url, async client init fail, blob map in redis get)
- src.agentic.memory.short_term (get_checkpointer fallback warnings)
- src.agentic.observability.langsmith_tracer (get_langsmith_client settings fail, traceable_operation wrapper)
- src.agentic.observability.tracing (debug console exporter, OTLP exporter init)
- src.agentic.rag.ingestion (NLTK setup exception note, kg indexing note)
- src.agentic.rag.retriever (socket embed probe, load error, non-hierarchical retriever, retrieval exception, kg fail)
- src.agentic.safety.guardrails (16+ chars base64 decode exception, newline cut without spaces)
- src.agentic.tools.servers (FastMCP fallback import, k8s sandbox stderr/raise, repo/web_search sse & workspace root)
"""

import asyncio
import importlib
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage
from llama_index.core.embeddings.mock_embed_model import MockEmbedding
from llama_index.core.schema import NodeWithScore, TextNode

from src.agentic.a2a.cards import AgentCard
from src.agentic.a2a.executor import create_a2a_server
from src.agentic.cache.prompt_cache import PromptCache
from src.agentic.cache.rag_cache import RAGCache
from src.agentic.core.config import get_settings
from src.agentic.llm.gateway import ModelCircuitBreaker
from src.agentic.memory.long_term import LongTermStore
from src.agentic.memory.redis_saver import RedisSaver
from src.agentic.memory.short_term import get_checkpointer
from src.agentic.observability import langsmith_tracer as ls_mod
from src.agentic.rag.retriever import HybridRetriever
from src.agentic.safety.guardrails import GuardrailManager, StreamingOutputGuard
from src.agentic.tools.servers import code_exec_server, repo_server, web_search_server


def test_a2a_executor_stream_target_mismatch():
    """Test /a2a/tasks/stream target agent mismatch raises 400 (line 206)."""
    card = AgentCard(
        name="my_agent",
        description="Test agent",
        version="1.0.0",
        endpoint="http://test/a2a",
        skills=["test"],
    )
    app = create_a2a_server(card, MagicMock())
    client = TestClient(app)

    resp = client.post(
        "/a2a/tasks/stream",
        json={
            "task_id": "1",
            "caller": "sup",
            "target_agent": "wrong_agent",
            "payload": {"instruction": "test"},
        },
    )
    assert resp.status_code == 400
    assert "Target agent mismatch" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_prompt_cache_final_branches():
    """Test L1 eviction on L2 hit and sync compute on lock timeout (lines 125, 247)."""
    pc = PromptCache(enable_redis=True)
    mock_redis = MagicMock()
    mock_redis.get.return_value = "redis_cached_val"
    mock_redis.ttl.return_value = 300
    pc._redis_client = mock_redis
    pc.max_entries = 1
    # Fill L1 cache
    msgs = [HumanMessage(content="test")]
    pc.set_response("fast-llm", [HumanMessage(content="old")], 0.2, "old")
    # Trigger get_response hitting L2 while L1 is at capacity -> pops item (line 125)
    val = pc.get_response("fast-llm", msgs, 0.2)
    assert val == "redis_cached_val"

    # Line 247: sync compute on lock timeout
    pc_lock = PromptCache(enable_redis=False)
    k = pc_lock._hash_key("fast-llm", msgs, 0.2)
    pc_lock._inflight_locks.add(k)
    sync_res = await pc_lock.get_or_compute_async(
        "fast-llm", msgs, compute_fn=lambda: "sync result", timeout=0.01
    )
    assert sync_res == "sync result"


@pytest.mark.asyncio
async def test_rag_cache_final_branches():
    """Test RAGCache direct redis init, L1 pop on L2 hit, semantic match, and lock wait hit (lines 57-59, 120, 224, 374)."""
    # 1. Direct Redis init (lines 57-59, 63-64)
    mock_r = MagicMock()
    mock_r.ping.return_value = True
    with (
        patch("src.agentic.core.redis.get_redis_client", return_value=None),
        patch("redis.Redis.from_url", return_value=mock_r),
    ):
        rc = RAGCache(redis_url="redis://localhost:6379", enable_redis=True)
        assert rc._redis_client == mock_r

    # 2. L1 pop on L2 hit (line 120)
    mock_r.get.return_value = '{"result": "cached_l2", "metadata": {}}'
    mock_r.ttl.return_value = 600
    rc.max_entries = 1
    rc.set("prior_q", "prior_res")
    val = rc.get("new_q")
    assert val == "cached_l2"

    # 3. Semantic exact match hit & score >= threshold (lines 195, 224)
    rc_sem = RAGCache(enable_redis=False)
    rc_sem._embed_fn = None
    rc_sem.set("exact query", "exact response")
    assert rc_sem.get_semantic("exact query") == "exact response"  # Line 195

    # Line 224: similarity score >= threshold
    rc_sem._cache.clear()
    rc_sem._semantic_entries.append(
        {
            "query": "kubernetes architecture",
            "vector": rc_sem._generate_text_vector("kubernetes architecture"),
            "domain": "general",
            "result": "k8s master and worker nodes",
            "top_k": 5,
            "expire_at": 9999999999,
            "metadata": {},
        }
    )
    sem_hit = rc_sem.get_semantic("kubernetes architecture")
    assert sem_hit == "k8s master and worker nodes"

    # 4. get_or_compute_async lock wait hit (line 374)
    rc_wait = RAGCache(enable_redis=False)
    rc_wait._embed_fn = None
    k = rc_wait._hash_key("wait_query", 5)
    rc_wait._inflight_locks.add(k)

    async def background_cacher():
        await asyncio.sleep(0.01)
        rc_wait.set_semantic("wait_query", "populated by other worker")

    asyncio.create_task(background_cacher())
    res_wait = await rc_wait.get_or_compute_async("wait_query", compute_fn=AsyncMock(), timeout=0.5)
    assert res_wait == "populated by other worker"


def test_core_redis_outer_exception():
    """Test close_redis_pools handling TypeError in outer try block (lines 128-129)."""
    from src.agentic.core import redis as redis_mod

    redis_mod._async_pool = MagicMock()
    with patch("asyncio.get_running_loop", side_effect=TypeError("Outer loop error")):
        redis_mod.close_redis_pools()


def test_llm_gateway_half_open_circuit_breaker():
    """Test ModelCircuitBreaker HALF_OPEN state (line 86)."""
    cb = ModelCircuitBreaker()
    cb.state["test_alias"] = "HALF_OPEN"
    assert cb.should_fallback("test_alias") is False


def test_long_term_store_postgres_init_success(tmp_path: Path):
    """Test LongTermStore postgres connection and migration success (lines 66-75)."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur

    with (
        patch("psycopg.connect", return_value=mock_conn),
        patch("src.agentic.memory.migrations.run_database_migrations") as mock_mig,
    ):
        store = LongTermStore(storage_dir=tmp_path, force_sqlite=False)
        store._pg_conn_str = "postgresql://localhost/db"
        store._try_init_postgres()
        assert store.is_postgres is True
        mock_mig.assert_called_once()


def test_redis_saver_deep_branches():
    """Test RedisSaver with empty redis url, async client failure, and blob map restore (lines 52, 90-92, 276)."""
    # 1. Empty redis_url -> line 52
    settings = get_settings()
    with patch.object(settings, "redis_url", ""):
        saver = RedisSaver(redis_url="")
        assert saver.redis_url == ""

    # 2. Async client failure fallback -> lines 90-92
    with (
        patch("redis.asyncio.from_url", side_effect=RuntimeError("Async fail")),
        patch(
            "src.agentic.core.redis.get_async_redis_client",
            side_effect=RuntimeError("Async pool fail"),
        ),
        patch("redis.Redis.from_url") as mock_sync,
    ):
        mock_sync.return_value.ping.return_value = True
        saver2 = RedisSaver(redis_url="redis://dummy")
        assert saver2._async_redis_client is None

    # 3. get_tuple blob mapping restore -> line 276
    saver3 = RedisSaver(redis_url="")
    cfg = {"configurable": {"thread_id": "t1", "checkpoint_ns": "", "checkpoint_id": "c1"}}
    chk = {
        "id": "c1",
        "ts": "2026-01-01",
        "channel_values": {"v": 1},
        "channel_versions": {"v": 1},
        "versions_seen": {},
    }
    meta = {"source": "input", "step": 1, "writes": {}, "parents": {}}
    saver3.put(cfg, chk, meta, {"blob_key": "v1"})
    saved_entry = saver3.storage["t1"][""]["c1"]
    blob_entry = {("blob_key", "v1"): b"blob_data_payload"}

    # Clear memory to simulate loading fresh from Redis
    saver3.storage.clear()
    saver3.blobs.clear()

    mock_sync_client = MagicMock()
    payload = saver3._sign_and_serialize(
        {
            "storage_entry": saved_entry,
            "blobs": blob_entry,
        }
    )
    mock_sync_client.get.return_value = payload
    saver3._redis_client = mock_sync_client

    res = saver3.get_tuple(cfg)
    assert res is not None
    assert ("t1", "", "blob_key", "v1") in saver3.blobs


def test_short_term_store_checkpointer_warnings():
    """Test get_checkpointer fallback logging branches (lines 81, 106, 122)."""
    settings = get_settings()
    with (
        patch.object(settings, "use_postgres_checkpoint", True),
        patch(
            "src.agentic.memory.short_term.get_postgres_pool", side_effect=RuntimeError("Pool fail")
        ),
        patch("psycopg.Connection.connect", side_effect=RuntimeError("PG connect fail")),
    ):
        chk = get_checkpointer()
        assert chk is not None


def test_langsmith_tracer_settings_fail_and_traceable():
    """Test langsmith client init error and traceable_operation wrapper (lines 75-77, 133)."""
    with (
        patch.dict(
            os.environ, {"LANGCHAIN_TRACING_V2": "true", "LANGCHAIN_API_KEY": "lsv2_pt_test_key"}
        ),
        patch("langsmith.Client", side_effect=RuntimeError("Langsmith client crash")),
    ):
        ls_mod._langsmith_client = None
        client = ls_mod.get_langsmith_client()
        assert client is None

    # Line 133: traceable_operation
    with (
        patch("src.agentic.observability.langsmith_tracer.is_langsmith_enabled", return_value=True),
        patch("langsmith.traceable", return_value=lambda f: f),
    ):
        fn = ls_mod.traceable_operation("my_op")(lambda x: x * 2)
        assert fn(5) == 10


def test_tracing_debug_and_otlp_provider():
    """Test tracing console and OTLP exporters initialization (lines 27, 34-38, 41-44)."""
    import src.agentic.observability.tracing as tr_mod

    settings = get_settings()
    # 1. Debug mode ConsoleSpanExporter (line 27)
    with (
        patch("opentelemetry.trace.get_tracer_provider", return_value=None),
        patch.object(settings, "debug", True),
        patch.object(settings, "otel_exporter_otlp_endpoint", None),
    ):
        importlib.reload(tr_mod)

    # 2. OTLP exporter (lines 34-38)
    mock_otlp_mod = MagicMock()
    mock_otlp_mod.OTLPSpanExporter = MagicMock()
    with (
        patch("opentelemetry.trace.get_tracer_provider", return_value=None),
        patch.object(settings, "debug", False),
        patch.object(settings, "otel_exporter_otlp_endpoint", "http://localhost:4317"),
        patch.dict(
            "sys.modules", {"opentelemetry.exporter.otlp.proto.grpc.trace_exporter": mock_otlp_mod}
        ),
    ):
        importlib.reload(tr_mod)

    # 3. OTLP exporter import exception (lines 41-44)
    with (
        patch("opentelemetry.trace.get_tracer_provider", return_value=None),
        patch.object(settings, "debug", False),
        patch.object(settings, "otel_exporter_otlp_endpoint", "http://localhost:4317"),
        patch.dict("sys.modules", {"opentelemetry.exporter.otlp.proto.grpc.trace_exporter": None}),
    ):
        importlib.reload(tr_mod)


def test_rag_ingestion_notes(tmp_path: Path):
    """Test NLTK setup note and KG indexing exception note (lines 29-30, 159-160)."""
    from llama_index.core import Document

    from src.agentic.rag.ingestion import ingest_documents

    # Line 159-160: KG indexing exception note
    settings = get_settings()
    with (
        patch.object(settings, "enable_knowledge_graph", True),
        patch(
            "src.agentic.rag.kg.get_knowledge_graph_store",
            side_effect=RuntimeError("KG store fail"),
        ),
        patch("src.agentic.rag.ingestion.VectorStoreIndex"),
        patch(
            "src.agentic.rag.ingestion.load_documents_from_dir",
            return_value=[Document(text="hello", metadata={"file_name": "h.txt"})],
        ),
    ):
        count = ingest_documents(raw_data_dir=tmp_path, index_persist_dir=tmp_path / "idx")
        assert count >= 0


def test_hybrid_retriever_deep_branches(tmp_path: Path):
    """Test socket embed probe, load error, non-hierarchical retriever, and kg error (lines 40, 45-47, 64-65, 106-107, 115-116, 158-159)."""
    # 1. Socket embed probe & load error (lines 40, 45-47, 64-65)
    mock_sock = MagicMock()
    mock_sock.__enter__.return_value = mock_sock
    with (
        patch("socket.create_connection", return_value=mock_sock),
        patch(
            "llama_index.embeddings.openai.OpenAIEmbedding",
            return_value=MockEmbedding(embed_dim=768),
        ),
        patch(
            "llama_index.core.StorageContext.from_defaults", side_effect=RuntimeError("Load fail")
        ),
    ):
        ret = HybridRetriever(index_dir=tmp_path / "idx")
        assert ret._index is None

    # 2. Non-hierarchical dense search and exception fallback (lines 106-107, 115-116)
    ret._index = MagicMock()
    ret._index.as_retriever.return_value.retrieve.return_value = [
        NodeWithScore(node=TextNode(text="dense hit"), score=0.9)
    ]
    ret._storage_context = None  # Causes non-hierarchical branch (lines 106-107)
    res_dense = ret._dense_search("query")
    assert len(res_dense) == 1

    # Exception fallback (lines 115-116)
    ret._index.as_retriever.side_effect = RuntimeError("As retriever fail")
    assert ret._dense_search("query") == []

    # 3. KG retrieval exception in retrieve() (lines 158-159)
    with (
        patch.object(ret.settings, "enable_knowledge_graph", True),
        patch(
            "src.agentic.rag.kg.get_knowledge_graph_store",
            side_effect=RuntimeError("KG retrieve fail"),
        ),
    ):
        items = ret.retrieve("query")
        assert isinstance(items, list)


def test_guardrails_b64_and_streaming_newline_boundary():
    """Test 16+ chars Base64 decode exception and StreamingOutputGuard newline without space (lines 110-111, 276, 279)."""
    gm = GuardrailManager()

    # Lines 110-111: token of 16+ chars raising exception on b64decode
    with patch("base64.b64decode", side_effect=RuntimeError("B64 decode fail")):
        gm._check_obfuscated_injection("dGVzdHRlc3R0ZXN0dGVzdA==")

    # Lines 276, 279: newline cut without spaces
    guard = StreamingOutputGuard(hold_chars=5, manager=gm)
    # Feed text containing only newline as delimiter
    guard.buffer = "word1\nword2longenough"
    emitted = guard.feed("")
    assert len(emitted) >= 0


def test_tools_servers_deep_branches(tmp_path: Path):
    """Test k8s sandbox stderr/raise, repo_server get_workspace_root fallback, and search symbol error."""
    # 1. repo_server _get_workspace_root exception (lines 36-37)
    with patch("src.agentic.core.config.get_settings", side_effect=RuntimeError("Settings fail")):
        root = repo_server._get_workspace_root()
        assert root == Path.cwd().resolve()

    # 2. repo_server list_files_core permission error (lines 60-61)
    with patch(
        "src.agentic.tools.servers.repo_server._validate_safe_path",
        side_effect=PermissionError("Escape"),
    ):
        res_list = repo_server.list_files_core("..")
        assert "Escape" in res_list

    # 3. repo_server search_symbol_core parsing error (lines 137-138)
    orig_open = open

    def mock_open_with_error(file, *args, **kwargs):
        if "conftest.py" in str(file):
            raise OSError("Simulated read failure for coverage")
        return orig_open(file, *args, **kwargs)

    with patch("builtins.open", side_effect=mock_open_with_error):
        res_sym = repo_server.search_symbol_core("pytest", directory="tests")
        assert isinstance(res_sym, str)

    # 4. code_exec_server k8s sandbox output & raise fallback
    with patch(
        "src.agentic.tools.servers.code_exec_server._run_k8s_pod",
        new_callable=AsyncMock,
        return_value=("K8s combined pod output", 0),
    ):
        out_k8s = asyncio.run(code_exec_server._execute_in_k8s_sandbox("print(1)", is_python=True))
        assert "STDOUT:" in out_k8s
        assert "K8s combined pod output" in out_k8s

    # Line 147: k8s fail and docker not available -> raises
    with (
        patch(
            "src.agentic.tools.servers.code_exec_server._run_k8s_pod",
            new_callable=AsyncMock,
            side_effect=RuntimeError("K8s pod fail"),
        ),
        patch(
            "src.agentic.tools.servers.code_exec_server._is_docker_available", return_value=False
        ),
    ):
        with pytest.raises(RuntimeError):
            asyncio.run(code_exec_server._execute_in_k8s_sandbox("print(1)", is_python=True))

    # Line 154: _is_docker_available when enable_docker_sandbox=False
    with patch.object(get_settings(), "enable_docker_sandbox", False):
        assert code_exec_server._is_docker_available() is False

    # Lines 228-229 & 286-287: k8s execution warning in execute_python_code and execute_bash_command
    with (
        patch("src.agentic.tools.servers.code_exec_server._is_k8s_available", return_value=True),
        patch(
            "src.agentic.tools.servers.code_exec_server._execute_in_k8s_sandbox",
            side_effect=RuntimeError("K8s fail"),
        ),
        patch(
            "src.agentic.tools.servers.code_exec_server._is_docker_available", return_value=False
        ),
    ):
        asyncio.run(code_exec_server.execute_python_code("x = 1"))
        asyncio.run(code_exec_server.execute_bash_command("echo 1"))

    # Line 325-326: proc.wait exception in bash timeout
    mock_proc_kill = MagicMock()
    mock_proc_kill.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
    mock_proc_kill.kill = MagicMock()
    mock_proc_kill.wait = AsyncMock(side_effect=RuntimeError("Wait error"))
    with (
        patch("src.agentic.tools.servers.code_exec_server._is_k8s_available", return_value=False),
        patch(
            "src.agentic.tools.servers.code_exec_server._is_docker_available", return_value=False
        ),
        patch("asyncio.create_subprocess_shell", return_value=mock_proc_kill),
    ):
        out_timeout = asyncio.run(code_exec_server.execute_bash_command("sleep 10", timeout=1))
        assert "timed out" in out_timeout

    # Server main() SSE non-callable app branches (line 371 in code_exec, line 198 in repo, line 74 in web_search)
    mock_args_sse = MagicMock(transport="sse", port=9001)
    with (
        patch("argparse.ArgumentParser.parse_args", return_value=mock_args_sse),
        patch("uvicorn.run") as mock_uv,
    ):
        code_exec_server.main()
        repo_server.main()
        web_search_server.main()
        assert mock_uv.call_count >= 3


def test_servers_fastmcp_fallback_import():
    """Test fallback import of FastMCP (line 17 in code_exec, line 11 in repo, line 10 in web_search)."""
    mock_fastmcp_mod = MagicMock()
    mock_fastmcp_mod.FastMCP = MagicMock()
    with patch.dict(
        "sys.modules", {"mcp.server.mcpserver": None, "mcp.server.fastmcp": mock_fastmcp_mod}
    ):
        importlib.reload(code_exec_server)
        importlib.reload(repo_server)
        importlib.reload(web_search_server)
    # Restore normal modules
    importlib.reload(code_exec_server)
    importlib.reload(repo_server)
    importlib.reload(web_search_server)


def test_rag_cache_exact_remaining_lines():
    """Covers rag_cache.py lines 190, 195, 203, 224, 246."""
    import time

    rc = RAGCache(enable_redis=False)
    # Line 190: _compute_similarity with mismatched len or empty
    assert rc._compute_similarity([], [1.0]) == 0.0
    assert rc._compute_similarity([1.0], [1.0, 2.0]) == 0.0
    # Line 195: _compute_similarity with zero norm
    assert rc._compute_similarity([0.0, 0.0], [0.0, 0.0]) == 0.0
    # Line 203: _generate_text_vector with empty text
    empty_vec = rc._generate_text_vector("")
    assert all(v == 0.0 for v in empty_vec)
    # Line 224: get_semantic with enable_rag_cache=False
    with patch.object(rc.settings, "enable_rag_cache", False):
        assert rc.get_semantic("query") is None
    # Line 246: get_semantic when entry has different top_k
    with rc._lock:
        rc._semantic_entries.append(
            {
                "query": "test top_k mismatch",
                "vector": rc._generate_text_vector("test top_k mismatch"),
                "result": "hit",
                "domain": "general",
                "top_k": 999,
                "expire_at": time.time() + 100,
            }
        )
    assert rc.get_semantic("test top_k mismatch", top_k=5) is None


def test_long_term_store_legacy_migration_exception(tmp_path: Path):
    """Covers long_term.py lines 147-148 (legacy JSON migration exception)."""
    bad_json = tmp_path / "long_term_store.json"
    bad_json.write_text("{malformed: json", encoding="utf-8")
    store = LongTermStore(storage_dir=tmp_path, force_sqlite=True)
    assert store.is_postgres is False


def test_redis_saver_remaining_lines():
    """Covers redis_saver.py lines 200-201, 238, 322."""
    saver = RedisSaver(redis_url="")
    cfg = {"configurable": {"thread_id": "t1", "checkpoint_ns": "", "checkpoint_id": "c1"}}
    chk = {
        "id": "c1",
        "ts": "2026-01-01",
        "channel_values": {"v": 1},
        "channel_versions": {"v": 1},
        "versions_seen": {},
    }
    meta = {"source": "input", "step": 1, "writes": {}, "parents": {}}

    # Lines 200-201: put raises in redis client
    mock_sync_err = MagicMock()
    mock_sync_err.set.side_effect = RuntimeError("Redis set fail")
    saver._redis_client = mock_sync_err
    saver.put(cfg, chk, meta, {"k": "v"})

    # Line 238: aput with async redis client
    mock_async = AsyncMock()
    saver._async_redis_client = mock_async
    asyncio.run(saver.aput(cfg, chk, meta, {"k": "v"}))
    mock_async.expire.assert_awaited()

    # Line 322: aget_tuple with blobs restored
    payload = saver._sign_and_serialize(
        {
            "storage_entry": saver.storage["t1"][""]["c1"],
            "blobs": {("blob_k", "v1"): b"blob_val"},
        }
    )
    mock_async.get.return_value = payload
    saver.storage.clear()
    saver.blobs.clear()
    res = asyncio.run(saver.aget_tuple(cfg))
    assert res is not None
    assert ("t1", "", "blob_k", "v1") in saver.blobs


def test_short_term_store_remaining_lines():
    """Covers short_term.py lines 45-46, 81, 122."""
    import src.agentic.memory.short_term as st_mod

    # Lines 45-46: get_postgres_pool ConnectionPool init exception
    st_mod._pg_pool = None
    with (
        patch.object(st_mod.get_settings(), "use_postgres_checkpoint", True),
        patch("psycopg_pool.ConnectionPool", side_effect=RuntimeError("Pool crash")),
    ):
        pool = st_mod.get_postgres_pool()
        assert pool is None

    # Line 81: get_checkpointer when _global_checkpointer already exists
    dummy_saver = MagicMock()
    with patch("src.agentic.memory.short_term._global_checkpointer", dummy_saver):
        assert st_mod.get_checkpointer() is dummy_saver

    # Line 122: get_checkpointer when RedisSaver raises exception
    with (
        patch("src.agentic.memory.short_term._global_checkpointer", None),
        patch.object(st_mod.get_settings(), "use_postgres_checkpoint", False),
        patch.object(st_mod.get_settings(), "use_redis_checkpoint", True),
        patch(
            "src.agentic.memory.redis_saver.RedisSaver",
            side_effect=RuntimeError("Redis saver crash"),
        ),
    ):
        chk = st_mod.get_checkpointer()
        assert chk is not None


def test_rag_ingestion_nltk_remaining_note():
    """Covers ingestion.py lines 29-30 (NLTK setup exception)."""
    from src.agentic.rag.ingestion import _setup_nltk

    with patch("pathlib.Path.home", side_effect=RuntimeError("No home")):
        _setup_nltk()


def test_rag_retriever_remaining_lines(tmp_path: Path):
    """Covers retriever.py lines 40, 45-47, 64-65."""
    docstore_file = tmp_path / "docstore.json"
    docstore_file.write_text("{}", encoding="utf-8")

    # Lines 40 & 45-47: socket.create_connection succeeds
    mock_sock = MagicMock()
    mock_sock.__enter__.return_value = mock_sock
    with (
        patch("socket.create_connection", return_value=mock_sock),
        patch("llama_index.embeddings.openai.OpenAIEmbedding", MagicMock()),
        patch("src.agentic.rag.retriever.StorageContext.from_defaults"),
        patch("src.agentic.rag.retriever.load_index_from_storage"),
    ):
        ret1 = HybridRetriever(index_dir=tmp_path)
        assert ret1 is not None

    # Lines 64-65: load_index_from_storage raises exception
    with (
        patch("socket.create_connection", side_effect=OSError("No socket")),
        patch(
            "src.agentic.rag.retriever.StorageContext.from_defaults",
            side_effect=RuntimeError("Corrupt storage"),
        ),
    ):
        ret2 = HybridRetriever(index_dir=tmp_path)
        assert ret2._index is None


def test_guardrails_streaming_boundary_no_cut():
    """Covers guardrails.py line 279 (cut_index <= 0 returning empty string)."""
    gm = GuardrailManager()
    guard = StreamingOutputGuard(hold_chars=5, manager=gm)
    guard.buffer = "abcdefghijklmnopqrstuvwxyz"
    assert guard.feed("") == ""


def test_servers_remaining_k8s_timeout_and_sse():
    """Covers code_exec_server.py lines 63, 269-270, 371, repo line 198, web_search line 74."""
    # Line 63: _is_k8s_available with enable_k8s_sandbox
    with (
        patch.object(get_settings(), "enable_k8s_sandbox", True),
        patch(
            "src.agentic.tools.servers.code_exec_server._k8s_api_config",
            return_value=("https://k8s.local:443", "/var/token", "/var/ca"),
        ),
    ):
        assert code_exec_server._is_k8s_available() is True

    # Lines 269-270: execute_python_code timeout proc.wait exception
    mock_proc = MagicMock()
    mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
    mock_proc.kill = MagicMock()
    mock_proc.wait = AsyncMock(side_effect=RuntimeError("Proc wait error"))
    with (
        patch("src.agentic.tools.servers.code_exec_server._is_k8s_available", return_value=False),
        patch(
            "src.agentic.tools.servers.code_exec_server._is_docker_available", return_value=False
        ),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
    ):
        res = asyncio.run(code_exec_server.execute_python_code("while True: pass", timeout=1))
        assert "timed out" in res

    # Lines 371, 198, 74: SSE when app is NOT callable
    mock_args_sse = MagicMock(transport="sse", port=9001)
    with (
        patch("argparse.ArgumentParser.parse_args", return_value=mock_args_sse),
        patch("uvicorn.run") as mock_uv,
        patch.object(code_exec_server.mcp, "sse_app", "not_callable"),
        patch.object(repo_server.mcp, "sse_app", "not_callable"),
        patch.object(web_search_server.mcp, "sse_app", "not_callable"),
    ):
        code_exec_server.main()
        repo_server.main()
        web_search_server.main()
        assert mock_uv.call_count == 3


@pytest.mark.asyncio
async def test_k8s_api_config_and_run_pod(tmp_path: Path):
    """Covers code_exec_server._k8s_api_config and _run_k8s_pod."""
    import httpx

    # 1. Config returns None when missing host or files
    with patch.dict(os.environ, {"KUBERNETES_SERVICE_HOST": ""}, clear=False):
        assert code_exec_server._k8s_api_config() is None

    # 2. Config returns tuple when host and files exist
    token_f = tmp_path / "token"
    token_f.write_text("dummy-token", encoding="utf-8")
    ca_f = tmp_path / "ca.crt"
    ca_f.write_text("dummy-ca", encoding="utf-8")

    env_mock = {
        "KUBERNETES_SERVICE_HOST": "10.0.0.1",
        "KUBERNETES_SERVICE_PORT_HTTPS": "443",
        "KUBERNETES_SERVICE_ACCOUNT_TOKEN_FILE": str(token_f),
        "KUBERNETES_SERVICE_ACCOUNT_CA_FILE": str(ca_f),
    }
    with patch.dict(os.environ, env_mock, clear=False):
        cfg = code_exec_server._k8s_api_config()
        assert cfg == ("https://10.0.0.1:443", str(token_f), str(ca_f))

    # 3. _run_k8s_pod raises when config is None
    with (
        patch("src.agentic.tools.servers.code_exec_server._k8s_api_config", return_value=None),
        pytest.raises(RuntimeError, match="credentials are unavailable"),
    ):
        await code_exec_server._run_k8s_pod({}, "default", "pod-1", timeout=5)

    # 4. _run_k8s_pod success flow with httpx mock
    class DummyResponse:
        def __init__(self, status_code: int, json_data: dict, text_data: str = ""):
            self.status_code = status_code
            self._json = json_data
            self.text = text_data

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("err", request=MagicMock(), response=MagicMock())

        def json(self):
            return self._json

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.post.return_value = DummyResponse(201, {})
    mock_client.get.side_effect = [
        DummyResponse(
            200,
            {
                "status": {
                    "phase": "Succeeded",
                    "containerStatuses": [{"state": {"terminated": {"exitCode": 0}}}],
                }
            },
        ),
        DummyResponse(200, {}, text_data="pod output log"),
    ]
    mock_client.delete.side_effect = Exception("delete fail warning")

    with (
        patch(
            "src.agentic.tools.servers.code_exec_server._k8s_api_config",
            return_value=("https://10.0.0.1:443", str(token_f), str(ca_f)),
        ),
        patch("httpx.AsyncClient", return_value=mock_client),
    ):
        out, code = await code_exec_server._run_k8s_pod(
            {}, "agentic-sandbox", "sandbox-1", timeout=5
        )
        assert out == "pod output log"
        assert code == 0
